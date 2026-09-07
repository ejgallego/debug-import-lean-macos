# Debugging macOS Mathlib import performance

Inspection date: 2026-09-07. The immediate objective is to attribute the macOS
slowdown to a specific loading or VM operation, then produce a small reproducer
and a validated explanation. The user has reproduced the problem on M1 and M2
laptops and reports that earlier investigation identified overly aggressive
reclamation of warm pages. Treat that finding as the starting point; recover the
earlier measurements to establish precisely which kind of residency was lost.
Packaging and broad benchmark improvements are deferred until the mechanism is
understood.

## Evidence already available

The repository at `40e59a65e0dd` has a useful paired benchmark: two warm-ups,
seven alternating legacy/module samples, one Lean thread, and separate traced
runs. It measures `lake env lean`, including launcher and process-exit costs.

The completed [CI run 34101969696](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34101969696)
used Lean `nightly-2026-09-06`, commit
`c155094f54eab345cca3da867dbd888a34fbf0d2`, and Mathlib
`79ed1faff530f9e32cc9613210facf9d350dbaed` on both platforms.
Downloaded artifacts are in `results/ci-34101969696/` (ignored by Git).

| Median metric | Linux legacy | macOS legacy | Linux module | macOS module |
|---|---:|---:|---:|---:|
| Wall seconds | 5.130 | 48.390 | 4.202 | 31.988 |
| User CPU seconds | 2.947 | 9.503 | 2.619 | 7.496 |
| System CPU seconds | 2.185 | 39.624 | 1.609 | 23.558 |
| Minor faults | 173,079 | 1,048,487 | 113,044 | 780,567 |
| Major faults | 0 | 0 | 0 | 0 |
| Peak RSS MiB | 6,648.3 | 3,187.1 | 3,738.7 | 2,135.4 |

Each column contains independently computed medians; user and system medians
need not sum to the median total CPU time. macOS legacy wall samples range from
41.63 to 58.35 seconds, so small changes need repeated paired measurements.
The runners differ in CPU, RAM, and virtualization. Cross-OS fault counts and
RSS also require care: neither establishes equivalent physical work or residency.
Zero reported major faults does not establish that all relevant data was resident.

The Lean traces contain a broad `runFrontend` span and tiny command/linter spans,
without native stacks or a breakdown of loading. In the traced processes:

| Case | Linux process / frontend seconds | macOS process / frontend seconds |
|---|---:|---:|
| Legacy | 5.103 / 3.938 | 47.462 / 38.833 |
| Module | 4.194 / 3.080 | 34.791 / 26.850 |

The difference is time outside the frontend span, not a measurement of teardown.
Startup, Lake, initialization, trace output, and exit all need separating.

The pre-existing `results/report/` and `results/module-smoke-report/` compare
Linux data with itself. They are report smoke checks, not macOS evidence.
The report's current strict validation allows this because it checks matching
versions and settings but does not enforce the platform identities.

The checked-in local toolchain/manifest are older (`nightly-2026-09-04`, Mathlib
`9d1a52c11563`). Do not mix those local measurements with the September 6 baseline.

## Relevant Lean code

Inspection used the exact CI revision, downloaded as `lean-module.cpp` and
`lean-compact.cpp` alongside the artifacts. The neighboring `../lean4` checkout
has a different HEAD and existing user changes; use an isolated matching source
checkout for experiments.

