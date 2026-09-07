# debug-import-lean-macos

This project compares the cost of checking minimal legacy and module-system
Mathlib consumers on GitHub-hosted Linux and macOS runners.

The workflow has three runtime jobs:

1. Linux on `ubuntu-24.04` (x86-64)
2. macOS on `macos-15-intel` (x86-64)
3. A Linux report job that compares the two artifacts

The first two are children of one matrix job, so their steps and benchmark command
cannot drift apart. They resolve the `nightly-testing` branch of
[`mathlib4-nightly-testing`](https://github.com/leanprover-community/mathlib4-nightly-testing)
and copy that branch's `lean-toolchain`. This selects the newest Lean nightly for
which Mathlib has a matching revision and cached build artifacts.

## What is measured

Each platform runs two warm-ups per case followed by seven paired iterations
with `LEAN_NUM_THREADS=1`. The order of the two cases alternates each iteration:

```lean
-- Legacy
import Mathlib

-- Module system
module
public import Mathlib
```

The raw JSON contains wall time, user and system CPU time, peak RSS, page faults,
and context-switch counts for every process and both cases. Timed samples run
without tracing overhead. Two additional invocations enable `trace.profiler` and
write Firefox Profiler-compatible `trace-legacy.json` and `trace-module.json`.

The comparison is deliberately described as a hosted-runner comparison. The jobs
use the same architecture and thread count, but the underlying CPUs and
virtualization differ; the report records those details rather than attributing the
entire difference to the operating system.

## Run it

The workflow runs after pushes to `main` and every Monday at 04:17 UTC. You can
also start **Debug Lean import on macOS** manually from the Actions tab. The final
job writes its Markdown table to the GitHub job summary and uploads
`comparison-report`. The `benchmark-linux` and `benchmark-macos` artifacts contain
the raw samples and traces. The report fails if the two jobs did not resolve exactly
the same Lean build, Mathlib commit, source commit, sample counts, and thread count.

For a local Linux measurement:

```console
lake update
lake exe cache get
LEAN_NUM_THREADS=1 python3 scripts/benchmark.py --platform linux
```

Generate a report from two downloaded artifacts with:

```console
python3 scripts/report.py \
  --linux artifacts/benchmark-linux/benchmark.json \
  --macos artifacts/benchmark-macos/benchmark.json \
  --markdown results/report/report.md \
  --json results/report/comparison.json \
  --strict
```
