#!/usr/bin/env python3
"""Bounded CI capability probe; failures are data, not silently missing traces."""
import json
import os
from pathlib import Path
import platform
import subprocess

out=Path('results/fault-probe');out.mkdir(parents=True,exist_ok=True)
fixture=out/'fixture.py'
fixture.write_text('''import mmap, os, time
p=os.sysconf('SC_PAGE_SIZE')
f=open('results/fault-probe/pages.bin','w+b'); f.write(b'x'*(p*1024)); f.flush()
a=mmap.mmap(f.fileno(),0,access=mmap.ACCESS_COPY)
b=mmap.mmap(-1,p*1024)
print('pid',os.getpid(),flush=True)
for i in range(0,p*1024,p): x=a[i]; a[i]=1; b[i]=1
time.sleep(1)
''')
commands=([['sw_vers'],['csrutil','status'],['ktrace','trace','--help'],
 ['sudo','ktrace','trace','-t','-N','-f','S0x0130','-T','10','-c','/usr/bin/sudo','-u',os.environ['USER'],'python3',str(fixture)],
 ['sudo','ktrace','trace','--ndjson','-f','S0x0130','-T','10','-c','/usr/bin/sudo','-u',os.environ['USER'],'python3',str(fixture)]]
 if platform.system()=='Darwin' else [['uname','-a'],['perf','version'],
 ['sudo','perf','record','-e','minor-faults,major-faults','-c','1','-d','-o',str(out/'perf.data'),'--','sudo','-u',os.environ['USER'],'python3',str(fixture)],
 ['sudo','perf','script','-i',str(out/'perf.data'),'--show-mmap-events','-F','comm,pid,tid,time,event,addr']])
records=[]
for i,cmd in enumerate(commands):
    with (out/f'{i}.stdout').open('w') as stdout,(out/f'{i}.stderr').open('w') as stderr:
        try: r=subprocess.run(cmd,stdout=stdout,stderr=stderr,timeout=30);code=r.returncode
        except (OSError,subprocess.TimeoutExpired) as e:code=str(e)
    records.append({'command':cmd,'exit':code});print(records[-1],flush=True)
(out/'commands.json').write_text(json.dumps(records,indent=2))

if platform.system()=="Linux" and (out/"perf.data").exists(): subprocess.run(["sudo","chmod","a+r",str(out/"perf.data")],check=True)
