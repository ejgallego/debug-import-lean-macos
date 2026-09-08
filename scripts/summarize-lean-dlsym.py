#!/usr/bin/env python3
"""Correlate real Lean symbol lookups with VM fault intervals."""
import argparse
from bisect import bisect_right
from collections import Counter,defaultdict
import importlib.util
import json
from pathlib import Path


def summarize(out):
    spec=importlib.util.spec_from_file_location('faults',Path(__file__).with_name('summarize-lean-faults.py'))
    helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
    meta=json.loads((out/'metadata.json').read_text());result={'source_commit':meta['source_commit'],'runs':[]}
    for path in sorted(out.glob('*-dlsym.json')):
        name=path.name[:-len('-dlsym.json')];data=json.loads(path.read_text());assert not data['overflow']
        phase_data=json.loads((out/(name+'-events.json')).read_text());assert data['pid']==phase_data['pid']
        spans=helpers.intervals(phase_data)
        selected=sorted(spans,key=lambda s:s[1]['trace_clock']-s[0]['trace_clock'])
        def phase(t):return next((b['label'] for b,e in selected if b['trace_clock']<=t<e['trace_clock']),'outside-import')
        es=sorted(data['events']);starts=[e[0] for e in es];scale=data['timebase_numer']/data['timebase_denom']/1e9
        assert all(es[i][1]<=es[i+1][0] for i in range(len(es)-1)), 'overlapping calls require thread-aware correlation'
        groups=defaultdict(lambda:{'calls':0,'seconds':0.0})
        for b,e,found in es:
            g=groups[(phase(b),bool(found))];g['calls']+=1;g['seconds']+=(e-b)*scale
        r={'name':name,'lookups':[dict(phase=k[0],found=k[1],**v) for k,v in sorted(groups.items())]}
        r['stages']=[]
        for b,e in spans:
            if b['label'] not in ('load','finalize','private_tables','initialize_extensions','run_init_attributes'):continue
            calls=[x for x in es if b['trace_clock']<=x[0]<e['trace_clock']]
            wall=(e['trace_clock']-b['trace_clock'])*scale
            r['stages'].append(dict(phase=b['label'],wall_seconds=wall,
                failed_calls=sum(not x[2] for x in calls),successful_calls=sum(bool(x[2]) for x in calls),
                failed_seconds=sum(x[1]-x[0] for x in calls if not x[2])*scale,
                successful_seconds=sum(x[1]-x[0] for x in calls if x[2])*scale))
        if name.startswith('trace'):
            details=[];_,errors,_=helpers.mac_faults(out/(name+'.stdout'),data['pid'],details);assert not any(errors.values())
            counts=Counter();pages=defaultdict(Counter);matched=Counter();zero_per_call=Counter()
            for f in details:
                i=bisect_right(starts,f['begin'])-1
                within=i>=0 and f['end']<=es[i][1]
                association=('successful-lookup' if es[i][2] else 'failed-lookup') if within else 'outside-lookup'
                label=phase(f['begin']);counts[(label,association,f['kind'],f['return_code'])]+=1
                if within:matched[i]+=1
                if within and f['kind']=='zero-fill' and f['return_code']==0:zero_per_call[i]+=1
                if f['kind']=='zero-fill':pages[(label,association)][f['address']]+=1
            r['faults']=[dict(phase=k[0],association=k[1],kind=k[2],return_code=k[3],count=v) for k,v in sorted(counts.items())]
            r['zero_fill_pages']=[dict(phase=k[0],association=k[1],distinct_pages=len(v),top=[dict(address=hex(a),count=n) for a,n in v.most_common(3)]) for k,v in sorted(pages.items())]
            r['calls_with_faults']=len(matched)
            r['zero_fill_per_call']=[dict(found=found,successful_zero_fills_per_call=n,calls=c)
                for (found,n),c in sorted(Counter((bool(e[2]),zero_per_call[i]) for i,e in enumerate(es)).items())]
        result['runs'].append(r)
    result['limits']='Only Lean interpreter RTLD_DEFAULT lookups are wrapped. Interval attribution requires nonoverlapping calls; VM events may include unrelated work on another thread. Inclusive lookup times include all lookup work, not just fault handling. Traced timing is diagnostic.'
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    (a.directory/'dlsym-summary.json').write_text(json.dumps(summarize(a.directory),indent=2)+'\n')
