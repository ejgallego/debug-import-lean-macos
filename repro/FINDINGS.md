# Experiment notes

## Artifact replay, 2026-09-07

[CI run 34109883721](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34109883721),
source `ad52cf7`, Lean `58774429865502f05c63239266aac30ef1e91ef7`, Mathlib
`9d1a52c11563a55a41e0fb61ed0865b384bfdb6d`. Raw downloaded artifacts are under
`results/ci-34109883721/`. The original replay maps 37,687 artifacts totaling
7,303,535,912 file bytes; mapped page rounding adds to that total.

### Intel macOS: address-dependent mapping cost

All timed processes succeeded. Across two processes and two cycles each, the
median map phase was 12.481 s with saved addresses and 1.952 s with kernel-chosen
addresses. Linux control medians were 0.353 and 0.351 s respectively. These phase
times include opens, header reads, closes, and fallback copies as well as mmap.
Saved-address Intel runs consistently had six wrong-address mappings and copied
1,247,544 bytes; kernel-chosen runs had no fallback.

The separate five-second Intel sample caught the mapping phase: 2,859 of 3,298
sampled stacks ended at `__mmap`, versus 356 at `__open`. It does not identify
the internal kernel function. Later residency probes reported all mapped pages
resident, with no observed losses. Repeated accesses eventually became cheap.
The first saved-address process had an 81.6 s initial access pass with many major
faults; do not treat that initial state as equivalent to later warmed processes.

### ARM macOS: repeated page-ins with a working set larger than available RAM

The actual runner had 7 GiB RAM and 16 KiB pages. All requested files existed and
Lean itself completed in 68.5 s. Four timed C processes exceeded the 300 s limit;
one kernel-chosen process completed in 256.3 s. The job failed because of those
timeouts, not because C failed to compile or the artifacts were unavailable.

Repeated access passes took roughly 40–60 s. In the residency run, only about
236,000–243,000 of 465,982 mapped pages were resident at checkpoints. The first
pass showed 65,012 resident-to-nonresident transitions; later passes incurred
about 465,984 reported major faults each. A zero loss count between some later
checkpoints does not imply no eviction: pages can be evicted and recovered within
the intervening full scan. The VM counters also show substantial page-in activity.

This is consistent with working-set thrashing. The full mapping set alone exceeds
the runner's RAM after rounding, before OS and other memory usage. It does not
establish overly aggressive reclamation when memory is sufficient. Increasing
the timeout would not resolve that confounder.

## Next reduction: mapping without bulk data or page access

`mmap-hints.c` removes Lean, artifact dependencies, per-file opens/header reads,
bulk storage I/O, pointer relocation, and all accesses to mapped pages. It maps
one page of one temporary file at offset zero repeatedly, retaining mappings until
the map loop finishes. Every platform uses ordinary `MAP_PRIVATE` hints with
read/write protection, without fixed-address flags or fallback copying. It also
offers anonymous backing as a control.

The same address set is inserted ascending, descending, or in a fixed shuffled
order. Compare 64 KiB and 1 GiB spacing, and kernel-selected placement. Require
all hinted mappings to land exactly as requested; nonzero exits flag invalid
conditions. Timings every 1,024 calls help distinguish constant cost per mapping
from cost that grows with accumulated mappings. Output occurs after the map loop.

The CI matrix tests 4,096, 16,384, and 32,768 mappings, with reversed case order on
the second repetition. Anonymous controls run at the largest count. Kernel-chosen
anonymous mappings may coalesce; call count is not necessarily VM entry count.

If this reproduces the address penalty, we have a small VM-map bookkeeping
reproducer. If it stays fast, restore one removed property at a time, starting
with multiple backing files or the exact saved-address distribution. Either
result is separate from establishing the original warm-page reclamation cause.
