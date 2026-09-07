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

## Direct artifact I/O and phase timers on ARM

The next experiment retains the pinned Lean executable. A small interposed
library times actual artifact calls without changing their arguments. It records
header reads separately from reads following the loader's seek back to zero for
copy fallback. LLDB marks the entry/return of `l_Lean_importModulesCore` and
`l_Lean_finalizeImport`, with ASLR enabled and counter snapshots read directly
from target memory. Phase stops and the extra library remain diagnostic changes;
they may affect cache state and address collisions.

The first attempt,
[run 34130546339](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34130546339)
at source `411bd09`, **failed its phase-measurement validation**. Apple's LLDB
raised a Python autogen-name `KeyError` when a breakpoint callback registered
another callback for the function return. The harness stopped those incomplete
targets and did not report phase durations. The subsequent implementation uses
an explicit synchronous stop/resume loop instead.

The two independent I/O-only processes in that run completed and passed the
capture checks: 37,687 mappings, 3,316,456 header bytes, no descriptor-table
overflow, and 122 rejected mappings matched by 122 seek-to-zero copy fallbacks.
Each copies 30,735,064 bytes. Their direct Lean timings were:

| I/O-only run | Total wall s | mmap s | Header reads s | Copy-fallback reads s | Open s |
|---|---:|---:|---:|---:|---:|
| 1 | 20.495 | 9.182 | 0.502 | 0.089 | 0.932 |
| 2 | 20.189 | 6.814 | 0.564 | 0.099 | 0.918 |

Tracked `fstat` and close calls add about 0.11 s per run. Consequently about
9.7/11.7 s remains outside these tracked calls. That remainder includes other
loading work and finalization; the failed phase measurements cannot divide it.
The fallback read itself is small, but these counters exclude fallback
allocation, rejected-map cleanup, and any induced relocation work.

Uninstrumented warm baselines on that VM range from 17.8 to 27.1 s, so the
I/O-only runs sit within the observed variation; this is not a precise overhead
estimate. The initial uninstrumented import takes 76.6 s with 118,790 major
faults. The two I/O-only runs have 247 and one major fault respectively. These
results again separate high first-use fault counts from slower warm execution.

### Successful loading/finalization split

[Run 34131902959](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34131902959)
at source `b7835ac` passes all nine processes and all phase/capture checks using
the synchronous LLDB loop. All three diagnostic imports have exactly one load
phase followed by one finalization phase, on the same import thread. The load
phase starts with zero counters and accounts for all 37,687 artifact mappings,
all 3,316,456 header bytes, and every tracked artifact open/fstat/close. Finalization
adds zero to every tracked artifact-call counter. This checks that the selected
boundaries frame the intended work.

The following columns are disjoint: `mmap` time is removed from the loading phase
to compute other loading work. They are debugger-run phase measurements, not
uninstrumented end-to-end baselines.

| Diagnostic run | Artifact mmap s | Other loading work s | Finalization s | Sum of import phases s |
|---|---:|---:|---:|---:|
| 1 | 5.948 | 2.203 | 6.854 | 15.005 |
| 2 | 6.023 | 2.318 | 6.203 | 14.544 |
| 3 | 5.987 | 2.092 | 5.112 | 13.191 |

The full loading phase is 8.079–8.340 s. Within it, tracked opens take
0.658–0.714 s, header reads 0.271–0.322 s, copy-fallback reads 0.019–0.067 s,
and fstat plus close about 0.08–0.09 s. About 1.06–1.14 s remains in other
loading activity, which includes pathname operations not covered by the tracked
descriptor calls, import bookkeeping, mapped-object access, and instrumentation.

All three have 122–128 rejected-address fallbacks, copying 30.7–31.1 MB. Fallback
read time is small; allocation, rejected-map cleanup, and induced relocation are
still outside that particular counter. Extra library placement and ASLR can
change collisions, so these are not assertions that instrumented and baseline
address maps are identical.

Across these three diagnostics, mmap varies by only 0.074 s while finalization
varies by 1.743 s. From run 1 to run 3, about 96% of the drop in the sum of import
phase times is in finalization. This locates the observed variation within these
runs, not the cause of all earlier slow warm outliers. Finalization's lack of
artifact syscalls does not rule out faults on already-mapped pages, compression,
anonymous allocation, or other filesystem activity.

