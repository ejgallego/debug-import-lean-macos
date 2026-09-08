#!/usr/bin/env python3
"""Validate the standalone dlsym reduction and its optional Mac VM trace."""
import argparse
from collections import Counter
import importlib.util
import json
from pathlib import Path
import statistics


def summarize(out):
    result={'system':(out/'system.txt').read_text(),'modes':{}}
    for mode in ('hit','miss','unique'):
        rows=[json.loads(p.read_text()) for p in sorted(out.glob(mode+'-*.json'))]
        assert len(rows)==3
        assert all(r['mode']==mode and r['count']==77742 and r['found']==(77742 if mode=='hit' else 0) for r in rows)
        result['modes'][mode]={'runs':rows,'median_seconds':statistics.median(r['wall_seconds'] for r in rows)}
    trace=out/'trace.txt'
    if trace.exists():
        meta=next(json.loads(s) for s in trace.open() if s.startswith('{'))
        spec=importlib.util.spec_from_file_location('faults',Path(__file__).with_name('summarize-lean-faults.py'))
        helpers=importlib.util.module_from_spec(spec);spec.loader.exec_module(helpers)
        details=[];_,errors,_=helpers.mac_faults(trace,meta['pid'],details);assert not any(errors.values())
        fs=[f for f in details if meta['trace_begin']<=f['begin']<meta['trace_end']]
        assert len(fs)==meta['minor_faults']+meta['major_faults'], 'trace does not reconcile with the loop counters'
        result['trace']={'process':meta,'errors':errors,'types':dict(Counter(f['kind'] for f in fs)),
                         'return_codes':dict(Counter(f['return_code'] for f in fs)),
                         'distinct_addresses':len({f['address'] for f in fs}),
                         'top_addresses':Counter(hex(f['address']) for f in fs).most_common(5)}
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    (a.directory/'summary.json').write_text(json.dumps(summarize(a.directory),indent=2)+'\n')
