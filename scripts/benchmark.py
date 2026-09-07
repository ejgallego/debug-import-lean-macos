#!/usr/bin/env python3
"""Measure legacy and module-system `import Mathlib` consumers."""

from __future__ import annotations

import argparse
import json
import os
import platform
import re
import resource
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CASES = (
    {"key": "legacy", "label": "Legacy import", "source": Path("ImportMathlib.lean")},
    {
        "key": "module",
        "label": "Module-system import",
        "source": Path("ImportMathlibModule.lean"),
    },
)


def worker(command: list[str]) -> int:
    """Run one sample in a fresh process so ru_maxrss is per sample."""
    started = time.perf_counter_ns()
    completed = subprocess.run(command, text=True, capture_output=True, check=False)
    elapsed = (time.perf_counter_ns() - started) / 1_000_000_000
    usage = resource.getrusage(resource.RUSAGE_CHILDREN)

    # Linux reports KiB; macOS and the other BSDs report bytes.
    max_rss_bytes = int(usage.ru_maxrss)
    if sys.platform.startswith("linux"):
        max_rss_bytes *= 1024

    print(
        json.dumps(
            {
                "wall_seconds": elapsed,
                "user_cpu_seconds": usage.ru_utime,
                "system_cpu_seconds": usage.ru_stime,
                "cpu_seconds": usage.ru_utime + usage.ru_stime,
                "max_rss_bytes": max_rss_bytes,
                "minor_page_faults": usage.ru_minflt,
                "major_page_faults": usage.ru_majflt,
                "voluntary_context_switches": usage.ru_nvcsw,
                "involuntary_context_switches": usage.ru_nivcsw,
                "exit_code": completed.returncode,
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            separators=(",", ":"),
        )
    )
    return completed.returncode


def run_sample(command: list[str]) -> dict[str, Any]:
    completed = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--worker", *command],
        text=True,
        capture_output=True,
        check=False,
    )
    try:
        sample = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            f"benchmark worker did not return JSON\nstdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        ) from error
    if completed.returncode != 0:
        raise RuntimeError(
            f"command failed with exit code {completed.returncode}: {command!r}\n"
            f"stdout:\n{sample['stdout']}\nstderr:\n{sample['stderr']}"
        )
    return sample


def capture(command: list[str]) -> str:
    return subprocess.run(command, text=True, capture_output=True, check=True).stdout.strip()


def cpu_model() -> str:
    if sys.platform == "darwin":
        return capture(["sysctl", "-n", "machdep.cpu.brand_string"])
    if sys.platform.startswith("linux"):
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.startswith("model name"):
                return line.split(":", 1)[1].strip()
    return platform.processor() or "unknown"


def total_memory_bytes() -> int | None:
    if sys.platform == "darwin":
        return int(capture(["sysctl", "-n", "hw.memsize"]))
    try:
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (ValueError, OSError, AttributeError):
        return None


def lean_commit(version: str) -> str:
    match = re.search(r"\bcommit ([0-9a-f]+)\b", version)
    if match is None:
        raise RuntimeError(f"could not find Lean commit in version string: {version!r}")
    return match.group(1)


def summarize(samples: list[dict[str, Any]], key: str) -> dict[str, float]:
    values = [float(sample[key]) for sample in samples]
    return {
        "min": min(values),
        "median": statistics.median(values),
        "mean": statistics.fmean(values),
        "max": max(values),
        "stdev": statistics.stdev(values) if len(values) > 1 else 0.0,
    }


