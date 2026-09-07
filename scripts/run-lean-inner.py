#!/usr/bin/env python3
"""Compare stock, relinked control, and internally timed real Lean imports."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import platform
import subprocess
import sys


def module(name,path):
    spec=importlib.util.spec_from_file_location(name,path)
    m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);return m


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--build',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if not os.environ.get('LEAN_PATH'):p.error('run using lake env')
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'metadata.json').exists():p.error('use a fresh output directory')
    build=json.loads((args.build/'build.json').read_text())
    profile=module('profile','scripts/profile-lean-import.py')
    phases=module('phases','scripts/summarize-lean-inner.py')
    pin=next(x['rev'] for x in json.loads(Path('lake-manifest.json').read_text())['packages'] if x['name']=='mathlib')
    if profile.capture(['git','-C','.lake/packages/mathlib','rev-parse','HEAD'])!=pin:raise RuntimeError('Mathlib pin mismatch')
    meta={'build':build,'mathlib':pin,'source_commit':profile.capture(['git','rev-parse','HEAD']),
          'git_status':profile.capture(['git','status','--short']),'system':platform.platform(),
          'page_size':os.sysconf('SC_PAGE_SIZE'),'threads':os.environ.get('LEAN_NUM_THREADS'),
          'memory_bytes':int(profile.capture(['sysctl','-n','hw.memsize'])) if sys.platform=='darwin' else os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE'),
          'cpu':profile.capture(['sysctl','-n','machdep.cpu.brand_string']) if sys.platform=='darwin' else profile.capture(['lscpu']),
          'workload':Path('ImportMathlibModule.lean').read_text()}
    (out/'metadata.json').write_text(json.dumps(meta,indent=2)+'\n')
    cases=['stock-initial','control-initial','inner-initial',
           'stock-1','control-1','inner-1','inner-2','stock-2','control-2','control-3','inner-3','stock-3',
           'disabled-1','io-stock','io-control','io-inner']
    records=[];failed=False
    for name in cases:
        kind=name.split('-')[0] if not name.startswith('io-') else name[3:]
        binary=build['instrumented' if kind in ('inner','disabled') else kind]
        env={}
        if kind=='inner':env['LEAN_INNER_TIMING_OUTPUT']=str(out/(name+'-events.json'))
        if name.startswith('io-'):
            lib='lean-io-timing.dylib' if sys.platform=='darwin' else 'lean-io-timing.so'
            env['DYLD_INSERT_LIBRARIES' if sys.platform=='darwin' else 'LD_PRELOAD']=str(out/lib)
            env['LEAN_IO_TIMING_OUTPUT']=str(out/(name+'-io.json'))
        profile.snapshot(out,name+'-before')
        spec={'output':str(out),'name':name,'command':[binary,'ImportMathlibModule.lean'],
              'timeout':300,'sample':False,'environment':env}
        status=subprocess.run([sys.executable,'scripts/profile-lean-import.py','--worker',json.dumps(spec)],timeout=390,check=False)
        record={'name':name,'process':json.loads((out/(name+'.json')).read_text())}
        try:
            if status.returncode or record['process']['exit_code']:raise ValueError('Lean process failed')
            if kind=='inner':
                result=phases.summarize(json.loads((out/(name+'-events.json')).read_text()))
                record['inner']=result
                if result['counts']!={'modules':10690,'private_constants':643468,'public_constants':643468,'extra_constant_names':544977,'initial_extensions':224}:
                    raise ValueError('workload cardinalities differ from pinned local control')
                tops=[s['inclusive'] for s in result['spans'] if s['parent'] is None]
                if sum(s['wall_ns'] for s in tops)/1e9>record['process']['wall_seconds']:raise ValueError('phase time exceeds process time')
                for key in ['user','system']:
                    if sum(s[key+'_ns'] for s in tops)/1e9>record['process'][key+'_seconds']+.002:raise ValueError('phase CPU exceeds process CPU')
                (out/(name+'-summary.md')).write_text(phases.markdown(result))
            if name.startswith('io-'):
                io=json.loads((out/(name+'-io.json')).read_text());record['io']=io
                if not all(io[k]==37687 for k in ['open_count','fstat_count','header_read_count','mmap_count','close_count']):raise ValueError('incomplete artifact call capture')
                if io['mmap_bytes']!=7303535912 or io['header_read_bytes']!=3316456 or io['untracked_fd_count']:raise ValueError('artifact capture bytes differ')
                if io['fallback_count']!=io['mmap_failed']+io['mmap_wrong_address']:raise ValueError('fallback accounting differs')
            record['valid']=True
        except (ValueError,KeyError,FileNotFoundError) as error:
            record['valid']=False;record['error']=str(error);failed=True
        records.append(record);(out/'records.json').write_text(json.dumps(records,indent=2)+'\n')
        profile.snapshot(out,name+'-after')
        profile.summary(out,[r['process'] for r in records])
        print(name,'valid:',record['valid'],record.get('error',''),flush=True)
    return int(failed)

if __name__=='__main__':raise SystemExit(main())
