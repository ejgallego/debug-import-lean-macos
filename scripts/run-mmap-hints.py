#!/usr/bin/env python3
"""Small address-order experiment; no Lean installation or artifact download."""
import argparse
import csv
import hashlib
import json
import os
from pathlib import Path
import platform
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--output", type=Path, required=True)
parser.add_argument("--counts", nargs="+", type=int, default=[4096, 16384, 32768])
parser.add_argument("--repeats", type=int, default=2)
args = parser.parse_args()
if not 1 <= args.repeats <= 4 or any(not 1 <= n <= 100000 for n in args.counts):
    parser.error("repeats must be 1..4 and counts 1..100000")
out = args.output.resolve()
out.mkdir(parents=True, exist_ok=True)
binary = out / "mmap-hints"
metadata = {"system": platform.system(), "release": platform.release(), "machine": platform.machine(),
            "page_size": os.sysconf("SC_PAGE_SIZE"), "counts": args.counts, "repeats": args.repeats,
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "source_sha256": hashlib.sha256(Path("repro/mmap-hints.c").read_bytes()).hexdigest(),
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
            "compiler": subprocess.check_output(["cc", "--version"], text=True).strip(),
            "runner": {k: os.environ.get(k) for k in ["RUNNER_ARCH", "ImageOS", "ImageVersion"]}}
(out / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
records = []
# Same count/page/FD: vary only hints, ordering, or spacing. The anonymous
# controls run only at the largest count; their VM entries may coalesce.
cases = [("any", 65536, "file"), ("ascending", 1073741824, "file"),
         ("descending", 1073741824, "file"), ("shuffled", 1073741824, "file"),
         ("shuffled", 65536, "file")]
for count in args.counts:
    selected = cases + ([("any", 65536, "anon"), ("ascending", 1073741824, "anon"),
                         ("shuffled", 1073741824, "anon")] if count == max(args.counts) else [])
    for repeat in range(1, args.repeats + 1):
        for order, spacing, backing in selected if repeat % 2 else reversed(selected):
            name = f"{count}-{order}-{spacing}-{backing}-{repeat}"
            command = [str(binary), str(count), str(spacing), order, backing]
            print(f"Running {name}", flush=True)
            started = time.monotonic()
            with (out / f"{name}.csv").open("w") as stdout, (out / f"{name}.stderr").open("w") as stderr:
                try:
                    result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=90)
                    code = result.returncode
                except subprocess.TimeoutExpired:
                    code = 124
            phases = {}
            with (out / f"{name}.csv").open() as stream:
                for row in csv.reader(stream):
                    if row[:1] == ["phase"]:
                        phases[row[1]] = float(row[2])
            records.append({"name": name, "command": command, "exit_code": code,
                            "wall_seconds": time.monotonic() - started, "phases": phases})
            (out / "processes.json").write_text(json.dumps(records, indent=2) + "\n")
            print(f"exit={code} phases={phases}", flush=True)
lines = ["# Mapping hints: no page access", "",
         f"{metadata['system']} {metadata['release']}, {metadata['machine']}; page size {metadata['page_size']}.",
         "One page per mapping, one shared backing file or anonymous memory; ordinary non-destructive "
         "hints on every OS. Reverse case order on the second repetition. "
         "Nonzero exits flag timeouts, failures, or hints that were not honored.", "",
         "| Case | Exit | Map s | Unmap s |", "|---|---:|---:|---:|"]
for r in records:
    lines.append(f"| {r['name']} | {r['exit_code']} | {r['phases'].get('map', '—')} | {r['phases'].get('unmap', '—')} |")
text = "\n".join(lines) + "\n"
(out / "summary.md").write_text(text)
if os.environ.get("GITHUB_STEP_SUMMARY"):
    Path(os.environ["GITHUB_STEP_SUMMARY"]).write_text(text)
print(text)
raise SystemExit(1 if any(r["exit_code"] for r in records) else 0)
