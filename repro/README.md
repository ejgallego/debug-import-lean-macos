# C experiment for Lean's macOS mmap slowdown

`mmap-replay.c` is a standalone starting point for reproducing the VM behavior.
It requires a C compiler and Lean artifact files, but does not link or run Lean.
CI has exercised it on Linux and Intel/ARM macOS. It exposes expensive hinted
mapping on Intel and working-set thrashing on the small ARM runner; connecting
those effects to the original Lean issue still needs investigation. See
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

## Initial local control

The first local Linux check mapped all 37,687 regions (7,303,535,912 file bytes)
at their saved addresses with no fallback. Comparing Linux strace captures of
Lean and C confirmed the same artifact mmap request sequence: addresses, lengths,
protections, flags, paths, and offsets. Repeated passes through unchanged
mappings were about 0.02 seconds after a roughly 0.4-second first pass. This is
a functional control, not macOS evidence or a comparison with CI hardware.
