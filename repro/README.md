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
phase events, timing counters, and VM snapshots are preserved. The cross-platform
workflow below extends the same phase method to Linux.

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

## Cross-platform real Lean phases

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

On ARM macOS:

```sh
mkdir -p results/platform-phases
cc -O2 -g -std=c11 -Wall -Wextra -Werror -dynamiclib \
  repro/lean-io-timing.c -o results/platform-phases/lean-io-timing.dylib
cc -O2 -g -std=c11 -Wall -Wextra -Werror repro/process-usage.c \
  -o results/platform-phases/process-usage
LEAN_NUM_THREADS=1 lake env python3 scripts/run-lean-phases.py --output results/platform-phases
```

Output must be fresh. `records.json`
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

## Real Lean finalization substeps without a debugger

The `lean-inner-phases.yml` experiment rebuilds `Lean.Environment` from the pinned
source, compiles the pinned C++ shell, and links them with the release's native
archives. It creates an unmodified relinked control and an instrumented relink.
Both use the normal Lean frontend and the original `.olean`/IR files. A sibling
`lib` symlink supplies the original toolchain library path. The stock release
binary remains a separate control. This avoids rebuilding Mathlib and does not
change saved-address mapping policy.

The instrumenter refuses missing or duplicate source anchors. It brackets:

- Loading, and finalization as a whole.
- Module preparation and constant-count calculation.
- Private constant/name-to-module tables, including allocation of all three hash
  tables; then population of the public table.
- Initial extension states, base environment assembly, and imported entries for
  the private, IR, and server views.
- Persistent marking before extension initialization and after it.
- Extension initialization as a whole, each named extension, and the nested
  execution of `[init]` attributes.

Extension spans include `addImportedFn`, state installation, and any registry
expansion/attribute updates. They are not pure `addImportedFn` timings.
The logger records monotonic wall time and `getrusage(RUSAGE_SELF)` user/kernel
CPU, minor/major faults, input blocks, and context switches at each boundary.
Events stay in a bounded memory buffer until process exit. The summary validates
nesting, complete stage coverage, nonnegative counters, and workload cardinalities;
it emits both inclusive and child-subtracted exclusive values. Do not add nested
spans. Sampling resolution and OS fault semantics still apply.

On ARM macOS, starting from this pinned repository:

```sh
lake exe cache get
mkdir -p results/inner-source results/inner
curl -fsSL https://codeload.github.com/leanprover/lean4/tar.gz/58774429865502f05c63239266aac30ef1e91ef7 \
  -o results/inner-source.tar.gz
tar -xzf results/inner-source.tar.gz -C results/inner-source --strip-components=1
python3 scripts/build-lean-inner.py --source results/inner-source --output results/inner-build
cc -O2 -g -std=c11 -Wall -Wextra -Werror -dynamiclib repro/lean-io-timing.c \
  -o results/inner/lean-io-timing.dylib
LEAN_NUM_THREADS=1 lake env python3 scripts/run-lean-inner.py \
  --build results/inner-build --output results/inner
```

Linux additionally needs clang and libc++ headers (Ubuntu 24.04:
`sudo apt-get install clang libc++-18-dev`). Build the validation library with
`-shared -fPIC -ldl` and suffix `.so`. `--libcxx-include` can point the build script
at separately extracted libc++ headers. Use fresh output directories.

The suite records initial runs separately, then balances stock/control/diagnostic
order across three passes. It also runs the diagnostic binary with logging off,
and checks artifact-call counts for all three binaries in separate I/O diagnostics.
The phase measurements themselves use neither a debugger nor a preload library.
Build commands, source patch, generated C, archive/binary hashes, machine details,
raw snapshots, and whole-process `wait4` measurements are retained. Changes in
linkage, compiler code shape, and allocation remain possible confounders; compare
the relinked and stock controls before treating phase timings as representative.

The outer `finalize` span includes caller-side release of the temporary
`ImportState` after `finalizeImport` returns. This differs from the older LLDB
function-return boundary. Its exclusive metrics expose work outside the named
stages; they must remain unassigned until the generated-C release and other
boundary gaps are timed directly. Whole-process faults outside the two outer
spans are likewise retained rather than attributed to imported data.

## Real Lean fault addresses and memory activity

`lean-fault-addresses.yml` runs on ARM macOS and Linux. Select `both`, `macos`,
or `linux` when dispatching it. It first probes the OS tracing tools, rebuilds
the pinned frontend, and collects an initial memory diagnostic followed by
untraced and traced repeats. All fault tracing needs root on the disposable CI
runner; the Lean child retains the ordinary runner identity. Normal memory
snapshots need no privilege.

