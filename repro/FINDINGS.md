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
mmap bottleneck that could account for much of the C replay's mapping phase; it does
not yet explain all of Lean's import time or prove the earlier reclamation cause.

The artifact-order comparison is implemented by `--suite order` in
`scripts/run-mmap-experiment.py` and is now the CI workflow's default. It changes
mapping creation order while retaining the full artifact list, per-file loader
operations, sizes, requested addresses, protections, and platform flags. It omits
synthetic page passes to avoid the small ARM runner's working-set confound.
Linux locally passes both orders with no fallback; permutation validation,
checksum preservation with page passes enabled, and address-collision fallback
have also been checked. Neither order yet models Lean's interleaved pointer
accesses, relocation writes, or heap activity.

## Reconnecting the reduction to real Lean artifacts

[Artifact-order CI run 34119198396](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34119198396)
completed successfully on all three hosts at source `23b554e1573b8dfbcc4797aac97a11b8334b3762`.
Lean and Mathlib remain pinned to `58774429865502f05c63239266aac30ef1e91ef7`
and `9d1a52c11563a55a41e0fb61ed0865b384bfdb6d`. Every case requests 37,687
artifact mappings totaling 7,303,535,912 file bytes. The inventory, descending
permutation, source hash, process exits, and absence of page-pass records were
checked in the downloaded artifacts. A separate local Linux syscall comparison
confirmed that captured-order C requests match Lean's entire artifact mmap
sequence, and descending order preserves the multiset of requests.

Median mapping-setup seconds across two fresh processes per order, each with two
map/unmap cycles (four observations, not four independent processes):

| Host | Captured order | Descending saved-address order | Ratio |
|---|---:|---:|---:|
| Linux | 0.169 | 0.164 | 1.04x |
| Intel macOS | 8.494 | 1.068 | 7.95x |
| ARM macOS | 6.582 | 0.705 | 9.34x |

Each host first runs a real Lean import. C process order is captured/descending,
then descending/captured. Headers are read once before the timed processes to
prepare the permutation. The measured map phase includes open, stat, header read,
mmap, rejected-address cleanup, copy fallback, and close; it is not isolated
mmap syscall time. All mapping phases report zero major faults. This does not
establish an absence of filesystem I/O or all page faults, especially on macOS.

Intel has exactly six rejected-address fallbacks and 1,247,544 fallback bytes in
every cycle of both orders. ARM has 122 fallbacks and 30,735,064 bytes in captured
order; descending has 122 or 125 fallbacks and 30,735,064 or 30,959,608 bytes.
Linux has none. Thus requests are identical, but accepted mappings are not
guaranteed identical, particularly on ARM. Per-file outcomes are not yet logged.
Even the ARM descending process with matching aggregate fallback counts takes
only 0.659/0.724 s per map phase. The slight fallback variation does not accompany
the disappearance of the order penalty.

The Intel map-phase median system CPU falls from 8.401 to 1.007 s; ARM falls from
5.370 to 0.639 s. Individual captured-order map times range from 7.775–10.781 s on
Intel and 4.941–8.652 s on ARM, so the small sample supports a large directional
effect rather than a precise speedup estimate. Lean reference process wall times
are 3.070/27.571/40.063 s on Linux/Intel/ARM. Those are single reference runs,
not baseline/candidate Lean measurements; the C ratios are not Lean speedups.

The small reduction now predicts a substantial mapping-setup penalty with Lean's
real artifact requests. It strengthens the insertion-order diagnosis and the
candidate XNU hole-search explanation. It still does not directly identify the
running kernel's internal path, reproduce Lean's complete access trace, or
establish the cause of the original warm-page reclamation behavior. Next capture
actual macOS loader outcomes and access/relocation interleaving before extending
the C model. A descending-order Lean loader is not yet implemented or validated.

## Actual Lean profiling on ARM: first import versus repeated imports

Following the decision to focus on ARM macOS, the ARM-only
[native profiling run 34126032890](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34126032890)
at source `1853e1fb8fbafc3e5970febe53351dddb1918da7` profiles the actual Lean
executable. The source remains `ImportMathlibModule.lean`, using the same pinned
Lean and Mathlib. The runner is an Apple M1 virtual machine with 7 GiB RAM,
16 KiB pages, and macOS 15.7.9. All imports and the sampler succeeded.

| Run | Wall s | User CPU s | System CPU s | Major faults |
|---|---:|---:|---:|---:|
| First import, unprofiled | 43.785 | 5.743 | 8.996 | 118,553 |
| Repeated import 1 | 10.500 | 3.878 | 5.905 | 74 |
| Repeated import 2 | 9.868 | 3.679 | 5.939 | 197 |
| Separate native-sampled import | 11.004 | 3.864 | 6.340 | 234 |
| Repeated import 3 | 9.908 | 3.611 | 6.063 | 8 |

These are direct Lean process measurements using `wait4`, excluding Lake and the
sampling/monitoring tools. The initial cache state is whatever artifact download
left behind; no purge or controlled cold-cache preparation was performed. Three
repeated imports have a 9.908 s median, substantially below the earlier single
40-second reference. The first import has approximately 29.0 seconds of wall
time beyond total CPU time, compared with 0.23–0.72 s on repeated runs. This
indicates waiting/descheduling; it is not a direct measurement of I/O wait.

