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

## Minimal mapping experiment: reproduced on both Macs

[CI run 34116087360](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34116087360),
source `54140ee`. All three jobs and all 36 cases per platform succeeded.
Every nonzero address hint was honored; there are no relocation or copy-fallback
paths in this program. Darwin version is 24.6.0 on both Macs; page sizes are
4 KiB on Intel/Linux and 16 KiB on ARM. Downloaded artifacts are under
`results/ci-34116087360/`.

Median map seconds for 32,768 mappings (two fresh processes per case):

| Backing and insertion pattern | Linux | Intel macOS | ARM macOS |
|---|---:|---:|---:|
| One file, kernel-selected addresses | 0.053753 | 0.092620 | 0.041906 |
| One file, ascending addresses, 1 GiB spacing | 0.055870 | 5.461600 | 3.005501 |
| One file, descending addresses, 1 GiB spacing | 0.045009 | 0.100817 | 0.049543 |
| One file, shuffled addresses, 1 GiB spacing | 0.063226 | 7.698875 | 3.665995 |
| One file, shuffled addresses, 64 KiB spacing | 0.065372 | 7.742154 | 3.699847 |
| Anonymous, ascending addresses, 1 GiB spacing | 0.052393 | 5.352758 | 3.343732 |
| Anonymous, shuffled addresses, 1 GiB spacing | 0.054545 | 6.770050 | 3.791880 |

For the file-backed ascending/descending/shuffled cases, the final requested
address set, mapping sizes, backing object, offset, flags, and protections are
identical. Only insertion order changes. There are no loads/stores through the
mapped addresses. The shuffled/descending ratio is approximately 76x on Intel
and 74x on ARM. Near-identical shuffled times at 64 KiB and 1 GiB spacing show
that enormous address gaps are not required. Anonymous results show that the
penalty also exists without file backing.

The count sweep grows much faster than linearly on macOS. For example, shuffled
file-backed mappings at 1 GiB spacing take 0.041/1.151/7.699 s on Intel for
4,096/16,384/32,768 mappings, and 0.019/0.744/3.666 s on ARM. In the first
32,768-mapping Intel shuffled run, a 1,024-call batch grows from 0.0063 s at the
start to 0.4844 s at the end. The descending run's corresponding batches are
0.00246 and 0.00248 s. Linux batches remain roughly constant. Timing noise and
cache effects mean these samples should not be fitted to an exact exponent.

### Candidate XNU mechanism

The preceding replay's sample reports kernel library version
`11417.140.69.711.44`. The corresponding public source release
[`xnu-11417.140.69`](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/vm/vm_map_store.c#L428)
contains `vm_map_store_find_space_forward`. When `map->holelistenabled` is true,
it starts at `map->holes_list` and follows `vme_next` until the free range extends
past the requested start address. Ascending insertion with gaps makes that search
cross a growing number of holes; descending insertion can keep using the first
large low-address hole. Shuffled insertion also requires progressively longer
searches. That predicts accumulated quadratic traversal work, with additional
cache/locality effects possible.

This is a source-supported explanation consistent with all the controls, not
direct observation of the running kernel's internal stacks or configuration.
The public release is not the exact patched binary on the runner. A kernel trace
or a controlled kernel change would establish attribution more firmly.

### Next discriminating check

Connect the reduction back to the original artifact replay: compare its mapping
phase in captured order versus descending saved-address order, preserving files,
sizes, hints, and flags, and initially skip page access. This is valid for the C
byte replay but cannot simply be applied to Lean's dependency-aware loader.
Alternatively, test a safe exact-address reservation primitive in the small
program to bypass hint searching, after checking collision semantics. Neither
experiment should overwrite occupied mappings.

Keep the original warm-page problem separate: this experiment establishes a
mapping-order cost without accessing data pages. It explains a concrete macOS
mmap bottleneck and a substantial part of the C replay's mapping phase; it does
not yet explain all of Lean's import time or prove the earlier reclamation cause.
