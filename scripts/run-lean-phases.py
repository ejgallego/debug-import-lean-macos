#!/usr/bin/env python3
"""Pair direct Lean baselines with passive I/O timers and sparse LLDB phase stops."""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import platform
import signal
import subprocess
import sys
from process_usage import check_units


def load_profile():
    spec = importlib.util.spec_from_file_location('profile_import', 'scripts/profile-lean-import.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    if sys.platform not in ('darwin','linux') or not os.environ.get('LEAN_PATH'):
        parser.error('run with lake env on Linux or ARM macOS')
    output=args.output.resolve();output.mkdir(parents=True,exist_ok=True)
    if (output/'metadata.json').exists():parser.error('use a fresh output directory')
    profile=load_profile()
    capture=profile.capture
    prefix=Path(capture(['lean','--print-prefix']));lean=prefix/'bin/lean'
    mac=sys.platform=='darwin'
    library=output/('lean-io-timing.dylib' if mac else 'lean-io-timing.so')
    helper=output/'process-usage'
    preload='DYLD_INSERT_LIBRARIES' if mac else 'LD_PRELOAD'
    command=[str(lean),'ImportMathlibModule.lean']
    pin=json.loads(Path('lake-manifest.json').read_text())
    mathlib=capture(['git','-C','.lake/packages/mathlib','rev-parse','HEAD'])
    if mathlib!=next(p['rev'] for p in pin['packages'] if p['name']=='mathlib'):raise RuntimeError('Mathlib pin mismatch')
    digest=lambda p:hashlib.sha256(Path(p).read_bytes()).hexdigest()
    metadata={'lean':capture([str(lean),'--version']),'mathlib':mathlib,'command':command,
              'source_commit':capture(['git','rev-parse','HEAD']),'git_status':capture(['git','status','--short']),
              'system':platform.platform(),'memory_bytes':int(capture(['sysctl','-n','hw.memsize'])) if mac else os.sysconf('SC_PHYS_PAGES')*os.sysconf('SC_PAGE_SIZE'),
              'page_size':os.sysconf('SC_PAGE_SIZE'),'debugger':capture(['lldb','--version']),
              'compiler':capture(['cc','--version']),
              'resource_check':check_units({'usage_helper':str(helper)}),
              'hashes':{str(p):digest(p) for p in [lean,library,Path('repro/lean-io-timing.c'),Path('scripts/lean_phase_lldb.py'),Path(__file__),Path('repro/process-usage.c'),Path('scripts/process_usage.py')] + ([helper] if mac else [])},
              'environment':{k:os.environ.get(k) for k in ['LEAN_NUM_THREADS','GITHUB_RUN_ID','ImageOS','ImageVersion']}}
    (output/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')
    records=[];failed=False
    def summary():
        lines=['# Real Lean phase timings','','Diagnostics retain ASLR. Syscall timers are passive; LLDB stops only at phase entry/return.',
               'Uninstrumented timings and debugger timings are separate. Copy-read time excludes allocation and relocation.','',
               '| Run | Direct wall s | Load phase s | Finalize phase s | mmap s | Header reads s | Copy reads s | Mappings | Fallbacks |',
               '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
        for r in records:
            io=r.get('io',{});ph={e['phase']:e for e in r.get('phases',{}).get('events',[])}
            val=lambda k:f'{io[k]/1e9:.3f}' if k in io else '—'
            phase=lambda k:f"{ph[k]['wall_seconds']:.3f}" if k in ph and 'wall_seconds' in ph[k] else '—'
            wall=f"{r['process']['wall_seconds']:.3f}" if 'process' in r else '—'
            lines.append('| '+' | '.join([r['name'],wall,phase('load'),phase('finalize'),val('mmap_ns'),val('header_read_ns'),val('copy_read_ns'),str(io.get('mmap_count','—')),str(io.get('fallback_count','—'))])+' |')
        lines += ['', '| Run / phase | Wall s | User CPU s | Kernel CPU s | Page-ins (Mac) / major faults (Linux) |', '|---|---:|---:|---:|---:|']
        for r in records:
            for e in r.get('phases',{}).get('events',[]):
                if 'usage_delta' not in e: continue
                u=e['usage_delta']
                lines.append(f"| {r['name']} / {e['phase']} | {e['wall_seconds']:.3f} | {u['user_ns']/1e9:.3f} | {u['system_ns']/1e9:.3f} | {u.get('pageins',u.get('major_faults'))} |")
        text='\n'.join(lines)+'\n';(output/'summary.md').write_text(text)
        if os.environ.get('GITHUB_STEP_SUMMARY'):Path(os.environ['GITHUB_STEP_SUMMARY']).write_text(text)
    for name in ['warmup','baseline-1','io-1','phases-1','phases-2','io-2','baseline-2','phases-3','baseline-3']:
        print('Running '+name,flush=True)
        profile.snapshot(output,name+'-before')
        env={} if name.startswith(('baseline','warmup')) else {preload:str(library),'LEAN_IO_TIMING_OUTPUT':str(output/(name+'-io.json'))}
        record={'name':name}
        if name.startswith('phases'):
            spec={'command':command,'environment':env,'stdout':str(output/(name+'.stdout')),
                  'stderr':str(output/(name+'.stderr')),'phase_output':str(output/(name+'-phases.json')),'usage_helper':str(helper)}
            spec_path=output/(name+'-spec.json');spec_path.write_text(json.dumps(spec,indent=2)+'\n')
            cmds=['lldb','--batch','--no-lldbinit','-o','command script import '+str(Path('scripts/lean_phase_lldb.py').resolve()),
                  '-o','script lean_phase_lldb.run(lldb.debugger, '+json.dumps(str(spec_path))+')']
            with (output/(name+'-lldb.log')).open('w') as log:
                proc=subprocess.Popen(cmds,stdout=log,stderr=subprocess.STDOUT,start_new_session=True)
                try:proc.wait(timeout=360)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid,signal.SIGKILL);proc.wait();record['timed_out']=True
                record['debugger_exit_code']=proc.returncode
            path=output/(name+'-phases.json')
            if path.exists():record['phases']=json.loads(path.read_text())
            ph=record.get('phases',{})
            valid=proc.returncode==0 and ph.get('exit_code')==0 and not ph.get('errors')
            valid &= sorted(e['phase'] for e in ph.get('events',[]))==['finalize','load']
            valid &= all('wall_seconds' in e and 'usage_delta' in e for e in ph.get('events',[]))
            failed |= not valid
        else:
            spec={'output':str(output),'name':name,'command':command,'timeout':300,'sample':False,'environment':env}
            proc=subprocess.run([sys.executable,'scripts/profile-lean-import.py','--worker',json.dumps(spec)],timeout=390,check=False)
            failed |= proc.returncode!=0
            record['process']=json.loads((output/(name+'.json')).read_text())
        if env:
            path=output/(name+'-io.json')
            if path.exists():record['io']=json.loads(path.read_text())
            io=record.get('io',{})
            # Same pinned module workload as the verified Linux trace.
            valid=io.get('mmap_count')==37687 and io.get('untracked_fd_count')==0
            valid &= io.get('header_read_bytes')==37687*88 and io.get('mmap_bytes')==7303535912
            valid &= all(io.get(k)==37687 for k in ('open_count','fstat_count','close_count'))
            valid &= io.get('fallback_count')==io.get('mmap_failed',0)+io.get('mmap_wrong_address',0)
            record['io_valid']=valid;failed |= not valid
            if name.startswith('phases'):
                events=record.get('phases',{}).get('events',[])
                load=next((e for e in events if e['phase']=='load'),{})
                finalize=next((e for e in events if e['phase']=='finalize'),{})
                coverage=bool(load.get('counter_delta')) and bool(finalize.get('counter_delta'))
                coverage &= all(v==0 for v in load.get('entry_counters',{}).values())
                coverage &= load.get('counter_delta')==io
                coverage &= all(v==0 for v in finalize.get('counter_delta',{}).values())
                record['phase_coverage_valid']=coverage;failed |= not coverage
        records.append(record)
        (output/'records.json').write_text(json.dumps(records,indent=2)+'\n')
        profile.snapshot(output,name+'-after');summary()
        print(json.dumps(record),flush=True)
    return int(failed)


if __name__=='__main__':raise SystemExit(main())
