#!/usr/bin/env python3
"""Profile the real Lean import; native sampling is separate from baseline runs."""
from __future__ import annotations

import argparse
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


def snapshot(output: Path, label: str) -> None:
    commands = [["vm_stat"], ["sysctl", "vm.swapusage"]] if sys.platform == "darwin" else [["cat", "/proc/meminfo"], ["cat", "/proc/vmstat"]]
    for i, command in enumerate(commands):
        with (output / f"{label}-vm-{i}.txt").open("w") as out:
            subprocess.run(command, stdout=out, stderr=subprocess.STDOUT, timeout=10, check=False)


def worker(spec: dict) -> int:
    output = Path(spec["output"])
    name = spec["name"]
    diagnostic = spec["sample"]
    sampler = None
    sample_record = None
    timeline = []
    with (output / f"{name}.stdout").open("w") as out, (output / f"{name}.stderr").open("w") as err, (output / f"{name}-sample-tool.txt").open("w") as sample_log:
        started = time.monotonic()
        target = subprocess.Popen(spec["command"], stdout=out, stderr=err, start_new_session=True,
                                  env={**os.environ, **spec.get("environment", {})})
        if diagnostic:
            command = ["sample", str(target.pid), "120", "1", "-mayDie", "-file", str(output / f"{name}.sample.txt")]
            sample_record = {"command": command, "target_pid": target.pid}
            try:
                sampler = subprocess.Popen(command, stdout=sample_log, stderr=subprocess.STDOUT)
            except OSError as error:
                sample_record["error"] = str(error)
        timed_out = False
        next_probe = started
        while True:
            pid, status, usage = os.wait4(target.pid, os.WNOHANG)
            if pid:
                target.returncode = os.waitstatus_to_exitcode(status)
                break
            elapsed = time.monotonic() - started
            if elapsed >= spec["timeout"]:
                timed_out = True
                try:
                    os.killpg(target.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                _, status, usage = os.wait4(target.pid, 0)
                target.returncode = os.waitstatus_to_exitcode(status)
                break
            if diagnostic and time.monotonic() >= next_probe:
                entry = {"elapsed_seconds": elapsed}
                for label, command in [("process", ["ps", "-p", str(target.pid), "-o", "pid=,rss=,vsz=,time=,state="]), ("vm_stat", ["vm_stat"])]:
                    try:
                        p = subprocess.run(command, text=True, capture_output=True, timeout=3)
                        entry[label] = {"stdout": p.stdout, "stderr": p.stderr, "exit_code": p.returncode}
                    except (OSError, subprocess.TimeoutExpired) as error:
                        entry[label] = {"error": str(error)}
                timeline.append(entry)
                next_probe = time.monotonic() + 1
            time.sleep(0.01)
        wall = time.monotonic() - started
        if sampler:
            try:
                sampler.wait(timeout=30)
            except subprocess.TimeoutExpired:
                sampler.terminate()
                try:
                    sampler.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    sampler.kill()
                    sampler.wait()
                sample_record["cleanup_timeout"] = True
            sample_record["exit_code"] = sampler.returncode
    # wait4 reports this Lean PID only, excluding the sampler and monitoring tools.
    record = {"name": name, "command": spec["command"], "pid": target.pid,
              "wall_seconds": wall, "user_seconds": usage.ru_utime,
              "system_seconds": usage.ru_stime,
              "max_rss_bytes": usage.ru_maxrss * (1024 if sys.platform.startswith("linux") else 1),
              "minor_faults": usage.ru_minflt, "major_faults": usage.ru_majflt,
              "input_blocks": usage.ru_inblock, "output_blocks": usage.ru_oublock,
              "voluntary_context_switches": usage.ru_nvcsw,
              "involuntary_context_switches": usage.ru_nivcsw,
              "exit_code": target.returncode, "timed_out": timed_out,
              "sample": sample_record}
    (output / f"{name}.json").write_text(json.dumps(record, indent=2) + "\n")
    if diagnostic:
        (output / f"{name}-timeline.json").write_text(json.dumps(timeline, indent=2) + "\n")
    print(json.dumps(record), flush=True)
    return 0 if target.returncode == 0 else 1


def summary(output: Path, records: list[dict]) -> None:
    lines = ["# Actual Lean import profile", "",
             "The first import is retained separately (sampled only with --sample-initial). Baselines have no sampler or timeline monitoring.",
             "CPU, faults, and peak RSS come from wait4 on the direct Lean PID; VM snapshots are host-wide.", "",
             "| Run | Wall s | User s | System s | Peak RSS MiB | Minor faults | Major faults | Exit |",
             "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for r in records:
        lines.append(f"| {r['name']} | {r['wall_seconds']:.3f} | {r['user_seconds']:.3f} | {r['system_seconds']:.3f} | {r['max_rss_bytes']/2**20:.1f} | {r['minor_faults']} | {r['major_faults']} | {r['exit_code']} |")
    lines += ["", "Native samples include waiting threads and kernel-call boundaries; they are not CPU-only samples or internal kernel stacks.",
              "Verify the sampled PID, symbol quality, duration, and sampler exit before interpreting the profile.",
              "Diagnostic wall time includes monitoring overhead and is not a baseline result.", ""]
    text = "\n".join(lines)
    (output / "summary.md").write_text(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", default="ImportMathlibModule.lean", choices=["ImportMathlibModule.lean", "ImportMathlib.lean"])
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--no-sample", action="store_true", help="local Linux smoke control")
    parser.add_argument("--sample-initial", action="store_true", help="also profile the first Mathlib import on this runner")
    args = parser.parse_args()
    if not 0 < args.timeout <= 600:
        parser.error("timeout must be in (0, 600]")
    if args.sample_initial and args.no_sample:
        parser.error("--sample-initial requires sampling")
    if not args.no_sample and (sys.platform != "darwin" or platform.machine() != "arm64"):
        parser.error("native sampling requires ARM macOS; use --no-sample for a local control")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    if (output / "metadata.json").exists():
        parser.error("output already contains a run; use a fresh directory")
    prefix = Path(capture(["lean", "--print-prefix"]))
    lean = prefix / "bin" / "lean"
    # Run this orchestrator through `lake env` so the direct binary inherits LEAN_PATH.
    if not os.environ.get("LEAN_PATH"):
        parser.error("run via lake env to supply the pinned dependency paths")
    pinned = json.loads(Path("lake-manifest.json").read_text())
    mathlib = capture(["git", "-C", ".lake/packages/mathlib", "rev-parse", "HEAD"])
    if mathlib != next(p["rev"] for p in pinned["packages"] if p["name"] == "mathlib"):
        parser.error("Mathlib does not match the manifest")
    digest = lambda p: hashlib.sha256(Path(p).read_bytes()).hexdigest()
    metadata = {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
                "page_size": os.sysconf("SC_PAGE_SIZE"), "lean": capture([str(lean), "--version"]),
                "lean_binary": str(lean), "lean_binary_sha256": digest(lean),
                "mathlib": mathlib, "source": args.source, "source_sha256": digest(args.source),
                "toolchain": Path("lean-toolchain").read_text().strip(),
                "source_commit": capture(["git", "rev-parse", "HEAD"]),
                "git_status": capture(["git", "status", "--short"]),
                "script_sha256": digest(__file__), "timeout": args.timeout, "sample_initial": args.sample_initial,
                "environment": {k: os.environ.get(k) for k in ["LEAN_PATH", "LEAN_NUM_THREADS", "RUNNER_ARCH", "ImageOS", "ImageVersion", "GITHUB_RUN_ID"]}}
    if sys.platform == "darwin":
        metadata["memory_bytes"] = int(capture(["sysctl", "-n", "hw.memsize"]))
        metadata["cpu"] = capture(["sysctl", "-n", "machdep.cpu.brand_string"])
        subprocess.run(["sh", "-c", "MANPAGER=cat man sample"], stdout=(output / "sample-man.txt").open("w"), stderr=subprocess.STDOUT, timeout=10, check=False)
    (output / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    records = []
    cases = [("initial-sampled", True) if args.sample_initial else ("warmup", False),
             ("baseline-1", False), ("baseline-2", False)]
    if not args.no_sample:
        cases.append(("sampled", True))
    cases.append(("baseline-3", False))
    failed = False
    for name, sample in cases:
        snapshot(output, f"{name}-before")
        spec = {"output": str(output), "name": name, "command": [str(lean), args.source], "timeout": args.timeout, "sample": sample}
        print(f"Running {name}", flush=True)
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--worker", json.dumps(spec)], timeout=args.timeout+90, check=False)
        failed |= result.returncode != 0
        record = json.loads((output / f"{name}.json").read_text())
        records.append(record)
        snapshot(output, f"{name}-after")
        summary(output, records)
    if not args.no_sample:
        statuses = {}
        for r in records:
            if r["sample"] is None:
                continue
            profile = output / f"{r['name']}.sample.txt"
            usable = r["sample"].get("exit_code") == 0 and profile.exists() and "Call graph:" in profile.read_text()
            statuses[r["name"]] = {"sampler_completed_with_call_graph": usable}
            failed |= not usable
        (output / "profile-status.json").write_text(json.dumps(statuses, indent=2)+"\n")
    return int(failed)


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--worker":
        raise SystemExit(worker(json.loads(sys.argv[2])))
    raise SystemExit(main())
