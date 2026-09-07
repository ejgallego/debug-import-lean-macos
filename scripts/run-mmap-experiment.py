#!/usr/bin/env python3
"""Run a bounded C replay experiment on a hosted runner; retain partial results."""
from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
import time


def capture(command: list[str]) -> str:
    return subprocess.check_output(command, text=True).strip()


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def bounded(command: list[str], stdout: Path, stderr: Path, timeout: float) -> dict:
    started = time.monotonic()
    with stdout.open("w") as out, stderr.open("w") as err:
        process = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
        timed_out = False
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            code = process.wait()
    return {"command": command, "exit_code": code, "timed_out": timed_out,
            "wall_seconds": time.monotonic() - started,
            "stdout": stdout.name, "stderr": stderr.name}


def memory_snapshot(output: Path, label: str) -> None:
    commands = ([["vm_stat"], ["sysctl", "vm.swapusage"]] if sys.platform == "darwin"
                else [["cat", "/proc/meminfo"], ["cat", "/proc/vmstat"]])
    for index, command in enumerate(commands):
        # Optional diagnostic failure must not hide the actual experiment result.
        try:
            bounded(command, output / f"{label}-vm-{index}.txt",
                    output / f"{label}-vm-{index}.stderr", 10)
        except OSError as error:
            (output / f"{label}-vm-{index}.stderr").write_text(str(error))


