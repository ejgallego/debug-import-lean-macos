#!/usr/bin/env python3
"""Paired process-only THP intervention on the pinned real Lean import."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--repetitions',type=int,default=4);p.add_argument('--no-trace',action='store_true');a=p.parse_args()
    if sys.platform!='linux' or not os.environ.get('LEAN_PATH'):p.error('Linux, using lake env, required')
    if a.repetitions<2 or a.repetitions%2:p.error('use a positive even repetition count >=2')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'metadata.json').exists():p.error('fresh output required')
    build=json.loads((a.build/'build.json').read_text())
    profile=module('profile','scripts/profile-lean-import.py');inner=module('inner','scripts/summarize-lean-inner.py')
    launcher=out/'lean-thp-exec'
    subprocess.run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','repro/lean-thp-exec.c','-o',str(launcher)],check=True)
    settings={}
    root=Path('/sys/kernel/mm/transparent_hugepage')
    for path in sorted(root.rglob('*')):
        if path.is_file() and path.name in ('enabled','defrag','hpage_pmd_size','use_zero_page','shrink_underused'):
            settings[str(path)]=path.read_text().strip()
    pin=next(x['rev'] for x in json.loads(Path('lake-manifest.json').read_text())['packages'] if x['name']=='mathlib')
    assert profile.capture(['git','-C','.lake/packages/mathlib','rev-parse','HEAD'])==pin
    meta={'build':build,'source_commit':profile.capture(['git','rev-parse','HEAD']),'git_status':profile.capture(['git','status','--short']),
          'system':platform.platform(),'page_size':os.sysconf('SC_PAGE_SIZE'),'cpu':profile.capture(['lscpu']),
          'memory_bytes':os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE'),'workload':Path('ImportMathlibModule.lean').read_text(),
          'mathlib':pin,'threads':os.environ.get('LEAN_NUM_THREADS'),'thp_settings':settings,
          'launcher_sha256':hashlib.sha256(launcher.read_bytes()).hexdigest(),'repetitions':a.repetitions,
          'no_trace':a.no_trace,'intervention':'PR_SET_THP_DISABLE before exec; no host setting changes'}
    (out/'metadata.json').write_text(json.dumps(meta,indent=2))
    cases=[]
    for kind in ['stock','phase']:
        cases += [(f'{kind}-{mode}-initial',kind,mode) for mode in ['default','disabled']]
        for i in range(1,a.repetitions+1):
            modes=['default','disabled'] if i%2 else ['disabled','default']
            cases += [(f'{kind}-{mode}-{i}',kind,mode) for mode in modes]
    for kind in ['census']+([] if a.no_trace else ['trace']):
        for i,modes in [(1,['default','disabled']),(2,['disabled','default'])]:
            cases += [(f'{kind}-{mode}-{i}',kind,mode) for mode in modes]
    records=[]
    for name,kind,mode in cases:
        env={'LEAN_THP_LAUNCH_OUTPUT':str(out/(name+'-launch.json'))}
        command=[str(launcher),mode,build['stock' if kind=='stock' else 'instrumented'],'ImportMathlibModule.lean']
        if kind!='stock':env.update({'LEAN_INNER_TIMING_OUTPUT':str(out/(name+'-events.json')),'LEAN_MEMORY_TIMING':'1'})
        if kind=='census':env['LEAN_THP_TIMING']='1'
        if kind=='trace':
            env.update({'LEAN_FAULT_REGIONS':str(out/(name+'-regions.txt')),'LEAN_FAULT_MAPS':str(out/(name+'-maps.json')),
                        'LEAN_IO_TIMING_OUTPUT':str(out/(name+'-io.json')),'LD_PRELOAD':str(out/'lean-io-timing.so'),
                        'LEAN_PATH':os.environ['LEAN_PATH'],'LEAN_NUM_THREADS':os.environ.get('LEAN_NUM_THREADS','1')})
            child=['sudo','-u',os.environ['USER'],'env',*[k+'='+v for k,v in env.items()],*command]
            command=['sudo','perf','record','--clockid','mono','-e','minor-faults,major-faults','-c','1','-d','-m','64M','-o',str(out/(name+'.data')),'--',*child]
            env={}
        profile.snapshot(out,name+'-before')
        worker={'output':str(out),'name':name,'command':command,'timeout':240,'sample':False,'environment':env}
        subprocess.run([sys.executable,'scripts/profile-lean-import.py','--worker',json.dumps(worker)],check=True,timeout=300)
        process=json.loads((out/(name+'.json')).read_text());assert process['exit_code']==0 and not process['timed_out']
        launch=json.loads((out/(name+'-launch.json')).read_text());expected=int(mode=='disabled')
        assert launch['before']==0 and launch['after']==expected
        record={'name':name,'kind':kind,'mode':mode,'process':process,'launch':launch}
        if kind!='stock':
            events=json.loads((out/(name+'-events.json')).read_text());phases=inner.summarize(events)
            assert events['pid']==launch['pid']
            assert events['thp_disable_initial']==expected and events['thp_disable_final']==expected
            assert phases['counts']=={'modules':10690,'private_constants':643468,'public_constants':643468,'extra_constant_names':544977,'initial_extensions':224}
            record['inner']=phases
            census=[e for e in events['events'] if e['thp_valid']]
            assert len(census)==(8 if kind=='census' else 0)
            if kind=='census':
                if expected:assert all(e['anon_huge_bytes']==0 for e in census)
                record['census']=census
        if kind=='trace':
            data=out/(name+'.data');subprocess.run(['sudo','chmod','a+r',str(data)],check=True)
            with (out/(name+'-perf.txt')).open('w') as f:
                subprocess.run(['sudo','perf','script','-i',str(data),'--show-mmap-events','-F','comm,pid,tid,time,event,addr'],stdout=f,check=True)
        profile.snapshot(out,name+'-after')
        record['valid']=True;records.append(record);(out/'records.json').write_text(json.dumps(records,indent=2))
        print(name,'valid',process['wall_seconds'],flush=True)
    final_settings={path:Path(path).read_text().strip() for path in settings}
    (out/'thp-settings-after.json').write_text(json.dumps(final_settings,indent=2))
    assert final_settings==settings, 'host THP settings changed during experiment'
    return 0

if __name__=='__main__':raise SystemExit(main())