Uninstrumented warm baselines on this VM are 18.719, 12.131, and 14.307 s.
I/O-only runs are 11.053 and 13.575 s, with 4.973 and 5.781 s in mmap. The first
uninstrumented import is 57.086 s with 118,795 major faults; warm baselines have
one, zero, and seven major faults. These process and timing differences prevent
a precise diagnostic-overhead estimate. Debugger launch-to-exit times are
14.302–16.486 s; the phase sums exclude startup/exit and some debugger work.
The explicit phase handlers take only 0.9–3.9 ms individually, but stop/resume
transport and memory/cache perturbation are not independently calibrated.

The remaining warm work is now localized: roughly two seconds of loading beyond
mmap, followed by several seconds of environment finalization. The previous
native profiles identify constant tables, extension initialization, imported
extension entries, and persistent marking within that phase. Next measure those
substeps together with per-phase CPU/page-in information to distinguish ordinary
CPU work from access to mapped or compressed pages. A C replay of mmap calls
alone cannot reproduce this finalization access pattern.

## Real Lean phase CPU and faults across Linux and ARM macOS

Successful CI run [34135145113](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34135145113),
source `941f5b9991f173f82d2fbee0e5a01847e627af56`, runs the actual pinned Lean
executable on ARM macOS, ARM Linux, and x86-64 Linux. No C replay timings enter
this comparison. All three jobs pass; all 27 Lean processes exit successfully.
Raw artifacts are `lean-phases-{macos-arm64,linux-arm64,linux-x86_64}`; local
copies are under `results/ci-34135145113/`.

Workload: `ImportMathlibModule.lean` (`module; public import Mathlib`), Lean
`58774429865502f05c63239266aac30ef1e91ef7`, Mathlib
`9d1a52c11563a55a41e0fb61ed0865b384bfdb6d`, `LEAN_NUM_THREADS=1`, ASLR enabled.
The same nine-process order runs on each host: initial import, baseline-1,
io-1, phases-1, phases-2, io-2, baseline-2, phases-3, baseline-3.

The macOS host is macOS 15.7.9 ARM64 with 7 GiB RAM and 16 KiB pages. Linux
hosts use Ubuntu 24.04, kernel 6.17.0-1022-azure, glibc 2.39, approximately
15.6 GiB RAM and 4 KiB pages. ARM Linux removes the architecture mismatch but
not CPU, memory, storage, virtualization, or cache-state differences. These
are host comparisons, not isolated estimates of an OS effect.

### Whole-process controls and phase elapsed times

All values below are medians of three measurements, in seconds. The whole-import
row uses uninstrumented warm controls. Phase rows use separate LLDB diagnostics;
the mmap row is contained within loading, and must not be added to it.

| Measurement | ARM Linux | ARM macOS | x86-64 Linux control |
|---|---:|---:|---:|
| Warm whole import, uninstrumented | 2.918 | 18.122 | 3.453 |
| Loading phase | 0.764 | 10.698 | 1.363 |
| Artifact mmap calls within loading | 0.081 | 6.939 | 0.158 |
| Loading minus artifact mmap | 0.683 | 3.471 | 1.206 |
| Finalization phase | 2.007 | 8.547 | 1.879 |

Individual ARM macOS uninstrumented warm controls are 14.440, 21.846, and
18.122 s, with 433, 3,305, and 795 major faults. ARM Linux controls are 2.918,
2.924, and 2.898 s with zero major faults. The first process takes 64.284 s on
Mac (118,808 major faults) versus 3.102 s on ARM Linux (zero major faults).
Artifact download does not establish equivalent cold caches, so that initial
ratio must not be interpreted as a controlled cold-start comparison.

This batch's warm host ratio is about 6.2x. Real artifact mmap time differs by
about 86x against ARM Linux, confirming that the C reproducer's slow cost class
is present in real Lean. Finalization differs by about 4.3x; mmap alone does not
explain the import gap. Do not subtract diagnostic phase times from separate
uninstrumented runs or infer the attainable speedup of a mapping-policy change.
Medians of differences and differences of medians also need not agree.

### Phase CPU and memory activity

