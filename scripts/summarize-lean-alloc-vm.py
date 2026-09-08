#!/usr/bin/env python3
"""Attribute Mac VM calls and zero-fill faults to observed mapping histories."""
import argparse
from bisect import bisect_right
from collections import Counter,defaultdict
import importlib.util
import json
from pathlib import Path


def load_module(name,path):
    s=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m


class Histories:
    """Disjoint address intervals; preserve purge history until unmap/remap."""
    def __init__(self):self.rows=[];self.starts=[]
    def get(self,address):
        i=bisect_right(self.starts,address)-1
        return self.rows[i][2] if i>=0 and address<self.rows[i][1] else {}
    def apply(self,a,b,kind):
        points=sorted({a,b}|{v for x,y,_ in self.rows for v in (x,y) if a<v<b})
        keep=[]
        for x,y,state in self.rows:
            if y<=a or x>=b:keep.append((x,y,state));continue
            if x<a:keep.append((x,a,state))
            if y>b:keep.append((b,y,state))
        for x,y in zip(points,points[1:]):
            old=self.get(x)
            if kind=='map':state={'map_seen':True,'purge_seen':False}
            elif kind=='unmap':state={}
            elif kind=='purge':state={**old,'purge_seen':True}
            else:state=old
            if state:keep.append((x,y,state))
        self.rows=sorted(keep);self.starts=[x for x,_,_ in self.rows]


def summarize(out):
    helpers=load_module('faults',Path(__file__).with_name('summarize-lean-faults.py'))
    meta=json.loads((out/'metadata.json').read_text());assert 'macOS' in meta['system']
    symbols=[]
    for line in (out/'lean-symbols.txt').read_text().splitlines():
        f=line.split()
        if len(f)==3:
            try: symbols.append((int(f[0],16),f[2]))
            except ValueError:pass
    symbols.sort();addresses=[s[0] for s in symbols]
    base=next(a for a,n in symbols if n=='__mh_execute_header')
    fixture=json.loads((out/'fixture.stdout').read_text());release=fixture['release']
    result={'source_commit':meta['source_commit'],'runs':[]}
    for path in sorted(out.glob('*-alloc.json')):
        if path.name.startswith('fixture'):continue
        name=path.name[:-len('-alloc.json')];data=json.loads(path.read_text());assert not data['overflow']
        phases=json.loads((out/(name+'-events.json')).read_text());assert data['pid']==phases['pid']
        spans=helpers.intervals(phases);ends=max(e['trace_clock'] for b,e in spans)
        top=[(b,e) for b,e in spans if b['label'] in ('load','finalize')]
        stages=[(b,e) for b,e in spans if b['label'] in ('prepare_modules','private_tables','public_table','imported_entries','initialize_extensions','mark_persistent_before','mark_persistent_after')]
        def phase(t):
            return next((b['label'] for b,e in stages+top if b['trace_clock']<=t<e['trace_clock']),'outside-import')
        scale=data['timebase_numer']/data['timebase_denom']
        groups=defaultdict(lambda:{'calls':0,'requested_bytes':0,'elapsed_ns':0,'errors':0})
        for e in data['events']:
            symbol=e.get('symbol','')
            if e.get('image','').endswith('/bin/lean'):
                pc=e['caller']-e['image_base']+base;i=bisect_right(addresses,pc)-1
                if i>=0:symbol=symbols[i][1]
            key=(phase(e['begin']),e['kind'],e['arg'],symbol)
            g=groups[key];g['calls']+=1;g['requested_bytes']+=e['size'];g['elapsed_ns']+=(e['end']-e['begin'])*scale;g['errors']+=int(e['error']!=0)
        r={'name':name,'operation_groups':[dict(phase=k[0],kind=k[1],arg=k[2],caller_symbol=k[3],**v) for k,v in sorted(groups.items())]}
        if name.startswith('trace'):
            details=[]
            faults,errors,_=helpers.mac_faults(out/(name+'.stdout'),data['pid'],details);assert not any(errors.values())
            by_phase=defaultdict(list)
            for f in details:
                if f['kind']=='zero-fill' and f['begin']<ends:by_phase[phase(f['begin'])].append(f)
            r['zero_fill_addresses']=[]
            for label,fs in sorted(by_phase.items()):
                pages=Counter(f['address']//meta['page_size']*meta['page_size'] for f in fs)
                r['zero_fill_addresses'].append(dict(phase=label,events=len(fs),distinct_pages=len(pages),
                    return_codes=dict(Counter(str(f['return_code']) for f in fs)),
                    kernel_map_events=sum(f['kernel_map']!=0 for f in fs),
                    handler_seconds=sum(f['end']-f['begin'] for f in fs)*scale/1e9,
                    most_frequent_pages=[dict(address=hex(a),events=n) for a,n in pages.most_common(5)]))
            operations=iter(sorted((e for e in data['events'] if not e['error'] and e['end']<ends),key=lambda e:e['end']))
            op=next(operations,None);history=Histories();counts=Counter()
            for begin,end,address,kind in sorted(faults):
                if begin>=ends:break
                while op is not None and op['end']<=begin:
                    effect='map' if op['kind'] in (1,5,7) else 'unmap' if op['kind'] in (2,6) else 'purge' if op['kind']==4 and op['arg'] in (fixture['dontneed'],fixture['free'],release) else None
                    if effect:history.apply(op['address'],op['address']+op['size'],effect)
                    op=next(operations,None)
                if kind!='zero-fill':continue
                state=history.get(address)
                origin='after-observed-purge' if state.get('purge_seen') else 'observed-map-without-purge' if state.get('map_seen') else 'unknown-history'
                counts[(phase(begin),origin)]+=1
            r['zero_fill_history']=[dict(phase=k[0],history=k[1],count=v) for k,v in sorted(counts.items())]
        result['runs'].append(r)
    result['limits']='Observed libc and selected Mach VM entry points; internal calls may bypass interposition, and kernel reclamation is not observed. History begins after observer initialization and applies completed operations; faults inside an operation need separate interval correlation. Older captures contain only libc calls. Counts after purge are temporal/address associations, not a causal latency estimate. Requested bytes may revisit pages; distinct virtual pages are not distinct physical allocations.'
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    (a.directory/'alloc-summary.json').write_text(json.dumps(summarize(a.directory),indent=2)+'\n')