def summary(output: Path, records: list[dict], metadata: dict) -> None:
    lines = ["# C mmap experiment", "",
             f"{metadata['system']} {metadata['release']}, {metadata['machine']}; "
             f"page size {metadata['page_size']}; RAM {metadata['memory_bytes'] / 2**30:.1f} GiB.",
             f"Lean: `{metadata['lean']}`; Mathlib: `{metadata['mathlib']}`.", "",
             "Each C process retains its mappings for three page-access passes, then remaps "
             "for a second cycle. Address modes alternate order between repetitions. "
             "Residency and sampled runs are separate diagnostics. No cache purge is used.", "",
             "| Process | Exit | Wall s | Cycle | Map s | Access 1 s | Access 2 s | Access 3 s | Unmap s |",
             "|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for record in records:
        cycles: dict[str, dict[str, str]] = {}
        if record["name"].startswith(("saved-", "any-", "residency")):
            for row in csv.reader((output / record["stdout"]).open()):
                if len(row) == 10 and row[0] == "phase":
                    key = f"touch-{row[3]}" if row[1] == "touch" else row[1]
                    cycles.setdefault(row[2], {})[key] = f"{float(row[4]):.3f}"
        for cycle, values in (cycles or {"—": {}}).items():
            cells = [record["name"], str(record["exit_code"]), f"{record['wall_seconds']:.3f}", cycle]
            cells += [values.get(key, "—") for key in ["map", "touch-1", "touch-2", "touch-3", "unmap"]]
            lines.append("| " + " | ".join(cells) + " |")
    lines += ["", "The Linux trace supplies artifact order. Destination files supply sizes and "
              "saved addresses. Memory access is synthetic; a fast replay does not rule out "
              "Lean's issue. Inspect CSV mapping outcomes and fallback bytes before attributing "
              "differences to placement. Fault counts and residency semantics differ by OS.", ""]
    text = "\n".join(lines)
    (output / "summary.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--source", choices=["ImportMathlib.lean", "ImportMathlibModule.lean"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repeats", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=300)
    args = parser.parse_args()
    if not 1 <= args.repeats <= 10 or not 0 < args.timeout <= 600:
        parser.error("repeats must be 1..10 and timeout must be in (0, 600]")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    binary = output / "mmap-replay"
    prefix = capture(["lake", "env", "lean", "--print-prefix"])
    manifest = output / "files.txt"
    with manifest.open("w") as stream:
        subprocess.run([sys.executable, "scripts/mapping-files.py", str(args.trace),
                        "--project-root", str(Path.cwd()), "--lean-prefix", prefix],
                       stdout=stream, check=True)
    # Fail before running if any mapped file is absent on this platform.
    paths = [Path(line) for line in manifest.read_text().splitlines()]
    if not all(path.is_file() for path in paths):
        missing = [str(path) for path in paths if not path.is_file()]
        raise RuntimeError(f"missing destination artifacts: {missing[:10]}")
    pinned = json.loads(Path("lake-manifest.json").read_text())
    mathlib = capture(["git", "-C", ".lake/packages/mathlib", "rev-parse", "HEAD"])
    if mathlib != next(p["rev"] for p in pinned["packages"] if p["name"] == "mathlib"):
        raise RuntimeError("Mathlib checkout does not match the pinned manifest")
    memory = (int(capture(["sysctl", "-n", "hw.memsize"])) if sys.platform == "darwin"
              else os.sysconf("SC_PHYS_PAGES") * os.sysconf("SC_PAGE_SIZE"))
    metadata = {
        "system": platform.system(), "release": platform.release(), "machine": platform.machine(),
        "memory_bytes": memory, "page_size": os.sysconf("SC_PAGE_SIZE"),
        "lean": capture(["lake", "env", "lean", "--version"]), "mathlib": mathlib,
        "toolchain": Path("lean-toolchain").read_text().strip(),
        "source_commit": capture(["git", "rev-parse", "HEAD"]),
        "source": args.source, "repeats": args.repeats, "timeout": args.timeout,
        "compiler": capture(["cc", "--version"]), "binary_sha256": sha256(binary),
        "c_source_sha256": sha256(Path("repro/mmap-replay.c")),
        "capture_sha256": sha256(args.trace), "manifest_sha256": sha256(manifest),
        "artifact_count": len(paths), "artifact_bytes": sum(p.stat().st_size for p in paths),
        "environment": {key: os.environ.get(key) for key in
                        ["LEAN_NUM_THREADS", "RUNNER_NAME", "RUNNER_ARCH", "ImageOS", "ImageVersion",
                         "GITHUB_RUN_ID", "GITHUB_RUN_ATTEMPT"]},
    }
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    records = []

    def run(name: str, command: list[str]) -> dict:
        print(f"Running {name}: {command}", flush=True)
        memory_snapshot(output, f"{name}-before")
        record = bounded(command, output / f"{name}.csv", output / f"{name}.stderr", args.timeout)
        record["name"] = name
        records.append(record)
        (output / "processes.json").write_text(json.dumps(records, indent=2) + "\n")
        print(f"Finished {name}: exit {record['exit_code']}, "
              f"{record['wall_seconds']:.3f}s, timeout={record['timed_out']}", flush=True)
        memory_snapshot(output, f"{name}-after")
        summary(output, records, metadata)
        return record

    # A real Lean reference first both confirms the workload and supplies a warmup.
    run("lean-reference", ["lake", "env", "lean", args.source])
    base = [str(binary), "--passes", "3", "--cycles", "2"]
    for repeat in range(1, args.repeats + 1):
        modes = [("saved", []), ("any", ["--any-address"])]
        for mode, flags in modes if repeat % 2 else reversed(modes):
            run(f"{mode}-{repeat}", [*base, *flags, str(manifest)])
    run("residency", [*base, "--residency", str(manifest)])

    if sys.platform == "darwin":
        # Keep sampling separate from baseline timing. A short-lived replay can
        # exit before attachment; record that as unavailable, not a good profile.
        command = [*base, str(manifest)]
        with (output / "sampled-replay.csv").open("w") as out, (output / "sampled-replay.stderr").open("w") as err:
            target = subprocess.Popen(command, stdout=out, stderr=err, start_new_session=True)
            diagnostics = []
            started = time.monotonic()
            try:
                for name, cmd in [("sample", ["sample", str(target.pid), "5", "1", "-file", str(output / "sample.txt")]),
                                  ("vmmap", ["vmmap", "-summary", str(target.pid)])]:
                    diagnostics.append(bounded(cmd, output / f"{name}.stdout", output / f"{name}.stderr", 20))
            except OSError as error:
                diagnostics.append({"error": str(error)})
            finally:
                try:
                    target.wait(timeout=max(0.1, args.timeout - (time.monotonic() - started)))
                except subprocess.TimeoutExpired:
                    try:
                        os.killpg(target.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    target.wait()
            (output / "diagnostics.json").write_text(json.dumps({"target_command": command,
                "target_exit_code": target.returncode, "tools": diagnostics}, indent=2) + "\n")
    print((output / "summary.md").read_text(), flush=True)
    return 1 if any(r["exit_code"] != 0 for r in records) else 0


if __name__ == "__main__":
    raise SystemExit(main())
