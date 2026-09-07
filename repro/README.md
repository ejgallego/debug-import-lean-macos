# C experiment for Lean's macOS mmap slowdown

`mmap-replay.c` is a standalone starting point for reproducing the VM behavior.
It requires a C compiler and Lean artifact files, but does not link or run Lean.
CI has exercised it on Linux and Intel/ARM macOS. Descending creation order makes
mapping setup about 8x/9.3x faster on Intel/ARM with actual Lean artifacts. Full
page sweeps also expose working-set thrashing on the small ARM runner; connecting
these effects to the original warm-page issue still needs investigation. See
[FINDINGS.md](FINDINGS.md) for the evidence and its limits.

The next reduction, `mmap-hints.c`, runs without any Lean files:

```sh
mkdir -p results/hints
cc -O2 -g -std=c11 -Wall -Wextra -Werror repro/mmap-hints.c -o results/hints/mmap-hints
results/hints/mmap-hints 32768 1073741824 shuffled file
results/hints/mmap-hints 32768 1073741824 ascending file
results/hints/mmap-hints 32768 65536 any file
```

It maps one page repeatedly with no page access. Arguments are count, address
spacing in bytes, order (`any`, `ascending`, `descending`, `shuffled`), and backing
(`file`, `anon`). Run `python3 scripts/run-mmap-hints.py --output results/hints`
for the count/order/spacing controls. Pushes to `experiments/mmap-hints` run the
standalone experiment on Linux and both macOS architectures without downloading
Lean or Mathlib. The full artifact replay remains on `experiments/mmap-replay`.

The initial question is whether Lean's many file mappings and a simple page-access
pattern suffice to produce repeated expensive faults on macOS. If they do, shrink
the file list and program while preserving that behavior. If they do not, add the
missing behavior observed in Lean, such as interleaved mapping/access, private
writes, address placement, or anonymous heap pressure.

## Capture the mapping sequence on Linux

Use the existing configured project and cached artifacts. Do not update the
toolchain between the reference capture and replay.

```sh
mkdir -p results/mmap-repro
env LEAN_NUM_THREADS=1 lake env strace -f -yy -s 4096 \
  -e trace=mmap,munmap -o results/mmap-repro/lean-direct.strace \
  lean ImportMathlibModule.lean
python3 scripts/mapping-files.py results/mmap-repro/lean-direct.strace \
  > results/mmap-repro/files.txt
```

Putting `strace` inside `lake env` excludes Lake's own imports. Use
`ImportMathlib.lean` for the legacy case. The extractor retains duplicates and
mapping-attempt order, including split strace records. With multiple calling
threads, entry order is not necessarily completion order. Paths escaped by
strace are rejected rather than guessed.

The manifest is one path per line. It can also be written by hand or derived
from a macOS capture. A recursively sorted file list is a different workload,
not the recorded import sequence. The Linux order is an initial approximation
on macOS, to be checked against a macOS Lean trace if the replay stays fast.

## Build and run on Linux or macOS

```sh
mkdir -p results/mmap-repro
cc -O2 -g -std=c11 -Wall -Wextra -Werror \
  repro/mmap-replay.c -o results/mmap-repro/mmap-replay
results/mmap-repro/mmap-replay --passes 3 --cycles 2 \
  results/mmap-repro/files.txt > results/mmap-repro/replay.csv
```

To transfer the Linux sequence to a Mac with the same Lean/Mathlib revisions and
its own cached artifacts, transfer the strace file and rebase its paths:

```sh
python3 scripts/mapping-files.py results/mmap-repro/lean-direct.strace \
  --project-root . --lean-prefix "$(lake env lean --print-prefix)" \
  > results/mmap-repro/files.txt
```

This uses the destination artifacts' sizes and saved addresses; it is not a claim
that Linux and macOS artifact bytes are identical. Record the local revisions.
The existing capture in this workspace uses `nightly-2026-09-04` and Mathlib
`9d1a52c11563`, not the September 6 toolchain used by the downloaded CI baseline.

## What the program does

For every file, it performs open, stat, an 88-byte compacted-header read, a private
writable mapping at the saved address, and close. Like the inspected Lean loader,
it rejects a different returned address and falls back to allocation plus a full
read. It uses Linux's non-replacing fixed-address flag where applicable; on macOS
the address is a hint. It never uses destructive `MAP_FIXED`.

It retains all regions, reads one byte per host page on each pass, then explicitly
unmaps/frees all regions. `--cycles 2` repeats mapping and access within one process;
invoke the executable twice to compare fresh processes. The `map` phase includes
metadata/header reads and any copy fallback, not just the mmap syscall. Normal
process-exit costs require an external timer or trace; they are not in `unmap`.

Controls, to run separately:

- `--any-address`: let the kernel choose the mapping address.
- `--map-only`: omit page-access passes, retaining the loader's header reads and
  any full-file copy fallback. This isolates mapping setup without touching the
  entire mapped working set.
