#!/usr/bin/env python3
"""Real Lean memory snapshots with separate address tracing runs."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys

p=argparse.ArgumentParser(description=__doc__)
p.add_argument('--build',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
if (out/'metadata.json').exists():p.error('fresh output required')
build=json.loads((a.build/'build.json').read_text())
spec=importlib.util.spec_from_file_location('profile','scripts/profile-lean-import.py');profile=importlib.util.module_from_spec(spec);spec.loader.exec_module(profile)
meta={'build':build,'source_commit':profile.capture(['git','rev-parse','HEAD']),'system':platform.platform(),'page_size':os.sysconf('SC_PAGE_SIZE'),
      'cpu':profile.capture(['sysctl','-n','machdep.cpu.brand_string']) if sys.platform=='darwin' else profile.capture(['lscpu']),
      'memory_bytes':int(profile.capture(['sysctl','-n','hw.memsize'])) if sys.platform=='darwin' else os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE'),
      'workload':Path('ImportMathlibModule.lean').read_text(),'lean_path':os.environ['LEAN_PATH'],
      'mathlib':profile.capture(['git','-C','.lake/packages/mathlib','rev-parse','HEAD'])}
pin=next(x['rev'] for x in json.loads(Path('lake-manifest.json').read_text())['packages'] if x['name']=='mathlib')
assert meta['mathlib']==pin
(out/'metadata.json').write_text(json.dumps(meta,indent=2))
for name in ['memory-initial','plain-1','memory-1','trace-1','memory-2','plain-2','trace-2','memory-3']:
    trace=name.startswith('trace');env={}
    if not name.startswith('plain'):
        env={'LEAN_INNER_TIMING_OUTPUT':str(out/(name+'-events.json')),'LEAN_MEMORY_TIMING':'1'}
    command=[build['instrumented'],'ImportMathlibModule.lean']
    if trace:
        env.update({'LEAN_FAULT_REGIONS':str(out/(name+'-regions.txt')),'LEAN_FAULT_MAPS':str(out/(name+'-maps.json')),
                    'LEAN_IO_TIMING_OUTPUT':str(out/(name+'-io.json')),
                    'DYLD_INSERT_LIBRARIES' if sys.platform=='darwin' else 'LD_PRELOAD':str(out/('lean-io-timing.dylib' if sys.platform=='darwin' else 'lean-io-timing.so')),
                    'LEAN_PATH':os.environ['LEAN_PATH'],'LEAN_NUM_THREADS':os.environ.get('LEAN_NUM_THREADS','1')})
        child=['sudo','-u',os.environ['USER'],'env',*[k+'='+v for k,v in env.items()],*command]
        if sys.platform=='darwin': command=['sudo','ktrace','trace','-t','-N','-p','lean','-f','S0x0130','-b','128','-T','180','-c',*child]
        else: command=['sudo','perf','record','-e','minor-faults,major-faults','-c','1','-d','-m','64M','-o',str(out/(name+'.data')),'--',*child]
        env={} # Do not preload the tracing program.
    profile.snapshot(out,name+'-before')
    worker={'output':str(out),'name':name,'command':command,'timeout':240,'sample':False,'environment':env}
    subprocess.run([sys.executable,'scripts/profile-lean-import.py','--worker',json.dumps(worker)],check=True,timeout=300)
    if trace and sys.platform!='darwin':
        data=out/(name+'.data');subprocess.run(['sudo','chmod','a+r',str(data)],check=True)
        with (out/(name+'-perf.txt')).open('w') as f:
            subprocess.run(['sudo','perf','script','-i',str(data),'--show-mmap-events','-F','comm,pid,tid,time,event,addr'],stdout=f,check=True)
    profile.snapshot(out,name+'-after')
    print(name,'finished',flush=True)
