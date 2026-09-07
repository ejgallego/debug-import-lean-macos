#!/usr/bin/env python3
"""Compare two import styles across Linux and macOS benchmark results."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable


CASES = (("legacy", "Legacy import"), ("module", "Module system"))
METRICS: tuple[tuple[str, str, Callable[[float], str]], ...]


def seconds(value: float) -> str:
    return f"{value:.3f} s"


def mebibytes(value: float) -> str:
    return f"{value / 2**20:.1f} MiB"


METRICS = (
    ("wall_seconds", "Wall time", seconds),
    ("cpu_seconds", "User + system CPU", seconds),
    ("max_rss_bytes", "Peak RSS", mebibytes),
)


def load(path: Path) -> dict[str, Any]:
    with path.open() as stream:
        data = json.load(stream)
    if data.get("schema_version") != 2:
        raise ValueError(f"unsupported schema in {path}; expected schema version 2")
    if set(data.get("cases", {})) != {key for key, _ in CASES}:
        raise ValueError(f"missing benchmark cases in {path}")
    return data


def median(data: dict[str, Any], case: str, metric: str) -> float:
    return float(data["cases"][case]["summary"][metric]["median"])


def gibibytes(value: int | None) -> str:
    return "unknown" if value is None else f"{value / 2**30:.1f} GiB"


def short_revision(value: str) -> str:
    return value[:12]


def ratio(numerator: float, denominator: float) -> float | None:
    return numerator / denominator if denominator else None


def ratio_text(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}×"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--linux", type=Path, required=True)
    parser.add_argument("--macos", type=Path, required=True)
    parser.add_argument("--markdown", type=Path, required=True)
    parser.add_argument("--json", type=Path, required=True, dest="json_output")
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()

    linux = load(args.linux)
    macos = load(args.macos)
    checks = {
        "Lean toolchain": (
            linux["versions"]["lean_toolchain"],
            macos["versions"]["lean_toolchain"],
        ),
        # Full `lean --version` output contains an OS-specific target triple.
        "Lean commit": (
            linux["versions"]["lean_commit"],
            macos["versions"]["lean_commit"],
        ),
        "Mathlib revision": (
            linux["versions"]["mathlib_revision"],
            macos["versions"]["mathlib_revision"],
        ),
        "GitHub revision": (linux["github"]["sha"], macos["github"]["sha"]),
        "runs per case": (
            linux["method"]["runs_per_case"],
            macos["method"]["runs_per_case"],
        ),
        "warmups per case": (
            linux["method"]["warmups_per_case"],
            macos["method"]["warmups_per_case"],
        ),
        "Lean threads": (
            linux["method"]["lean_num_threads"],
            macos["method"]["lean_num_threads"],
        ),
    }
    mismatches = {name: values for name, values in checks.items() if values[0] != values[1]}
    comparable = not mismatches

    cross_platform: dict[str, Any] = {}
    cross_rows: list[str] = []
    for case_key, case_label in CASES:
        cross_platform[case_key] = {}
        for metric_key, metric_label, formatter in METRICS:
            linux_value = median(linux, case_key, metric_key)
            macos_value = median(macos, case_key, metric_key)
            observed_ratio = ratio(macos_value, linux_value)
            cross_platform[case_key][metric_key] = {
                "linux_median": linux_value,
                "macos_median": macos_value,
                "macos_over_linux": observed_ratio,
            }
            cross_rows.append(
                f"| {case_label} | {metric_label} | {formatter(linux_value)} | "
                f"{formatter(macos_value)} | {ratio_text(observed_ratio)} |"
            )

    module_delta: dict[str, Any] = {"linux": {}, "macos": {}}
    delta_rows: list[str] = []
    for metric_key, metric_label, _ in METRICS:
        linux_ratio = ratio(median(linux, "module", metric_key), median(linux, "legacy", metric_key))
        macos_ratio = ratio(median(macos, "module", metric_key), median(macos, "legacy", metric_key))
        module_delta["linux"][metric_key] = linux_ratio
        module_delta["macos"][metric_key] = macos_ratio
        delta_rows.append(f"| {metric_label} | {ratio_text(linux_ratio)} | {ratio_text(macos_ratio)} |")

    def environment_row(data: dict[str, Any]) -> str:
        info = data["platform"]
        image = "/".join(value for value in [info.get("image_os"), info.get("image_version")] if value)
        return (
            f"| {info['label']} | {info['system']} {info['release']} | {info['machine']} | "
            f"{info['cpu_model']} | {info['logical_cpu_count']} | "
            f"{gibibytes(info['total_memory_bytes'])} | {image or 'unknown'} |"
        )

    if comparable:
        status = "✅ Both jobs used the same Lean build, Mathlib revision, source revision, run counts, and thread count."
    else:
        details = "; ".join(f"{name}: {left!r} vs {right!r}" for name, (left, right) in mismatches.items())
        status = f"❌ Results are not directly comparable: {details}."

    markdown = "\n".join(
        [
            "# `import Mathlib`: Linux vs macOS",
            "",
            status,
            "",
            "## Median warm-import cost",
            "",
            "| Import style | Metric | Linux | macOS | macOS / Linux |",
            "|---|---|---:|---:|---:|",
            *cross_rows,
            "",
            "## Module-system delta",
            "",
            "Ratios below are module-system / legacy within the same platform; lower is better.",
            "",
            "| Metric | Linux | macOS |",
            "|---|---:|---:|",
            *delta_rows,
            "",
            f"Each case is the median of {linux['method']['runs_per_case']} measured processes after "
            f"{linux['method']['warmups_per_case']} warm-ups per case, with "
            f"`LEAN_NUM_THREADS={linux['method']['lean_num_threads']}`. Case order alternates each iteration. "
            "Timing samples do not enable tracing; each platform performs one separate traced run per case.",
            "",
            "## Inputs",
            "",
            f"- Lean: `{linux['versions']['lean_toolchain']}`",
            f"- Mathlib: `{short_revision(linux['versions']['mathlib_revision'])}` from `nightly-testing`",
            f"- Source: `{short_revision(linux['github']['sha'] or 'local')}`",
            "- Legacy source: `import Mathlib`",
            "- Module-system source: `module` followed by `public import Mathlib`",
            "",
            "## Runner environments",
            "",
            "| Platform | OS | Architecture | CPU | Logical CPUs | RAM | Runner image |",
            "|---|---|---|---|---:|---:|---|",
            environment_row(linux),
            environment_row(macos),
            "",
            "## Interpretation",
            "",
            "This compares the end-to-end process cost of checking minimal Mathlib consumers on GitHub-hosted "
            "x86-64 runners. It includes Lean/Lake startup and loading cached Mathlib artifacts. Because runner "
            "hardware and virtualization differ, macOS / Linux is an observed runner ratio, not an OS-only effect. "
            "The within-platform module-system / legacy ratio is the cleaner comparison of import modes. Download "
            "each platform artifact for raw samples and the two Firefox Profiler-compatible traces.",
            "",
        ]
    )

    report = {
        "schema_version": 2,
        "comparable": comparable,
        "mismatches": mismatches,
        "cross_platform": cross_platform,
        "module_system_delta": module_delta,
        "linux": str(args.linux),
        "macos": str(args.macos),
    }
    args.markdown.parent.mkdir(parents=True, exist_ok=True)
    args.json_output.parent.mkdir(parents=True, exist_ok=True)
    args.markdown.write_text(markdown)
    args.json_output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n")
    print(markdown)
    return 2 if args.strict and not comparable else 0


if __name__ == "__main__":
    raise SystemExit(main())