- `--map-order INDICES.txt`: create mappings in the supplied permutation of
  zero-based manifest indices. Every index must occur exactly once. Access,
  residency probes, and teardown still follow the original manifest order.
- `--write`: write the same byte back once per page, inducing private writes
  without modifying the backing files. This approximates COW activity, not Lean's
  relocation algorithm.
- `--residency`: query mappings before the first access pass and after each pass;
  report resident pages and transitions since the preceding probe. Queries are
  timed separately and allocate diagnostic buffers. Use unprobed timing runs too.
  Fallback buffers are excluded. Totals count mapped pages, including duplicates,
  not unique physical pages. Snapshots are sequential, not globally atomic; pages
  evicted and recovered between probes will not appear as losses. Raw flags are
  kept in memory for comparison but are not exported by this initial program.

CSV records include wall/user/system time, faults, checksum, mapping outcomes,
total bytes, and fallback bytes. Fault and residency accounting is OS-specific.

This replays artifact mapping attempts, not the full OS trace. It does not model
Lean's pointer traversal, dependency relocation, heap allocations, intervening
syscalls, or exact page-reference history. A fast result does not rule out Lean's
reclamation problem. Before claiming a reproducer, require the same macOS symptom
and a corresponding native/kernel profile or residency signature.

## GitHub-hosted experiments

The `C mmap experiment` workflow captures a Linux module import, then runs the
C replay on Linux, Intel macOS 15, and ARM macOS 15. It uses the checked-in
`lean-toolchain` and `lake-manifest.json`; it does not advance the nightly.
Pushes to `experiments/mmap-replay` or `experiments/mmap-artifact-order` trigger
the experiment. The workflow also defines manual dispatch with an import-case
choice, repetition count, and suite (`order` or `placement`); GitHub
requires the workflow on the default branch to enable manual dispatch there.

The default `order` suite runs a real Lean import followed by two order-balanced
repetitions comparing captured order with descending saved-address order. Each C
process performs two map/unmap cycles with no page-access loop. The runner reads
headers before the timed processes to prepare a stable permutation, preserving
the relative order of equal addresses. It records the permutation and an artifact
inventory. The C loader itself still opens, stats, reads each header, maps, and
closes each file. Check fallback counts and bytes: overlapping ranges can cause
different mappings to be rejected when creation order changes.

Run this suite locally after compiling the binary into the output directory:

```sh
env LEAN_NUM_THREADS=1 python3 scripts/run-mmap-experiment.py \
  --trace results/mmap-repro/lean-direct.strace \
  --source ImportMathlibModule.lean --output results/mmap-repro --suite order
```

The `placement` suite runs a real Lean import, two order-balanced repetitions of saved-address
and kernel-selected-address C processes, then a separate residency run. Each C
process makes three access passes per cycle and two map/unmap cycles. Individual
processes have a five-minute timeout. macOS also attempts a separate `sample`
profile and `vmmap` summary; inspect their status and output before assuming a
usable profile was obtained. Neither supplies guaranteed kernel-stack attribution.

Artifacts contain the Linux mapping trace, rebased file lists, compiler/binary and
input identities, phase CSVs, process exit statuses and wall times, VM snapshots,
and job summaries. They are uploaded even when experiment steps fail. The ARM
runner's smaller RAM is a potential pressure variable, not an OS-only comparison.

## Profile the remaining ARM import cost

The `ARM Lean import profile` workflow runs only on `macos-15` (ARM), using the
same pinned Lean and Mathlib. Pushes to `experiments/lean-arm-profile` trigger it.
It profiles the actual module import, not the C replay:

```sh
env LEAN_NUM_THREADS=1 lake env python3 scripts/profile-lean-import.py \
  --output results/lean-arm-profile
```

