#!/usr/bin/env python3
"""Compare Linux and macOS benchmark JSON files and write Markdown/JSON reports."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Callable


def load(path: Path) -> dict[str, Any]:
    with path.open() as stream:
        data = json.load(stream)
    if data.get("schema_version") != 1:
        raise ValueError(f"unsupported schema in {path}")
    return data


def median(data: dict[str, Any], metric: str) -> float:
    return float(data["summary"][metric]["median"])


def seconds(value: float) -> str:
    return f"{value:.3f} s"


def mebibytes(value: float) -> str:
    return f"{value / 2**20:.1f} MiB"


def gibibytes(value: int | None) -> str:
    return "unknown" if value is None else f"{value / 2**30:.1f} GiB"


def short_revision(value: str) -> str:
    return value[:12]


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
        "sample count": (len(linux["samples"]), len(macos["samples"])),
        "Lean threads": (
            linux["method"]["lean_num_threads"],
            macos["method"]["lean_num_threads"],
        ),
    }
    mismatches = {name: values for name, values in checks.items() if values[0] != values[1]}
    comparable = not mismatches

    metric_specs: list[tuple[str, str, Callable[[float], str]]] = [
        ("wall_seconds", "Wall time", seconds),
        ("cpu_seconds", "User + system CPU", seconds),
        ("max_rss_bytes", "Peak RSS", mebibytes),
    ]
    comparisons: dict[str, Any] = {}
    metric_rows: list[str] = []
    for key, label, formatter in metric_specs:
        linux_value = median(linux, key)
        macos_value = median(macos, key)
        ratio = macos_value / linux_value if linux_value else None
        comparisons[key] = {
            "linux_median": linux_value,
            "macos_median": macos_value,
            "macos_over_linux": ratio,
        }
        ratio_text = "n/a" if ratio is None else f"{ratio:.2f}×"
        metric_rows.append(
            f"| {label} | {formatter(linux_value)} | {formatter(macos_value)} | {ratio_text} |"
        )

    def environment_row(data: dict[str, Any]) -> str:
        info = data["platform"]
        image = "/".join(value for value in [info.get("image_os"), info.get("image_version")] if value)
        return (
            f"| {info['label']} | {info['system']} {info['release']} | {info['machine']} | "
            f"{info['cpu_model']} | {info['logical_cpu_count']} | "
            f"{gibibytes(info['total_memory_bytes'])} | {image or 'unknown'} |"
        )

    if comparable:
        status = "✅ Both jobs used the same Lean build, Mathlib revision, source revision, sample count, and thread count."
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
            "| Metric | Linux | macOS | macOS / Linux |",
            "|---|---:|---:|---:|",
            *metric_rows,
            "",
            f"Each result is the median of {len(linux['samples'])} measured processes after "
            f"{linux['method']['warmups']} warm-ups, with `LEAN_NUM_THREADS={linux['method']['lean_num_threads']}`. "
            "The timing samples do not enable tracing; each platform also performs one separate traced run.",
            "",
            "## Inputs",
            "",
            f"- Lean: `{linux['versions']['lean_toolchain']}`",
            f"- Mathlib: `{short_revision(linux['versions']['mathlib_revision'])}` from `nightly-testing`",
            f"- Source: `{short_revision(linux['github']['sha'] or 'local')}`",
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
            "This compares the end-to-end process cost of checking a file containing only `import Mathlib` "
            "on GitHub-hosted x86-64 runners. It includes Lean/Lake startup and loading cached Mathlib artifacts. "
            "Because the runner hardware and virtualization differ, the ratio is an observed runner ratio, not an "
            "OS-only effect. Download each platform artifact for raw samples and its Firefox Profiler-compatible "
            "`trace.json`.",
            "",
        ]
    )

    report = {
        "schema_version": 1,
        "comparable": comparable,
        "mismatches": mismatches,
        "comparisons": comparisons,
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