The observer reads the stopped Lean process externally. Linux CPU uses
`/proc/PID/stat` at 10 ms resolution. ARM macOS uses
`proc_pidinfo(PROC_PIDTASKINFO)` and converts Mach time units with
`mach_timebase_info`. Each job checks the units against a 0.3 s process CPU busy
loop before running Lean; all pass. CPU includes all Lean threads, excluding
the observer. The underlying interfaces are described by the
[Linux proc stat manual](https://www.man7.org/linux/man-pages/man5/proc_pid_stat.5.html)
and [XNU's task counter export](https://github.com/apple-oss-distributions/xnu/blob/main/osfmk/kern/bsd_kern.c).

| Host / phase | Median wall s | Median user CPU s | Median kernel CPU s | Median total CPU s | Median wall minus CPU s |
|---|---:|---:|---:|---:|---:|
| ARM Linux / loading | 0.764 | 0.270 | 0.490 | 0.760 | 0.004 |
| ARM macOS / loading | 10.698 | 0.533 | 7.105 | 7.638 | 2.831 |
| ARM Linux / finalization | 2.007 | 1.920 | 0.080 | 2.010 | 0.006 |
| ARM macOS / finalization | 8.547 | 5.727 | 1.137 | 6.719 | 1.828 |

Columns are independently computed medians. Linux CPU quantization can exceed
wall by a few milliseconds. Wall minus CPU includes scheduling, waits, and
debugger transport; it is not a measured disk-wait interval. The key new finding
is that macOS finalization's additional cost includes substantial user CPU and
kernel CPU, as well as an elapsed-time residual. It cannot all be assigned to
waiting for file reads.

| ARM macOS diagnostic | Load wall s | Load page-ins | Finalize wall s | Finalize page-ins | Finalize total CPU s |
|---|---:|---:|---:|---:|---:|
| phases-1 | 9.357 | 168 | 8.547 | 2,098 | 6.719 |
| phases-2 | 12.425 | 1,474 | 8.448 | 14,507 | 6.681 |
| phases-3 | 10.698 | 565 | 10.721 | 6,826 | 7.076 |

All Linux phase measurements have zero major faults. ARM Linux finalization
has approximately 44,000 minor faults; Mac finalization records 234,083–235,178
total faults and zero reported COW faults. Retain the native counter names;
page sizes and fault accounting differ. Mac finalization adds roughly 2.32 GB
resident memory and ARM Linux about 2.14 GB; these deltas are not peaks.
The data do not support attributing the Mac finalization cost to COW faults.
Page-ins occur after repeated imports, but neither their count nor phase elapsed
time has a simple monotonic relationship in these three samples.

All five instrumented processes per host capture exactly 37,687 artifact opens,
stats, header reads, mappings and closes, with 3,316,456 header bytes and
7,303,535,912 requested mapping bytes. All phase entries start with zero artifact
counters; all counts and timed calls occur during loading, with zero increments
during finalization. Each Mac phase process falls back on 122 mappings and reads
30,735,064 bytes. ARM Linux has 3 fallbacks / 358,568 bytes in phases-1 and zero
in phases-2/3; x86-64 Linux has none. Thus finalization page-ins occur without any
of the tracked artifact syscalls. This is compatible with accesses to mapped
memory and other memory activity; it does not identify individual faulting pages.

### Limits and next experiment

LLDB and the injected library can change address layout and memory pressure.
In particular, the debugger shares the Mac runner's 7 GiB RAM and could increase
page-ins. The uninstrumented controls themselves show warm faults and variability,
but the larger diagnostic page-in counts must not be presented as baseline counts
or proof of aggressive reclamation. The passive I/O processes take 15.322 and
15.452 s, within the range of the surrounding uninstrumented controls; this is
not a calibrated overhead estimate.

The next focused experiment should split finalization into constant-table
construction, persistent marking, extension initialization, and imported-entry
processing, using the native profiles to choose boundaries. Prefer a temporary
Lean build with in-process phase resource snapshots to remove LLDB's memory
footprint, paired with an unmodified build on the same machine. Keep first-use
and warm repetitions separate and record host memory pressure. A controlled
intervening-memory workload is still needed to establish cache loss causally.
Continue using C to isolate the mapping-order mechanism, while grounding the
remaining work in these real Lean phases.

## Finalization substeps and named extensions, without LLDB

Successful CI run [34138341689](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34138341689),
source `8997871110f6bfa4f3d90175ee8bdc64125b01e0`, measures real Lean imports on
ARM macOS and ARM Linux with internal wall/CPU/fault snapshots. Both jobs pass,
as do all 32 process/capture validations. The artifacts are
`lean-inner-{macos-arm64,linux-arm64}`; local copies are in
`results/ci-34138341689/`, with raw measurements under each artifact's `inner/`
and source patch/generated C under `rebuild/`.

The first attempt, run `34137725745` at `b7552e8`, completed all Linux checks but
failed to compile the Mac logger: `_POSIX_C_SOURCE` hides Darwin's additional
`rusage` fields. Defining `_DARWIN_C_SOURCE` fixes that portability issue; no
Mac timing from the failed build is used. An earlier local link smoke also
caught a missing `LEAN_EXPORTING` flag, which hid Environment symbols needed by
the interpreter. The committed builder sets it, and real Mathlib imports pass.

### Method and representativeness

This uses the same pinned module-system Mathlib consumer, Lean commit
`58774429865502f05c63239266aac30ef1e91ef7`, Mathlib commit
`9d1a52c11563a55a41e0fb61ed0865b384bfdb6d`, `LEAN_NUM_THREADS=1`, and enabled
ASLR. `Lean.Environment` is rebuilt from the pinned source and linked with the
release archives and pinned C++ shell. The installed artifact files are reused
unchanged. The suite compares the stock release, an uninstrumented relinked
control, and an instrumented relink; it does not substitute a custom import
algorithm or C replay for Lean's frontend.

The instrumented runs use neither LLDB nor the I/O preload library. A bounded
in-process logger buffers monotonic clock and `getrusage(RUSAGE_SELF)` snapshots
until exit. Nested spans have inclusive and exclusive metrics. The summary
checks complete nesting and stage coverage, workload cardinalities, and that
phase CPU/elapsed totals fit within the direct process's `wait4` measurements.
A logging-disabled diagnostic binary is also timed. Separate I/O diagnostics
check artifact calls for all three executable variants.

The Mac runner is Apple M1 (Virtual), macOS 15.7.9, 7 GiB RAM, 16 KiB pages.
ARM Linux is Neoverse-N2, Ubuntu 24.04, kernel 6.17.0-1022-azure, about 15.6 GiB
RAM, 4 KiB pages. Neither hardware nor RAM is matched; ratios describe these
hosts and cannot be assigned to the OS alone.

| Whole import | ARM Linux median s (range) | ARM macOS median s (range) |
|---|---:|---:|
| Stock, three warm controls | 3.035 (3.005–3.054) | 15.005 (12.222–17.477) |
| Uninstrumented relink, three warm controls | 3.098 (3.044–3.144) | 14.015 (13.387–17.203) |
| Instrumented relink, three warm diagnostics | 3.082 (3.034–3.200) | 13.187 (11.906–19.416) |
| Instrumented binary with logging disabled, one run | 3.153 | 12.796 |

The three main variants are order-balanced across warm passes. Linux medians
are within about 2.1%; Mac ranges overlap but are much wider. These controls
support using the diagnostic for attribution, not claiming an improvement or a
precisely calibrated logging overhead. Stock's initial import is 47.300 s on Mac
with 118,962 major faults, versus 3.232 s / zero on Linux. Initial cache state
is not controlled, and subsequent variant-initial runs are retained separately.

### What finalization does, and its measured costs

All numbers below are medians of three warm instrumented runs, in seconds.
Rows are independently aggregated, so the medians need not sum exactly.

| Stage | ARM Linux | ARM macOS | Work performed |
|---|---:|---:|---|
| Prepare modules | 0.031 | 0.209 | Select module/IR data and count incoming constants |
| Private tables | 0.444 | 1.345 | Allocate the three hash tables; populate private constants and name-to-module index; resolve duplicate declarations |
| Public table | 0.176 | 0.267 | Populate the exported constant view |
| Initial extension states | 0.001 | 0.003 | Create initial state slots |
| Assemble environment base | 0.006 | 0.007 | Build environment headers and region lists |
| Imported entries | 0.126 | 0.362 | Arrange extension entries by module for private, IR and server views |
| Persistent marking before extensions | 0.388 | 1.020 | Traverse the existing environment graph and mark new reachable objects persistent |
| Initialize extensions | 0.833 | 2.101 | Invoke each extension's import callback, install its state, run initializers and update registries |
| Persistent marking after extensions | 0.148 | 0.294 | Mark newly constructed extension state reachable from the environment |
| Outside the named stages | 0.009 | 0.416 | Residual includes final wrapping and caller-side temporary-state release; not individually attributed |
| **Outer finalization interval** | **2.162** | **6.007** | Includes the stages and their residual |

Every diagnostic processes 10,690 modules, 643,468 incoming private constant
entries, 643,468 incoming public constant entries, and 544,977 extra IR constant
names. These are input counts, not unique-key counts or proof that the two
constant maps have identical contents. The initial registry has 224 environment
extensions, and 339 named persistent extensions are visited as imported
initializers register further extensions.

The implementation is in the pinned
[`Lean.Environment.finalizeImport`](https://github.com/leanprover/lean4/blob/58774429865502f05c63239266aac30ef1e91ef7/src/Lean/Environment.lean#L2308).
Private-table construction walks constant names and information, populates hash
maps, and checks whether repeated declarations subsume each other. The imported
entries stage builds per-extension arrays indexed by module before callbacks
consume them. Thus finalization rebuilds process-local indices over imported
objects, even when the underlying files are already cached.

[`lean_mark_persistent`](https://github.com/leanprover/lean4/blob/58774429865502f05c63239266aac30ef1e91ef7/src/runtime/object.cpp#L567)
uses an explicit work list, changes reference-counted objects to persistent,
and traverses their pointer fields. Scalars and already persistent objects
stop traversal. The first pass prevents costly subsequent shared/MT marking
when callbacks put the environment in references; the second covers new
extension state. The measured first marking pass is much more expensive than
the second on both hosts. This graph traversal is absent from the mmap-only C
reproducer.

### Which extension callbacks dominate

These are inclusive named-extension spans, contained inside `initialize_extensions`.
They include import callbacks, state installation and registry/attribute updates.

| Extension | ARM Linux s | ARM macOS s | Mac/Linux host ratio |
|---|---:|---:|---:|
| Parser | 0.127 | 0.588 | 4.6x |
| Simplifier | 0.307 | 0.542 | 1.8x |
| Regular initialization attributes | 0.112 | 0.434 | 3.9x |
| Typeclass instances | 0.079 | 0.158 | 2.0x |

Together these four spans account for 81.4–82.1% of Mac extension initialization
and 74.6–75.5% on Linux. Their differing ratios argue against applying a single
blanket multiplier to finalization.

- [Parser imports](https://github.com/leanprover/lean4/blob/58774429865502f05c63239266aac30ef1e91ef7/src/Lean/Parser/Extension.lean#L337)
  convert serialized entries to parser functions and rebuild category/token
  state. Parser descriptions are recursively compiled, and parser constants may
  be evaluated. Median Mac parser time includes 0.395 s user and 0.165 s kernel
  CPU, versus 0.126 s user and 0.001 s kernel on Linux.
- [Simplifier imports](https://github.com/leanprover/lean4/blob/58774429865502f05c63239266aac30ef1e91ef7/src/Lean/Meta/Tactic/Simp/SimpTheorems.lean#L732)
  replay imported simp entries into theorem lookup structures, including
  discrimination trees and lemma-name sets. Typeclass imports likewise rebuild
  [an instance discrimination tree and name map](https://github.com/leanprover/lean4/blob/58774429865502f05c63239266aac30ef1e91ef7/src/Lean/Meta/Instances.lean#L80).
  Their shared [scoped-extension importer](https://github.com/leanprover/lean4/blob/58774429865502f05c63239266aac30ef1e91ef7/src/Lean/ScopedEnvExtension.lean#L61)
  iterates imported entries and calls `addEntry` for global entries.
- The regular-initializer extension runs
  [module `[init]` attributes](https://github.com/leanprover/lean4/blob/58774429865502f05c63239266aac30ef1e91ef7/src/Lean/Compiler/InitAttr.lean#L198).
  Its nested `run_init_attributes` span is 0.397 s on Mac versus 0.089 s on Linux.
  That time is already included in the regular-initializer row above.

### CPU, faults, variation, and remaining uncertainty

| Mac diagnostic | Load s | Finalization s | Finalization CPU s | Finalization major faults | Unassigned finalization s |
|---|---:|---:|---:|---:|---:|
| inner-1 | 6.436 | 6.007 | 5.736 | 190 | 0.475 |
| inner-2 | 10.520 | 8.525 | 8.405 | 8 | 0.416 |
| inner-3 | 6.756 | 4.549 | 4.520 | 189 | 0.061 |

Linux finalization spans are 2.120, 2.162 and 2.272 s, with zero major faults
and almost equal CPU/elapsed totals. Mac's slowest finalization is also almost
entirely CPU time and has the fewest major faults. Multiple stages, including
private tables and persistent marking, slow together. This batch therefore
localizes a substantial warm CPU cost and variability beyond mmap; major
page-ins cannot explain that slow warm diagnostic. CPU time includes memory
stalls while executing and does not establish which hardware/runtime mechanism
causes the host difference.

Whole-process faults need care: in the three Mac diagnostics, 2,832, 372 and
2,831 major faults occur outside the load/finalization intervals. Those may
include native image/startup activity and are not finalization faults. The
relinked binaries change native layout and show different whole-process fault
counts from stock. The phase data prevent charging all those faults to mapped
Mathlib declarations. Earlier LLDB and current internal measurements also use
different hosts/runs and slightly different boundaries, so their difference is
not a measured debugger penalty.

Specifically, the new outer `finalize` timer brackets the call from
`importModules`, and therefore includes caller-side release of its `ImportState`.
The older LLDB function-entry/return timer excludes that release. The generated
C artifact (`rebuild/instrumented.c`) shows `lean_dec_ref` on the import-state
argument immediately after `l_Lean_finalizeImport` returns and before the end
marker. The unassigned Mac interval is largely user CPU (0.060–0.418 s). This
makes temporary-state destruction a concrete next hypothesis, not an established
attribution: directly bracket that decrement and distinguish it from other gaps
between source markers. Linux's unassigned interval is only 8.6–9.1 ms.

The separate I/O checks still see all 37,687 opens, stats, 88-byte reads, mappings
and closes, with 7,303,535,912 requested mapping bytes. Mac stock/control have
122 fallback mappings / 30,735,064 copied bytes; the instrumented I/O run has
125 / 30,959,608. Linux has zero fallbacks. Actual mmap calls take 7.434, 7.455
and 5.285 s in the Mac stock/control/instrumented I/O runs, versus 0.083–0.087 s
on Linux. These are separate processes; do not subtract their mmap timings from
the internally timed load spans.

Next isolate import-state release in the generated C, then sample the parser
callback and private-table construction on the same runner with adjacent
uninstrumented controls. Controlled memory-pressure and per-page evidence remain
necessary before assigning warm cache loss to an aggressive reclamation policy.
The current data identify the real runtime work a more faithful C reduction
would need: constant-name indexing, pointer-rich extension indices, graph
traversal, and temporary-state destruction, in addition to mapping setup.

## Address-level faults and task I/O (September 7, 2026)

Real Lean now has fault addresses, kernel event types on macOS, and per-phase
bytes read from disk and task decompressions. This separates three mechanisms
that aggregate fault counts conflated: first access to file-backed data,
zero-fill allocation, and retrieving compressed anonymous pages. It does **not**
yet explain the remaining CPU cost or establish an aggressive eviction policy.

The macOS and initial Linux experiment is
[run 34146262753](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34146262753),
source `932c9f1`. The corrected Linux address/phase experiment is
[run 34155839886](https://github.com/ejgallego/debug-import-lean-macos/actions/runs/34155839886),
source `7d0afeb`. Same pinned Lean, Mathlib, module consumer and one Lean thread
as the previous experiment. Mac is M1 / 7 GiB / 16 KiB pages; Linux is
Neoverse-N2 / approximately 15.6 GiB / 4 KiB pages. These remain different
hardware and memory configurations, not a controlled pure-OS comparison.
Raw local downloads are `results/ci-34146262753/{macos,linux}/` and
`results/ci-34155839886/linux/`; artifacts retain rebuild hashes and commands.

### Capture and validation

- macOS `ktrace trace -t -N -p lean -f S0x0130` supplies VM-fault entry/return
  events, address and outcome. The tracer runs separately; Lean runs as the
  ordinary runner user. We decode only the PID recorded by the target logger.
- Linux `perf record --clockid mono -e minor-faults,major-faults -c 1 -d`
  captures every requested fault event, including its address. The first Linux
  run omitted the explicit clock: **do not use that run for fault-to-phase
  attribution**. Its timing/I/O snapshots remain useful. The corrected run
  explicitly matches `CLOCK_MONOTONIC` and validates against phase counters.
- All four accepted traces have no reported lost events or unmatched pairs.
  Each trace's load/finalize event counts match its target `getrusage` total
  exactly. All Linux inner stages match exactly; the Mac final marking stage
  differs by one event at the snapshot boundary. Mac traces contain 290,368
  and 286,726 target faults; corrected Linux traces contain 96,968 and 95,165.
- Each traced process still opens/stats/header-reads/maps/closes 37,687 artifacts,
  requesting 7,303,535,912 mapping bytes. Accepted artifact ranges are recorded
  by the existing passive interposer. Mac fallback counts are 122/125; Linux 0.
- Other address classes use an endpoint mapping census and native image ranges.
  This is **not a mapping-lifetime trace**: temporary allocations may already
  have disappeared. Mac leaves 54,169/78,480 addresses unclassified, mostly
  zero-fill faults during extension initialization. Their kernel fault types
  are known even when their endpoint mapping is absent.
- A separate `artifact-range-before-mmap-return` category retains faults that
  match a subsequently accepted artifact mapping but precede its recorded
  completion time. On Mac these are within about 6 microseconds of completion;
  clock-pair skew and faults inside mmap cannot be distinguished here. Do not
  silently relabel these as unrelated file mappings. This ambiguity does not
  affect the private-table or extension findings below.

The original probe run `34145095798` established that macOS tracing works on
this CI runner, which reports **SIP disabled**. This does not establish tracing
availability on a normally configured managed Mac. Its Linux artifact upload failed because `perf.data` was root-owned;
subsequent runs fix readability and preserve both platforms' probe evidence.

### Untraced I/O and compression

Per-phase macOS disk bytes are `proc_pid_rusage(..., RUSAGE_INFO_V4)`
`ri_diskio_bytesread`; compression snapshots use `TASK_VM_INFO`, including the
**target task's** cumulative decompression count. Linux reads `/proc/self/io`
`read_bytes`. Unsupported Linux compression fields are explicitly marked as
unsupported, not interpreted as measured zeros. Memory snapshots are enabled
only at coarse phase boundaries, not at every named extension. The phase
snapshots themselves use no tracer or preload library.

| Platform / run | Load s | Load disk MiB | Finalize s | Finalize CPU s | Finalize disk MiB | Task decompressions in finalize |
|---|---:|---:|---:|---:|---:|---:|
| Mac first measured | 26.819 | 579.305 | 42.433 | 16.704 | 1790.406 | 9,894 |
| Mac repeat 1 | 13.579 | 71.957 | 9.126 | 8.721 | 0.516 | 3,766 |
| Mac repeat 2 | 10.732 | 70.008 | 10.104 | 9.563 | 0.219 | 6,973 |
| Mac repeat 3 | 11.509 | 67.992 | 6.534 | 6.498 | 0.309 | 1 |
| Linux repeat 1 | 0.732 | 0 | 2.202 | 2.201 | 0 | unavailable |
| Linux repeat 2 | 0.755 | 0 | 2.114 | 2.114 | 0 | unavailable |
| Linux repeat 3 | 0.743 | 0 | 2.083 | 2.082 | 0 | unavailable |

The Mac first measured import has substantially more actual disk activity and
non-CPU elapsed time than repeats. This is stronger evidence than a major-fault
count, but its cache state was not controlled. All measured Linux load/finalize
spans, including its first run, report zero disk-read bytes.

Warm Mac finalization can still take **6.534 s with 6.498 s CPU, 316 KiB read
and one decompression**. Storage/decompression therefore cannot account for the
whole warm gap. Conversely, warm Mac loading has zero reported major faults yet
68–72 MiB charged disk reads. Disk-byte accounting also includes explicit reads
and other charged I/O; it is not a measurement of artifact mmap reads alone.
There is no valid conversion from the fault counter to physical bytes read.

### What the addresses and fault types show

| Phase / evidence | Mac trace 1 | Mac trace 2 | Linux trace 1 | Linux trace 2 |
|---|---:|---:|---:|---:|
| Private tables: artifact-address faults | 75,533 | 75,532 | 13,735 | 13,735 |
| Private tables: all faults | 81,390 | 81,077 | 13,832 | 13,832 |
| Extension initialization: zero-fill faults | 90,392 | 90,383 | type unavailable | type unavailable |
| Extension initialization: artifact-address faults | 7,296 | 7,296 | 1,465 | 1,465 |
| Extension initialization: all faults | 99,274 | 98,873 | 5,680 | 3,914 |

Private-table construction is primarily faulting on imported artifacts.
Extension initialization's huge Mac count is primarily **zero-fill**, not
rereading artifact pages. The Mac extension zero-fill handlers total only
0.160/0.236 s entry-to-return elapsed time, out of 2.106/2.278 s for that phase.
The finalization compressor handlers total 0.108/0.071 s. These durations include
preemption and are not CPU cycles or an estimate of time an optimization would
save. However, they show that a dramatic event count does not imply a dramatic
fault-handler contribution in these captures.

All traced finalization VM-fault handlers total 4.709/1.126 s, versus phase times
11.606/7.945 s. File-page-in events account for 3.940/0.442 s of those totals.
Their variation accompanies 171.098/25.098 MiB charged reads. Even the lighter
trace leaves most finalization time outside fault handlers. Meanwhile actual
artifact mmap calls still take 11.683/10.185 s on Mac, versus 0.112/0.107 s on
Linux, in these same traced processes.

**Tracing perturbs this memory-constrained workload.** Mac traced finalization
reads 25–171 MiB versus 0.2–0.5 MiB in adjacent untraced repeats. The 128 MiB
kernel trace buffer and tracing process compete for RAM. Plain relinked imports
also vary considerably (31.635/26.674 s versus 18.344–22.979 s for memory-enabled
repeats); this batch cannot estimate a clean instrumentation overhead percentage.
Use untraced runs for timing claims and traces for the observed mechanism mix;
do not transplant traced fault proportions onto untraced timings.

### Why the counter ratios still need care, and the next experiment

[XNU's resource accounting](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/bsd/kern/kern_resource.c)
reports task faults minus page-ins as minor faults and task page-ins as major
faults. Its [VM fault path](https://github.com/apple-oss-distributions/xnu/blob/xnu-11417.140.69/osfmk/vm/vm_fault.c)
can count a first mapping of a prefetched clustered page as a page-in even on a
cache hit. Linux [getrusage](https://man7.org/linux/man-pages/man2/getrusage.2.html)
distinguishes faults by whether servicing them required I/O. The counts are
useful within each platform, but are not interchangeable units across platforms.

Linux's [fault-around implementation](https://github.com/torvalds/linux/blob/v6.17/mm/memory.c)
can establish nearby file-page mappings on one fault. Its
[transparent huge pages](https://www.kernel.org/doc/html/latest/admin-guide/mm/transhuge.html)
can likewise satisfy a larger anonymous range with one allocation fault. The
Linux host's `thp_fault_alloc` rises by exactly 202 in each untraced repeat here
(195/199 in the corrected traces). That is host evidence, not yet a per-Lean
measurement or a causal explanation. Allocator reuse/purge differences are
another candidate; the pinned Lean source selects mimalloc v3.4.4.

Next compare Linux default versus **process-only THP disabled**, preserving
identical Lean artifacts and phase markers, and capture the target's anonymous
huge-page usage plus allocator reserve/commit/purge activity. On Mac, trace those
allocator operations and temporary allocation lifetimes to explain the zero-fill
volume. In parallel as a research direction, sample the remaining CPU-heavy
private-table/parser work and isolate `ImportState` release. Keep the already
established mmap address-order problem separate: these fault results do not
explain away its mapping-setup cost.