After the source/build preparation above, on macOS:

```sh
mkdir -p results/faults
cc -O2 -std=c11 -Wall -Wextra -Werror -dynamiclib repro/lean-io-timing.c \
  -o results/faults/lean-io-timing.dylib
LEAN_NUM_THREADS=1 lake env python3 scripts/run-lean-faults.py \
  --build results/inner-build --output results/faults
python3 scripts/summarize-lean-faults.py results/faults --output results/faults/summary.json
```

Linux needs `perf` for the running kernel and the `.so` build of the interposer.
The runner explicitly requests `perf --clockid mono`; omitting it invalidates
alignment with Lean's phase clocks. Raw perf data, decoded events, ktrace output,
phase snapshots, endpoint memory maps, accepted artifact mmap ranges, target
PID, and target/task versus host memory counters are retained.

`LEAN_MEMORY_TIMING=1` adds coarse per-phase disk bytes and resident memory;
macOS also records compressed bytes and cumulative target decompressions.
`LEAN_FAULT_REGIONS` requests an endpoint map census after the final phase
marker. `LEAN_FAULT_MAPS` adds accepted artifact ranges to the I/O observer.
These diagnostic modes preserve the workload but add observer work; compare
untraced runs before using their elapsed times.

The summary validates workload counts, successful exits, artifact call coverage,
target identity, paired Mac events, trace loss and fault-count reconciliation.
It retains mappings at the mmap completion boundary as a separate category;
endpoint maps do not recover temporary allocations freed before the census.
An incomplete trace fails validation after writing its diagnostic summary.
Parser checks can be run with `python3 scripts/test-lean-fault-summary.py`.
See `FINDINGS.md` for the measured mechanism split and observer limitations.

## Process-only transparent huge page control (ARM Linux)

`lean-thp.yml` compares the same Lean executable with the host's default THP
policy and with `PR_SET_THP_DISABLE` set before `exec`. The small
`lean-thp-exec.c` launcher checks its inherited and requested states, then becomes
the target process. The instrumented target independently verifies the state
at startup and exit. No host setting is changed, and no privilege is needed for
this process-scoped intervention.

After building the pinned instrumented frontend as above:

```sh
mkdir -p results/thp
cc -O2 -std=c11 -Wall -Wextra -Werror -shared -fPIC repro/lean-io-timing.c \
  -ldl -o results/thp/lean-io-timing.so
LEAN_NUM_THREADS=1 lake env python3 scripts/run-lean-thp.py \
  --build results/inner-build --output results/thp
python3 scripts/summarize-lean-faults.py results/thp --output results/thp/fault-summary.json
python3 scripts/summarize-lean-thp.py results/thp
```

The full suite has 28 imports: stock and instrumented warmups in both modes,
four AB/BA pairs for each binary, then two pairs each for page censuses and
fault traces. Initial runs are retained separately. Use `--no-trace` to omit the
privileged `perf` diagnostics; `--repetitions 2` gives a shorter local validation.
The main stock timing runs have no in-process observer enabled. Phase timing
runs collect the existing coarse resource snapshots but have no huge-page census
or tracer. The same executable is used for both policies within each comparison.

`LEAN_THP_TIMING=1` enables separate `/proc/self/smaps_rollup` snapshots around
load, private tables, extension initialization, and outer finalization. They
record `Anonymous`, `AnonHugePages`, and time spent reading the census. These
reads walk many mappings and measurably add kernel work; their timings are
excluded from the headline comparison. `AnonHugePages` measures resident huge
pages at the snapshot, not cumulative allocation or all possible THP sizes.
Host THP policy for each exposed size is recorded before and checked after.

`thp-summary.md` and `thp-summary.json` preserve distributions and paired timing
ratios as well as the separate page census. `fault-summary.json` retains address
classes and phase-count reconciliation. A default run with no observed huge pages
is explicitly identified, since disabling an unused mechanism would not test the
hypothesis. The raw launch, target, phase and perf records remain the evidence.

## File fault-around and macOS VM-operation experiments

`lean-around-purge.yml` runs two complementary experiments:

- ARM Linux compares the kernel's original `fault_around_bytes` with one base
  page (fault-around disabled). It runs four AB/BA stock pairs, four phase pairs,
  and two separate trace pairs, preserving warmups separately. This debugfs
  setting is host-wide, so run this experiment on a disposable dedicated runner.
  Each change is read back; `finally` restores and verifies the original value.
  THP is left at its existing policy. Readahead advice is not changed.