Host-wide `vm_stat` page-ins increase by 129,500 during the first import versus
1,583–3,076 during each unprofiled repeated import. First-import compression and
decompression deltas are 107,453 and 24,200 pages. No swap-in/out events occur.
Together with Lean's major-fault collapse, these measurements strongly implicate
first-use page availability in the large initial penalty, but do not prove an
overly aggressive reclamation policy. The repeated module imports here do not
reproduce a persistent 40-second warm slowdown.

### Warm import native attribution

The sampled PID is the direct Lean child. Symbols resolve through the pinned
`libleanshared.dylib`; attachment begins about 0.2 s after launch. The sampler
finishes normally when Lean exits. Diagnostic wall time is about 11% above the
unprofiled median, so percentages below are approximate attribution, not exact
baseline seconds or CPU percentages.

The importing worker has 7,109 stack observations. Three other threads spend
almost all their observations waiting for this worker or the event loop. They
must not be added to the import denominator. The reusable
`scripts/summarize-lean-sample.py` checks tree-count conservation and computes
disjoint categories on the thread containing the most `importModules` samples:

| Worker stack category | Samples | Share |
|---|---:|---:|
| Artifact `mmap` | 2,969 | 41.8% |
| Initialize persistent extensions | 1,288 | 18.1% |
| Other `finalizeImport`, including constant tables | 1,140 | 16.0% |
| Mark persistent | 709 | 10.0% |
| Filesystem calls outside `finalizeImport` | 522 | 7.3% |
| Load extension entries | 296 | 4.2% |
| Compacted-region reader | 31 | 0.4% |
| Other work and waits | 154 | 2.2% |

Thus roughly 48% is environment construction after loading, including extension
initialization, constant maps, and marking. The compacted-region reader's small
warm share gives no support for treating pointer relocation as the dominant
remaining warm cost in this run. Ordinary memory faults can appear at the
faulting user instruction in a stack sample, so these categories do not separate
algorithmic CPU work from memory stalls within each operation. Internal kernel
attribution remains unavailable.

### First-import native profile and warm variability on a second runner

[Follow-up run 34127346438](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34127346438)
at source `190a52b` profiles both the initial and warm imports on another fresh
ARM runner, with identical pinned inputs. Both sampler PIDs match their direct
Lean children, attachment begins within 0.23 s of launch, all symbols resolve
well enough to identify the import worker, and all processes succeed.

| Run | Wall s | User CPU s | System CPU s | Major faults |
|---|---:|---:|---:|---:|
| Initial import, sampled | 61.539 | 8.024 | 15.343 | 118,771 |
| Repeated import 1 | 11.871 | 4.246 | 6.772 | 265 |
| Repeated import 2 | 27.724 | 9.395 | 13.099 | 318 |
| Separate warm sampled import | 19.708 | 5.906 | 9.559 | 1 |
| Repeated import 3 | 12.743 | 4.754 | 6.505 | 23 |

The first import again has about 119,000 major faults and considerable wall time
beyond CPU time (38.2 s). Its import worker has 41,378 observations:

| Worker stack category | Initial import | Warm import on same runner |
|---|---:|---:|
| Other `finalizeImport`, including constant tables | 30.6% | 13.5% |
| Filesystem calls outside `finalizeImport` | 24.3% | 9.3% |
| Artifact `mmap` | 13.2% | 48.6% |
| Mark persistent | 11.0% | 8.3% |
| Load extension entries | 7.7% | 3.2% |
| Initialize persistent extensions | 7.4% | 12.6% |
| Compacted-region reader | 0.3% | 1.0% |
| Other work and waits | 5.5% | 3.5% |

First-import `read` leaves account for 7,246 observations, almost all under
`lean_compacted_region_read`. The profile does not directly distinguish header
reads from full-file fallback reads. Other first-import hot leaves are the
constant-table loop `Lean_finalizeImport_spec__6` (7,973), `lean_mark_persistent`
(4,539), and the extension-entry loop (2,542). The constant/extension categories
include memory access to mapped data as well as ordinary CPU work; do not
interpret them as pure algorithmic overhead. Around 57% of initial observations
are under environment construction, versus 38% in this runner's warm profile.
The large first-import penalty therefore reaches well beyond the mmap syscall.

The initial sampled time cannot be compared directly with the previous runner's
unprofiled first time to estimate profiler overhead: these are different VMs and
initial cache states. The second runner also has a real unprofiled warm outlier
at 27.7 s. Its major faults remain low, while user and system CPU roughly double
relative to its first repeated import. Host-wide compression/decompression and
page-ins increase, but are not specific to Lean. These observations leave
runner/memory-state variability unresolved; major file faults alone do not
explain every slow warm run. No kernel reclamation policy has been established.

### Consequences for the next experiment

Keep first-use and repeated-import results separate. Across these profiles,
mapping calls account for roughly 42–49% of warm worker observations and
environment construction for roughly 38–48%. The first-use penalty has high
major-fault counts and appears in loader reads and later imported-data access.
The earlier single 40-second reference must not be treated as the steady warm
baseline or have an unrelated C mapping time subtracted from it.

The next C fidelity improvement should preserve Lean's sparse, interleaved page
accesses and finalization access order, rather than sweeping every mapped page.
First collect actual loader mapping/fallback timings and phase boundaries to
distinguish the warm outlier's mapping cost from finalization cost. Reproduce
cache loss under a controlled intervening workload before claiming aggressive
warm-page eviction. These ARM results concern the module-system consumer;
legacy imports and the original laptop conditions still need their own checks.