It retains the first import separately, runs two unprofiled imports, a separate
native-sampled import, then one final unprofiled import. CI also uses
`--sample-initial` to profile the first import, before any Mathlib warmup. This is
the cache state left by artifact download, not a controlled cold-cache condition.
`wait4` reports CPU,
peak RSS, faults, and resource counters for the direct Lean PID; profiler and
monitoring processes are excluded. The diagnostic uses `sample` at 1 ms for up
to 120 seconds, with `-mayDie` to retain symbols if Lean exits. It also records
roughly one-second process/VM observations. Each Lean process has a five-minute
timeout. Native samples include waiting threads and kernel-call boundaries;
they do not supply internal kernel stacks or a CPU-only time breakdown.
The sampling approach follows Apple's
[description of native stack sampling](https://developer.apple.com/library/archive/documentation/Performance/Conceptual/CodeSpeed/Articles/DiagnosingSlowness.html).

Artifacts preserve raw profiles, the runner's `sample` manual, per-process
metrics, diagnostic timelines, host-wide VM snapshots, and input identities.
Check PID, symbols, coverage, tool exits, and diagnostic overhead before deriving
attribution. Use `--no-sample` for a local Linux functional control.

Summarize a downloaded profile with:

```sh
python3 scripts/summarize-lean-sample.py results/lean-arm-profile/sampled.sample.txt
```

The summary checks call-tree count conservation and selects the thread containing
the most `importModules` observations. Its disjoint categories exclude other
threads' idle observations from the denominator. Counts are stack observations,
not exact seconds or CPU-only percentages; retain and inspect the raw call tree.

## Direct phase and syscall timings on ARM

The `ARM Lean phase timings` workflow on `experiments/lean-arm-phases` uses the
unchanged pinned Lean executable. A diagnostic library interposes artifact
`open`, `read`, `mmap`, `lseek`, `fstat`, and `close` calls. It keeps aggregate
elapsed times in memory and writes them at process exit. It passes mapping
arguments through unchanged. Files are identified by the compacted-artifact
suffixes used in the captured workload; duplicated descriptors and unrelated
loader APIs are not a general-purpose tracing interface.

The library classifies reads after the loader's successful seek back to offset
zero as copy-fallback reads. Their time excludes allocation, rejected-map cleanup,
and relocation. Before-seek reads are checked against the expected 88-byte
headers for all 37,687 mappings. The runner rejects incomplete capture, including
descriptor-table overflow or inconsistent fallback counts.

LLDB separately records entry and return of `l_Lean_importModulesCore` and
`l_Lean_finalizeImport`, taking counter snapshots by reading target memory.
ASLR remains enabled. Only four phase stops are expected per import. Phase wall
times exclude the entry handler body but include debugger stop/resume transport;
they are diagnostics, not uninstrumented baseline times. Loading an extra library
can itself change address collisions, so compare fallback counts too.

After a first uninstrumented import, the suite runs uninstrumented, I/O-timed,
and phase-timed processes in a recorded sequence. Raw baselines, debugger logs,
phase events, timing counters, and VM snapshots are preserved. The Linux preload
backend is only a functional control; decisive timings are ARM macOS.

This uses Apple's
[static dyld interposition mechanism](https://github.com/apple-oss-distributions/dyld)
and the [LLDB Python API](https://lldb.llvm.org/use/python-reference.html).

## Initial local control

The first local Linux check mapped all 37,687 regions (7,303,535,912 file bytes)
at their saved addresses with no fallback. Comparing Linux strace captures of
Lean and C confirmed the same artifact mmap request sequence: addresses, lengths,
protections, flags, paths, and offsets. Repeated passes through unchanged
mappings were about 0.02 seconds after a roughly 0.4-second first pass. This is
a functional control, not macOS evidence or a comparison with CI hardware.

### Cross-platform real Lean phases

`lean-platform-phases.yml` runs the same pinned `module; public import Mathlib`
consumer on ARM macOS, ARM Linux, and x86-64 Linux. Each job runs one initial
process, three uninstrumented warm controls, two passive syscall-timer processes,
and three LLDB phase processes, in the same interleaved order. This is the actual
Lean executable, not the C replay. The initial process is not a controlled cold
cache measurement. `LEAN_NUM_THREADS=1` is set in all jobs; ASLR remains enabled.

On Linux, install LLDB, then run:

```sh
mkdir -p results/platform-phases
cc -O2 -g -std=c11 -Wall -Wextra -Werror -shared -fPIC \
  repro/lean-io-timing.c -ldl -o results/platform-phases/lean-io-timing.so
LEAN_NUM_THREADS=1 lake env python3 scripts/run-lean-phases.py --output results/platform-phases
```

On ARM macOS, build `lean-io-timing.dylib` with `-dynamiclib` instead of
`-shared -fPIC -ldl`, and also build the external observer:

```sh
cc -O2 -g -std=c11 -Wall -Wextra -Werror repro/process-usage.c \
  -o results/platform-phases/process-usage
```

The same runner command then applies. Output must be fresh. `records.json`
contains phase wall time, user/kernel CPU, resource counter snapshots and deltas,
and artifact syscall times. The runner checks the CPU counter units against a
short busy loop, and validates that all 37,687 artifact operations fall inside
loading and none inside finalization. A failed check fails the job.

Linux CPU comes from `/proc/PID/stat` (clock-tick resolution, recorded in JSON).
ARM macOS uses `proc_pidinfo(PROC_PIDTASKINFO)` with Mach timebase conversion.
The external observer runs while Lean is stopped; its work is excluded from
Lean's CPU and the entry-handler portion of phase wall time. Debugger transport,
cache effects, and target breakpoint handling still affect diagnostic timings.
Linux major/minor faults and macOS page-ins/faults/COW faults retain their native
names and must not be treated as identical cross-OS counters. Resident-byte
deltas are not peak memory. CPU totals cover all Lean threads. Wall minus CPU
is a residual, not a direct disk-wait measurement.

Linux wrappers forward to libc after constructor initialization, with raw syscall
bootstrap only for allocator activity before constructors. The pinned Linux
binary's `__fxstat` entry point is covered. Injecting a library can still alter
address collisions. Compare the adjacent uninstrumented controls; different CI
hardware and RAM prevent interpreting host ratios as a pure OS effect.
