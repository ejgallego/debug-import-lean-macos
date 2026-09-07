#!/usr/bin/env python3
"""Classify traced Lean faults; keep endpoint-map inference and trace loss visible."""
import argparse
from bisect import bisect_right
from collections import Counter, defaultdict
import importlib.util
import json
from pathlib import Path
import re

TYPES={1:'zero-fill',2:'page-in',3:'copy-on-write',4:'cache-hit',5:'nonzero-fill',6:'guard',7:'page-in-file',8:'page-in-anonymous',9:'compressor',10:'compressor-swapin',11:'copy-on-read'}


def intervals(data):
    stack=[];spans=[]
    for e in data['events']:
        if e['kind']=='B':stack.append(e)
        elif e['kind']=='E':
            b=stack.pop();assert b['label']==e['label']
            spans.append((b,e))
    assert not stack
    return spans


class Ranges:
    def __init__(self,rows):
        self.rows=sorted(rows);self.starts=[r[0] for r in self.rows]
    def find(self,a):
        i=bisect_right(self.starts,a)-1
        return self.rows[i] if i>=0 and a<self.rows[i][1] else None


def classifier(out,name,page_size):
    maps=json.loads((out/(name+'-maps.json')).read_text());assert not maps['overflow']
    # Include the file's final partial page, as the kernel maps whole pages.
    artifact=Ranges([(a,(b+page_size-1)//page_size*page_size,t) for a,b,t in maps['maps']])
    native=[];other=[]
    lines=(out/(name+'-regions.txt')).read_text().splitlines();assert lines[0]=='error 0'
    for line in lines[1:]:
        f=line.split()
        if '-' in f[0]:
            a,b=(int(x,16) for x in f[0].split('-'));path=f[5] if len(f)>5 else ''
            kind='anonymous' if not path or path.startswith('[') else 'other-file'
            if '/bin/lean' in path or '.so' in path:kind='native-image'
        else:a,b,kind=int(f[0],16),int(f[1],16),f[2]
        (native if kind=='native-image' else other).append((a,b,kind))
    native=Ranges(native);other=Ranges(other)
    def classify(addr,monotonic_ns):
        r=artifact.find(addr)
        if r and monotonic_ns>=r[2]:return 'artifact'
        r=native.find(addr) or other.find(addr)
        return r[2] if r else 'unclassified'
    return classify,len(maps['maps'])


def mac_faults(path,pid):
    pending={};faults=[];errors=Counter();other_pids=Counter()
    pattern=re.compile(r'^\s*(\d+)\s+[\d.]+(?:\([^)]*\))?\s+(130000[9a])\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+([0-9a-f]+)\s+\S+\s+.*\((\d+)\)\s*$')
    for line in path.open():
        if 'lost' in line.lower() or 'dropped' in line.lower():errors['loss_messages']+=1
        m=pattern.match(line)
        if not m:
            if re.search(r'\b130000[9a]\b',line):errors['unparsed_fault_lines']+=1
            continue
        clock,code,arg1,addr,arg3,arg4,thread,proc=m.groups()
        if int(proc)!=pid:other_pids[proc]+=1;continue
        clock=int(clock);addr=int(addr,16)
        if code.endswith('9'):
            if thread in pending:errors['duplicate_start']+=1
            pending[thread]=(clock,addr)
        elif thread not in pending:errors['orphan_end']+=1
        else:
            begin,firstaddr=pending.pop(thread)
            if addr!=firstaddr:errors['address_mismatch']+=1
            faults.append((begin,clock,addr,TYPES.get(int(arg4,16),'unknown-'+arg4)))
    errors['unfinished']=len(pending)
    return faults,dict(errors),dict(other_pids)


def linux_faults(path,pid):
    faults=[];errors=Counter();other=Counter()
    pattern=re.compile(r'^\s*\S+\s+(\d+)/(\d+)\s+(\d+)\.(\d+):\s+(minor-faults|major-faults):\s+([0-9a-f]+)')
    for line in path.open():
        if 'LOST' in line:errors['loss_messages']+=1
        m=pattern.match(line)
        if not m:
            if 'minor-faults:' in line or 'major-faults:' in line:errors['unparsed_fault_lines']+=1
            continue
        proc,tid,sec,frac,kind,addr=m.groups()
        if int(proc)!=pid:other[proc]+=1;continue
        clock=int(sec)*10**9+int(frac.ljust(9,'0'))
        faults.append((clock,clock,int(addr,16),kind))
    return faults,dict(errors),dict(other)


def summarize(out):
    meta=json.loads((out/'metadata.json').read_text());mac='macOS' in meta['system']
    spec=importlib.util.spec_from_file_location('inner',Path(__file__).with_name('summarize-lean-inner.py'));inner=importlib.util.module_from_spec(spec);spec.loader.exec_module(inner)
    report={'metadata':meta,'runs':[]}
    for path in sorted(out.glob('*-events.json')):
        name=path.name[:-len('-events.json')];data=json.loads(path.read_text())
        validated=inner.summarize(data);spans=intervals(data)
        process=json.loads((out/(name+'.json')).read_text())
        assert process['exit_code']==0 and not process['timed_out'], 'target or tracer failed'
        assert validated['counts']=={'modules':10690,'private_constants':643468,'public_constants':643468,'extra_constant_names':544977,'initial_extensions':224}
        if name.startswith('trace') and not mac:
            assert '--clockid' in process['command'] and 'mono' in process['command'], 'perf clock must explicitly match CLOCK_MONOTONIC'
        record={'name':name,'pid':data['pid'],'compression_supported':data['compression_supported'],'counts':validated['counts'],'memory':[]}
        for b,e in spans:
            if b['memory_valid'] and e['memory_valid']:
                metrics=['wall_ns','user_ns','system_ns','minor_faults','major_faults','disk_read_bytes','disk_write_bytes','decompressions']
                d={k:e[k]-b[k] for k in metrics};assert all(v>=0 for v in d.values())
                d.update({k:[b[k],e[k]] for k in ['resident_bytes','compressed_bytes']});d['phase']=b['label'];record['memory'].append(d)
        if name.startswith('trace'):
            classify,n=classifier(out,name,meta['page_size'])
            io=json.loads((out/(name+'-io.json')).read_text())
            assert all(io[k]==37687 for k in ['open_count','fstat_count','header_read_count','mmap_count','close_count'])
            assert io['mmap_bytes']==7303535912 and io['header_read_bytes']==3316456 and not io['untracked_fd_count']
            assert n==io['mmap_count']-io['mmap_failed']-io['mmap_wrong_address']
            faults,errors,other=(mac_faults(out/(name+'.stdout'),data['pid']) if mac else linux_faults(out/(name+'-perf.txt'),data['pid']))
            stderr=(out/(name+'.stderr')).read_text()
            if re.search(r'\blost\b|\bdropped\b',stderr,re.I):errors['stderr_loss_message']=1
            assert faults, 'no target fault addresses captured'
            # macOS clock_gettime and mach_absolute_time share a timebase; fit
            # the conversion using widely separated snapshots (no wall clock).
            first=data['events'][0];last=data['events'][-1]
            scale=(last['wall_ns']-first['wall_ns'])/(last['trace_clock']-first['trace_clock'])
            def monotonic(t):return first['wall_ns']+(t-first['trace_clock'])*scale
            buckets=defaultdict(lambda: {'count':0,'fault_elapsed_ns':0})
            page_sets=defaultdict(set)
            stage_spans=[(b,e) for b,e in spans if b['label'] in inner.STAGES]
            top_spans=[(b,e) for b,e in spans if b['label'] in ('load','finalize')]
            for start,end,addr,kind in faults:
                phase=next((b['label'] for b,e in stage_spans if b['trace_clock']<=start<e['trace_clock']),None)
                if phase is None:phase=next((b['label'] for b,e in top_spans if b['trace_clock']<=start<e['trace_clock']),'outside-import')
                category=classify(addr,monotonic(start))
                key=(phase,category,kind);b=buckets[key];b['count']+=1;b['fault_elapsed_ns']+=(end-start)*scale
                page_sets[(phase,category)].add(addr//meta['page_size'])
            record['trace']={'faults':len(faults),'capture_errors':errors,'other_pids':other,'artifact_mappings':n,'clock_scale':scale,
                'buckets':[dict(phase=k[0],category=k[1],fault_type=k[2],**v) for k,v in sorted(buckets.items())],
                'distinct_virtual_pages':[dict(phase=k[0],category=k[1],pages=len(v)) for k,v in sorted(page_sets.items())],
                'classification':'Artifact mmap results with creation timestamps; other categories use endpoint maps, not complete mapping lifetimes.',
                'duration':'macOS fault entry-to-return elapsed time, including preemption; Linux duration unavailable.'}
            if any(errors.values()):record['trace']['incomplete']=True
        report['runs'].append(record)
    return report


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);p.add_argument('--output',type=Path);a=p.parse_args()
    result=summarize(a.directory);text=json.dumps(result,indent=2)+'\n'
    if a.output:a.output.write_text(text)
    else:print(text,end='')