def command_for(source: Path) -> list[str]:
    return ["lake", "env", "lean", str(source)]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--platform", required=True, dest="platform_label")
    parser.add_argument("--runs", type=int, default=7)
    parser.add_argument("--warmups", type=int, default=2)
    parser.add_argument("--output-root", type=Path, default=Path("results"))
    args = parser.parse_args()

    if args.runs < 1 or args.warmups < 0:
        parser.error("--runs must be positive and --warmups must be non-negative")
    for case in CASES:
        if not case["source"].is_file():
            parser.error(f"missing {case['source']}; run this script from the project root")

    output_dir = args.output_root / args.platform_label
    output_dir.mkdir(parents=True, exist_ok=True)
    metrics = ["wall_seconds", "cpu_seconds", "max_rss_bytes"]
    case_results: dict[str, dict[str, Any]] = {
        case["key"]: {
            "label": case["label"],
            "source": str(case["source"]),
            "command": command_for(case["source"]),
            "samples": [],
        }
        for case in CASES
    }

    for number in range(1, args.warmups + 1):
        order = CASES if number % 2 else tuple(reversed(CASES))
        for case in order:
            sample = run_sample(command_for(case["source"]))
            print(
                f"warmup {number}/{args.warmups} [{case['key']}]: "
                f"{sample['wall_seconds']:.3f} s"
            )

    for number in range(1, args.runs + 1):
        # Alternate order to avoid systematically giving either case the warmer cache.
        order = CASES if number % 2 else tuple(reversed(CASES))
        for case in order:
            sample = run_sample(command_for(case["source"]))
            case_results[case["key"]]["samples"].append(sample)
            print(
                f"sample {number}/{args.runs} [{case['key']}]: "
                f"{sample['wall_seconds']:.3f} s, "
                f"{sample['max_rss_bytes'] / 2**20:.1f} MiB peak RSS"
            )

    for case in CASES:
        case_result = case_results[case["key"]]
        trace_path = (output_dir / f"trace-{case['key']}.json").resolve()
        trace_command = [
            "lake",
            "env",
            "lean",
            "-Dtrace.profiler=true",
            "-Dtrace.profiler.threshold=0",
            f"-Dtrace.profiler.output={trace_path}",
            str(case["source"]),
        ]
        case_result["trace_run"] = run_sample(trace_command)
        if not trace_path.is_file():
            raise RuntimeError(f"Lean succeeded but did not produce {trace_path}")
        case_result["trace_command"] = trace_command
        case_result["trace_file"] = trace_path.name
        case_result["trace_size_bytes"] = trace_path.stat().st_size
        case_result["summary"] = {
            metric: summarize(case_result["samples"], metric) for metric in metrics
        }
        print(f"profiler trace [{case['key']}]: {trace_path} ({trace_path.stat().st_size} bytes)")

    mathlib_dir = Path(".lake/packages/mathlib")
    lean_version = capture(["lake", "env", "lean", "--version"])
    result = {
        "schema_version": 2,
        "benchmark": "import Mathlib: legacy vs module system",
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "platform": {
            "label": args.platform_label,
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
            "cpu_model": cpu_model(),
            "logical_cpu_count": os.cpu_count(),
            "total_memory_bytes": total_memory_bytes(),
            "runner_name": os.environ.get("RUNNER_NAME"),
            "runner_os": os.environ.get("RUNNER_OS"),
            "runner_arch": os.environ.get("RUNNER_ARCH"),
            "image_os": os.environ.get("ImageOS"),
            "image_version": os.environ.get("ImageVersion"),
        },
        "versions": {
            "lean_toolchain": Path("lean-toolchain").read_text().strip(),
            "lean": lean_version,
            "lean_commit": lean_commit(lean_version),
            "lake": capture(["lake", "--version"]),
            "mathlib_revision": capture(["git", "-C", str(mathlib_dir), "rev-parse", "HEAD"]),
            "mathlib_branch": "nightly-testing",
        },
        "github": {
            "repository": os.environ.get("GITHUB_REPOSITORY"),
            "sha": os.environ.get("GITHUB_SHA"),
            "run_id": os.environ.get("GITHUB_RUN_ID"),
            "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
        },
        "method": {
            "warmups_per_case": args.warmups,
            "runs_per_case": args.runs,
            "measurement_order": "legacy,module on odd runs; module,legacy on even runs",
            "lean_num_threads": os.environ.get("LEAN_NUM_THREADS"),
            "timing_clock": "time.perf_counter_ns",
            "resource_api": "getrusage(RUSAGE_CHILDREN) in a fresh worker per sample",
            "cache_state": "warm OS page cache after explicit warmups",
            "tracing": "one separate traced process per case; traces do not affect timed samples",
        },
        "cases": case_results,
    }
    result_path = output_dir / "benchmark.json"
    result_path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(f"benchmark data: {result_path}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--worker":
        if len(sys.argv) == 2:
            raise SystemExit("--worker requires a command")
        raise SystemExit(worker(sys.argv[2:]))
    raise SystemExit(main())