- ARM macOS records anonymous `mmap`, `munmap`, `mprotect`, and `madvise` calls,
  plus self-task `mach_vm_allocate`, `mach_vm_deallocate`, `mach_vm_map`,
  `mach_vm_protect`, `mach_vm_copy` and `mach_vm_read_overwrite`, including ranges,
  time, caller, result and thread. Three observer-only imports
  alternate with phase controls; two additional fault traces provide address
  histories. The fixture checks an explicit map/commit/release/reuse/unmap
  sequence before collecting Lean data. Native symbol addresses and image bases
  identify callers in the pinned Lean binary.

After building the pinned frontend and the observer libraries in a fresh
`results/around-purge` directory, run:

```sh
LEAN_NUM_THREADS=1 lake env python3 scripts/run-lean-around-purge.py \
  --build results/inner-build --output results/around-purge
python3 scripts/summarize-lean-faults.py results/around-purge \
  --output results/around-purge/fault-summary.json
# Linux only:
python3 scripts/summarize-lean-around.py results/around-purge
# macOS only:
python3 scripts/summarize-lean-alloc-vm.py results/around-purge
```

The Mac observer is built with:

```sh
cc -O2 -g -std=c11 -Wall -Wextra -Werror -dynamiclib repro/lean-alloc-vm.c \
  -o results/around-purge/lean-alloc-vm.dylib
python3 scripts/check-alloc-vm-fixture.py results/around-purge
nm -n results/inner-build/instrumented/bin/lean > results/around-purge/lean-symbols.txt
```

Build the existing `lean-io-timing` library in that output directory too. The
workflow performs all preparation and retains raw data. The history reducer
splits address intervals for partial purges/unmaps and resets history on remap;
its checks run with `python3 scripts/test-alloc-vm-history.py`.

An observed purge followed by a zero-fill fault at the same address is a
chronological association, not a causal time estimate. These interposers cover
selected libc/Mach entry points after initialization; calls bound internally
inside dyld can bypass them. Kernel reclamation is not observed. Requested-byte
totals can revisit the same memory. The summary reports distinct zero-fill
virtual pages, repeated addresses and kernel return codes separately. Use the
untraced controls for latency claims and verify trace completeness before
interpreting the history categories.

## Failed dynamic symbol lookups

`dlsym-miss.c` is an independent C reduction of the repeated zero-fill lead:

```sh
cc -O2 -std=c11 -Wall -Wextra -Werror repro/dlsym-miss.c -o /tmp/dlsym-miss
/tmp/dlsym-miss hit 77742
/tmp/dlsym-miss miss 77742
/tmp/dlsym-miss unique 77742
```

Add `-ldl` on Linux. `hit` repeatedly looks up `malloc`; `miss` repeats one
absent name; `unique` queries distinct absent names. Each mode warms the lookup
and error paths before recording loop time, CPU and process fault deltas.
No Lean or artifact files are needed. The C test explains a mechanism; its
library set and address space do not reproduce the full Lean import's latency.

`lean-dlsym.yml` runs the C test on both ARM platforms and separately measures
actual Lean on ARM macOS. To build the latter, add `--dlsym` to
`scripts/build-lean-inner.py`. This rebuilds the pinned interpreter in both
control and instrumented binaries. Only the instrumented interpreter's
`lookup_symbol_in_cur_exe` call site forwards to a recorder that still calls
`dlsym(RTLD_DEFAULT, symbol)` and returns its result. There is no global dlsym
interposer, and the native-symbol cache is unchanged. Rebuilt sources, commands
and hashes are retained in the build evidence.

```sh
LEAN_NUM_THREADS=1 lake env python3 scripts/run-lean-around-purge.py \
  --build results/dlsym-build --output results/dlsym --dlsym
python3 scripts/summarize-lean-faults.py results/dlsym \
  --output results/dlsym/fault-summary.json
python3 scripts/summarize-lean-dlsym.py results/dlsym
```

Build the I/O observer in the fresh output directory as in the preceding
workflow. Three recorded imports alternate with phase controls; two separate
ktrace captures associate faults with lookup intervals. The summary rejects
overlapping lookup intervals and retains hit/miss counts, inclusive times,
fault return codes and repeated addresses. It does not equate lookup time with
fault-handler time or with a measured optimization's benefit.