- [`src/library/module.cpp`](https://github.com/leanprover/lean4/blob/c155094f54eab345cca3da867dbd888a34fbf0d2/src/library/module.cpp):
  `lean_compacted_region_save` derives a deterministic, 64 KiB-aligned address
  from the module-name hash over a large address range.
  `lean_compacted_region_read` opens/stats/reads the header, requests a writable
  private mapping at that saved address, and accepts it only at the exact address.
  Linux uses `MAP_FIXED_NOREPLACE`; macOS takes the ordinary hint path. Failure
  or a different returned address leads to cleanup and `malloc` plus a full read.
- [`src/runtime/compact.cpp`](https://github.com/leanprover/lean4/blob/c155094f54eab345cca3da867dbd888a34fbf0d2/src/runtime/compact.cpp):
  `region_reader::read` avoids the structural relocation walk only when both its
  own region and every dependency region are at their saved addresses. A missed
  mapping can therefore force writes in dependent regions that mapped correctly.
- `Lean/Environment.lean`: follow module-part loading, import traversal,
  `finalizeImport`, and extension initialization to separate loading from later
  traversal of mapped objects.

Apple documents that a nonzero address is a hint and may produce a different
returned address, and that `MAP_PRIVATE` modifications are copy-on-write.
See [Apple's mmap manual](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/mmap.2.html).
Verify OS-specific experimental flags against the actual runner SDK/manuals.

## Current priority: explain loss of warm pages

This sequence supersedes the broader implementation order below. Keep exact
inputs and raw observations for experiments, but do not spend the next iteration
building collection infrastructure or expanding the platform matrix.

The main workstream is now an aggressively reduced C reproducer, seeded from
Lean's observed OS behavior. `repro/mmap-replay.c` and `scripts/mapping-files.py`
provide the first executable experiment. Reproduce the macOS signature with the
full recorded artifact sequence, then remove files and behaviors while retaining
it. Use focused Lean traces to fill gaps if the C version remains fast; do not
require a complete kernel diagnosis before building the reproducer. A matching
syscall sequence alone is insufficient because it omits ordinary memory accesses.
The checks below guide experiments in this program and comparison with Lean.

1. **Identify the reclamation window.** With the same files and access order,
   compare repeated access through an unchanged mapping, unmap/remap in the same
   process, and a fresh process. Observe immediately after warming, during the
   remaining workload, and just before revisiting pages. Start with no artificial
   memory pressure or intentional delay. Then vary delay and working-set size
   separately. This distinguishes loss during import from loss between imports.
2. **Observe residency transitions for the same file offsets.** Extend the C
   replay, then add a targeted Lean loader hook if necessary. Retain page-level
   residency bitmaps at sparse checkpoints so resident-to-nonresident transitions
   can be identified, rather than comparing unrelated total RSS values. Validate
   `mincore`/Mach page-query semantics and page granularity on the actual macOS
   build, especially for private mappings and backing/shadow objects. A process
   fault does not alone establish eviction of the backing file page. Consult
   [XNU's mincore implementation](https://github.com/apple-oss-distributions/xnu/blob/main/bsd/kern/kern_mman.c)
   and the matching released kernel sources before interpreting results.
3. **Measure the cost of revisiting those pages.** Around each access pass record
   elapsed/user/system time and faults; correlate with file page-ins, disk reads,
   compression/decompression, and native/kernel stacks where exposed. Separate
   missing backing pages from resident backing pages that need fresh process
   translations. Capture profiles in separate repeated runs. System-wide VM
   counters provide context, not exclusive attribution to Lean.
4. **Apply one intervention aimed at retention.** Compare the unchanged mapping
   with a successfully `mlock`ed, bounded subset on an otherwise identical run.
   Record lock success and limits; a failed or partial lock is not an experimental
   condition. Locking changes both retention and fault/translation behavior, so
   combine a timing improvement with residency and stack evidence. Test
   `MADV_WILLNEED` separately as a prefetch/advice control, not as a retention
   guarantee. Pre-touching alone must not be treated as proof pages stay warm.
5. **Reduce the trigger and inspect the implicated XNU path.** Preserve the
   failing file-backed access pattern, then independently vary mapped-file count,
   touched bytes, address dispersion, access order, and private writes. Keep
   copy-on-write/relocation out of the initial read-only control and add it as its
   own dimension. Select the relevant page-reclaim, file-pager, or page-table path
   from observed stacks and residency changes before studying kernel policy.

Do not use a continuously touching helper as the first observer: it changes
reference history and can prevent the reclamation being investigated. Keeping an
extra mapping alive is also an intervention, not necessarily a neutral way to
observe what happens after the original process exits. Compare sparse probing
with unprobed runs to quantify disturbance.

The immediate target result is a causal sequence: identifiable warm pages lose
residency at a particular workload boundary, revisiting them incurs an identified
kernel cost, and a controlled intervention changes both effects. If backing pages
remain resident, follow the translation/fault evidence rather than interpreting
RSS or raw fault totals as file-cache eviction. The prior M1/M2 findings and the
CI data need to be connected by this mechanism; their similarity alone does not
prove an identical cause.

## Broader debugging backlog (deferred)

### 1. Make the baseline reproducible and expose the missing measurements

Change `.github/workflows/benchmark.yml`, `scripts/benchmark.py`, and
`scripts/report.py` in a first implementation step:

- Resolve one immutable Mathlib revision and its toolchain once for the whole
  workflow, and distribute them to both jobs. Provide an explicit pinned mode
  for this investigation; retain nightly tracking as a separate mode.
- Report user/system CPU separately, faults, sample spread, and raw warm-ups.
  Require Linux/macOS identities, matching architecture, and actual sample counts
  in strict reports. Preserve partial results and diagnostics on failed runs.
- Record resolved binary identity, source/patch hashes, build flags, page size,
  runner image, and filesystem. Inventory the actual imported artifact paths,
  sizes, hashes, and part types; a matching source revision alone does not prove
  identical artifact bytes across platforms.
- Keep the existing end-to-end command. Add a direct Lean mode with the Lake
  environment resolved outside each timed sample, plus a minimal `prelude` file
  and an Init-only consumer to measure startup and small-import costs.
- Keep warm-cache measurements first. Explicit warm-ups do not guarantee the
  entire working set remains resident; collect memory pressure and swap context.

Exit criterion: a pinned rerun reproduces the slowdown and separates launcher
cost from Lean process cost. Do not start a large version/architecture matrix yet.

### 2. Attribute the cost with native profiles and phase boundaries

Collect separate diagnostic runs of the direct Lean process in both import modes:

- On macOS, probe availability of `sample`, `vmmap`, and Instruments/`xctrace`.
  Capture user stacks and, where available, kernel stacks with Time Profiler or
  System Trace. On Linux use `perf` as a comparison. Validate process selection,
  symbols, and call chains. Record hosted-runner profiler restrictions explicitly;
  use a developer Mac if kernel attribution is unavailable there.
- Locate time in file metadata/header reads, mapping syscalls, relocation,
  demand faults during import traversal, extension initialization, and exit.
  A user stack stalled beneath the loader is not by itself kernel attribution.
- Add coarse diagnostic boundaries around import loading and finalization, and
  a marker immediately before normal process return. Compare the last marker
  with parent-observed exit to quantify cleanup. Preserve normal exit behavior.
- Capture VM region count, resident/private/dirty memory, page-table information
  where exposed, and compression/swap before, during, and after import.

Exit criterion: identify the dominant phase and sampled native/kernel cost class.
The current frontend-only traces cannot satisfy this criterion.

### 3. Test the loader mechanism indicated by the profile

In a diagnostic build of the exact Lean revision, emit buffered per-region data:
file/part, size, saved and returned address, flags, syscall duration, immediate
errno on failure, exact-address hit, fallback bytes, dependency relocation reason,
and relocation duration. Flush outside the measured phase; keep acceptance runs
free of this logging. Distinguish mapping failure from successful wrong-address
mapping, and own-address relocation from dependency-induced relocation.

| Hypothesis | Discriminating experiment | Supporting result |
|---|---|---|
| Address-hint misses force copying and relocation | Correlate actual returned addresses, fallback bytes, and dependency walks with sampled cost | Substantial fallback/relocation accounting for the slow phase |
| Many mappings or widely scattered addresses make VM bookkeeping expensive | Replay the observed sizes/count/order; compare saved hints, kernel-selected placement, and compact placement in a standalone harness | Cost grows with mapping count or address dispersion at fixed touched bytes |
| Demand faults are expensive even with successful mappings | Time map-only, first read per page, subsequent reads, and unmap separately | Mapping is cheap; first touch accounts for excess system time |
| Relocation triggers expensive copy-on-write faults | Separate read-only access from one private write per page and the actual relocation walk | Dirty-page growth and write-fault time explain the extra cost |
| Teardown of mappings/page tables dominates | Measure explicit unmap and normal process exit separately from loading | A large reproducible cost remains after the final frontend boundary |
| Import processing or filesystem work dominates instead | Follow sampled stacks into metadata reads, allocation, or extension initialization | Cost persists independently of mapping layout/fallback behavior |

Compare the normal loader with a same-revision build bypassing the mmap branch
(the source build has an `MMAP` option; verify its propagation to the tested stage).
This is an attribution experiment: disabling mmap also changes copying,
relocation, residency, and address placement. A speedup alone does not establish
that the mmap syscall is slow. Verify the effective mode in each tested binary.
Likewise, do not substitute `PROT_READ` in Lean where relocation needs writes.

Exit criterion: one controlled change moves the predicted cost bucket and has an
explanation consistent with both the native profile and actual loader decisions.

### 4. Reduce to a portable reproducer

After identifying the cost class, add a small C/C++ replay program that consumes
the observed region inventory. Preserve mapping lifetime and realistic file
backing; repeat mapping one file is only an additional control for many files.
Measure separately open/header, map-only, page reads, private writes, explicit
unmap, and process exit. Check every mapping result and preserve checksums so
accesses cannot be optimized away. Obtain page size at runtime.

Sweep count at fixed total bytes, bytes at fixed count, touch density, address
dispersion, and order. Use anonymous mappings as a control for file-backed work.
Only change one dimension at a time. Alternate baseline/candidate runs on the
same host; retain raw observations. Do not force mappings over occupied memory
with unchecked `MAP_FIXED`.

Also find a smaller Lean import closure reproducing the same hotspot. For
relocation-dependent failures, preserve artifact-part dependencies and layout;
changing import size alone may remove the trigger.

Exit criterion: a reproducible slowdown without full Mathlib, preferably without
Lean, exhibiting the same scaling and dominant phase as the original workload.

### 5. Validate a fix and establish its scope

Implement the smallest change justified by the result, then compare uninstrumented
baseline/candidate builds of the same revision and build mode on the same Mac.
Use order-balanced repeats for both full Mathlib cases and the reduced reproducer.
Collect a fresh native profile showing that the targeted cost moved as predicted.

Check import correctness, both module modes, and relevant relocation/compacted
region tests. Include collision and dependency-relocation cases if touching
address handling, and snapshot/closure cases if sharing their loader path.
If changing saved addresses or format, regenerate compatible artifacts rather
than treating old cached bytes as equivalent inputs.

Run Linux regression measurements, then an ARM macOS confirmation with its actual
page size recorded. Add other OS versions or bisect Lean only when the evidence
requires them. A Lean bisect must hold a compatible workload constant or rebuild
its artifacts per revision; mismatched nightly caches are not valid comparisons.

The final evidence bundle should include exact reproduction commands, revisions,
patches/build settings, raw paired timings, native profiles, loader diagnostics,
the reduced reproducer, correctness checks, and rejected hypotheses. Success is
a specific operation and trigger with a validated explanation, not just a smaller
macOS/Linux ratio.

## Recommended next implementation

Start with a small native residency/access probe implementing the unchanged-map,
remap, and fresh-process experiments above, using real Lean artifact files.
Use an existing reproducing Mac for the decisive measurements; this workspace is
Linux. Reuse the existing benchmark for end-to-end comparison. Defer reporting,
packaging, and broad CI improvements. The initial C replay and Linux strace
extractor are implemented and exercised locally, including fallback and private
write checks. Next run the recorded sequence on macOS, profile any reproduced
slow phase, and reduce it. See `repro/README.md` for commands and fidelity limits.
No Lean source changes or new CI runs have been made.
