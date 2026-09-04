# debug-import-lean-macos

This project compares the cost of checking a Lean file whose only command is
`import Mathlib` on GitHub-hosted Linux and macOS runners.

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

Each platform runs two warm-ups followed by seven fresh Lean processes with
`LEAN_NUM_THREADS=1`:

```console
lake env lean ImportMathlib.lean
```

The raw JSON contains wall time, user and system CPU time, peak RSS, page faults,
and context-switch counts for every process. The timed samples run without tracing
overhead. One additional invocation enables `trace.profiler` and writes a Firefox
Profiler-compatible `trace.json`.

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
the same Lean build, Mathlib commit, source commit, sample count, and thread count.

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
