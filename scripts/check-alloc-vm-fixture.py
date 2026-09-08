#!/usr/bin/env python3
"""Verify passive observer coverage on explicit Mac VM operations."""
import json
import os
from pathlib import Path
import subprocess
import sys
out=Path(sys.argv[1]).resolve()
subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','repro/lean-alloc-vm-fixture.c','-o',str(out/'alloc-fixture')],check=True)
result=subprocess.run([str(out/'alloc-fixture')],capture_output=True,text=True,check=True,
    env={**os.environ,'DYLD_INSERT_LIBRARIES':str(out/'lean-alloc-vm.dylib'),'LEAN_ALLOC_VM_OUTPUT':str(out/'fixture-alloc.json')})
(out/'fixture.stdout').write_text(result.stdout);d=json.loads(result.stdout)
events=json.loads((out/'fixture-alloc.json').read_text());assert not events['overflow']
selected=[e for e in events['events'] if e['address']==d['address'] and e['size']==d['size']]
assert [(e['kind'],e['arg']) for e in selected]==[(1,0),(3,3),(4,d['release']),(4,d['reuse']),(3,0),(2,0)]
assert all(e['error']==0 and e['end']>=e['begin'] for e in selected)
print('Verified anonymous map, two protection changes, release, reuse and unmap')
