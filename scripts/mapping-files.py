#!/usr/bin/env python3
"""Extract ordered artifact mmap attempts from a direct Lean strace -f -yy log.

Capture with `lake env strace ... lean ...`, not `strace ... lake env lean ...`:
the latter includes Lake's own imports. Paths and duplicates are preserved.
This is a Linux capture aid; the resulting text manifest is consumed by portable C.
"""
import argparse
import re
from pathlib import Path

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("trace", type=Path)
parser.add_argument("--project-root", type=Path, help="rebase paths containing /.lake/ to this checkout")
parser.add_argument("--lean-prefix", type=Path, help="rebase toolchain /lib/lean/ paths to this prefix")
args = parser.parse_args()
pattern = re.compile(
    r"\bmmap\(\s*(?:0x[0-9a-f]+|NULL),\s*\d+,\s*PROT_READ\|PROT_WRITE,\s*"
    r"MAP_PRIVATE(?:\|MAP_FIXED_NOREPLACE)?,\s*\d+<([^>]+)>,\s*0\s*\)"
)
artifact = re.compile(r"\.(?:olean(?:\.(?:server|private))?|ir(?:\.(?:sig|private))?)$")
paths = []
pending = {}
for order, line in enumerate(args.trace.read_text().splitlines()):
    # strace can split calls across lines even with LEAN_NUM_THREADS=1.
    pid_match = re.match(r"\s*(\d+)\s+(.*)", line)
    pid, body = (pid_match[1], pid_match[2]) if pid_match else ("main", line)
    if "<... mmap resumed>" in body:
        if pid not in pending:
            parser.error("mmap resume without matching entry")
        order, entry = pending.pop(pid)
        line = entry + body.split("<... mmap resumed>", 1)[1]
    if "mmap(" not in line:
        continue
    if "<unfinished ...>" in line:
        if pid in pending:
            parser.error("overlapping unfinished mmap calls for one thread")
        pending[pid] = (order, line.split("<unfinished ...>", 1)[0])
        continue
    match = pattern.search(line)
    if match and artifact.search(match[1]):
        path = match[1]
        if "\\" in path:
            parser.error("escaped strace path needs explicit decoding")
        if args.project_root and "/.lake/" in path:
            path = str(args.project_root.resolve() / ".lake" / path.split("/.lake/", 1)[1])
        elif args.lean_prefix and "/lib/lean/" in path:
            path = str(args.lean_prefix.resolve() / "lib/lean" / path.split("/lib/lean/", 1)[1])
        paths.append((order, path))
if pending:
    parser.error("trace ended with unfinished mmap calls")
if not paths:
    parser.error("no compacted artifact mapping attempts found")
print("\n".join(path for _, path in sorted(paths)))
