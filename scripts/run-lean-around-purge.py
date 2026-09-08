#!/usr/bin/env python3
"""Linux fault-around intervention and macOS passive allocator VM capture."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def module(name,path):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--build',type=Path,required=True);p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'metadata.json').exists():p.error('fresh output required')
    mac=sys.platform=='darwin';build=json.loads((a.build/'build.json').read_text());profile=module('profile','scripts/profile-lean-import.py');inner=module('inner','scripts/summarize-lean-inner.py')
    knob='/sys/kernel/debug/fault_around_bytes';original=None
    if not mac:original=int(subprocess.check_output(['sudo','cat',knob],text=True))
    pin=next(x['rev'] for x in json.loads(Path('lake-manifest.json').read_text())['packages'] if x['name']=='mathlib')
    assert profile.capture(['git','-C','.lake/packages/mathlib','rev-parse','HEAD'])==pin
    meta={'source_commit':profile.capture(['git','rev-parse','HEAD']),'build':build,'system':platform.platform(),'page_size':os.sysconf('SC_PAGE_SIZE'),
          'memory_bytes':int(profile.capture(['sysctl','-n','hw.memsize'])) if mac else os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE'),
          'cpu':profile.capture(['sysctl','-n','machdep.cpu.brand_string']) if mac else profile.capture(['lscpu']),
          'mathlib':pin,'workload':Path('ImportMathlibModule.lean').read_text(),'fault_around_original':original,'threads':os.environ.get('LEAN_NUM_THREADS')}
    (out/'metadata.json').write_text(json.dumps(meta,indent=2))
    cases=[]
    if mac:
        cases=[('stock-initial','stock','default'),('phase-initial','phase','default')]
        for i in range(1,4):
            order=['phase','alloc'] if i%2 else ['alloc','phase']
            cases.extend((f'{k}-{i}',k,'default') for k in order)
        cases += [('trace-1','trace','default'),('phase-after','phase','default'),('trace-2','trace','default')]
    else:
        for kind in ['stock','phase']:
            cases.extend((f'{kind}-{mode}-initial',kind,mode) for mode in ['default','single'])
            for i in range(1,5):
                cases.extend((f'{kind}-{mode}-{i}',kind,mode) for mode in (['default','single'] if i%2 else ['single','default']))
        for i in range(1,3):cases.extend((f'trace-{mode}-{i}','trace',mode) for mode in (['default','single'] if i%2 else ['single','default']))
    records=[]
    def set_knob(n):
        subprocess.run(['sudo','tee',knob],input=str(n)+'\n',text=True,stdout=subprocess.DEVNULL,check=True)
        assert int(subprocess.check_output(['sudo','cat',knob],text=True))==n
    try:
        for name,kind,mode in cases:
            if not mac:set_knob(original if mode=='default' else os.sysconf('SC_PAGE_SIZE'))
            env={};command=[build['stock' if kind=='stock' else 'instrumented'],'ImportMathlibModule.lean']
            if kind!='stock':env.update({'LEAN_INNER_TIMING_OUTPUT':str(out/(name+'-events.json')),'LEAN_MEMORY_TIMING':'1'})
            if mac and kind in ('alloc','trace'):
                env.update({'DYLD_INSERT_LIBRARIES':str(out/'lean-alloc-vm.dylib'),'LEAN_ALLOC_VM_OUTPUT':str(out/(name+'-alloc.json'))})
            if kind=='trace':
                preload='DYLD_INSERT_LIBRARIES' if mac else 'LD_PRELOAD';io=str(out/('lean-io-timing.dylib' if mac else 'lean-io-timing.so'))
                env[preload]=io+(':'+env[preload] if preload in env else '')
                env.update({'LEAN_FAULT_REGIONS':str(out/(name+'-regions.txt')),'LEAN_FAULT_MAPS':str(out/(name+'-maps.json')),
                    'LEAN_IO_TIMING_OUTPUT':str(out/(name+'-io.json')),'LEAN_PATH':os.environ['LEAN_PATH'],'LEAN_NUM_THREADS':os.environ.get('LEAN_NUM_THREADS','1')})
                child=['sudo','-u',os.environ['USER'],'env',*[k+'='+v for k,v in env.items()],*command]
                if mac:command=['sudo','ktrace','trace','-t','-N','-p','lean','-f','S0x0130','-b','64','-T','240','-c',*child]
                else:command=['sudo','perf','record','--clockid','mono','-e','minor-faults,major-faults','-c','1','-d','-m','64M','-o',str(out/(name+'.data')),'--',*child]
                env={}
            profile.snapshot(out,name+'-before')
            worker={'output':str(out),'name':name,'command':command,'timeout':270,'sample':False,'environment':env}
            subprocess.run([sys.executable,'scripts/profile-lean-import.py','--worker',json.dumps(worker)],check=True,timeout=330)
            process=json.loads((out/(name+'.json')).read_text());assert process['exit_code']==0 and not process['timed_out']
            r={'name':name,'kind':kind,'mode':mode,'process':process}
            if not mac:
                r['fault_around_bytes']=int(subprocess.check_output(['sudo','cat',knob],text=True));assert r['fault_around_bytes']==(original if mode=='default' else os.sysconf('SC_PAGE_SIZE'))
            if kind!='stock':
                events=json.loads((out/(name+'-events.json')).read_text());r['inner']=inner.summarize(events)
                assert r['inner']['counts']=={'modules':10690,'private_constants':643468,'public_constants':643468,'extra_constant_names':544977,'initial_extensions':224}
            if mac and kind in ('alloc','trace'):
                alloc=json.loads((out/(name+'-alloc.json')).read_text());assert not alloc['overflow'] and alloc['pid']==events['pid'] and alloc['events']
            if kind=='trace' and not mac:
                data=out/(name+'.data');subprocess.run(['sudo','chmod','a+r',str(data)],check=True)
                with (out/(name+'-perf.txt')).open('w') as f:subprocess.run(['sudo','perf','script','-i',str(data),'--show-mmap-events','-F','comm,pid,tid,time,event,addr'],stdout=f,check=True)
            profile.snapshot(out,name+'-after');r['valid']=True;records.append(r)
            (out/'records.json').write_text(json.dumps(records,indent=2));print(name,'valid',process['wall_seconds'],flush=True)
    finally:
        if not mac:
            set_knob(original);(out/'fault-around-restored.json').write_text(json.dumps({'original':original,'restored':int(subprocess.check_output(['sudo','cat',knob],text=True))}))
    return 0

if __name__=='__main__':raise SystemExit(main())
