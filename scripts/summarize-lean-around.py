#!/usr/bin/env python3
"""Summarize the paired Linux file fault-around intervention."""
import argparse
import json
from pathlib import Path
from statistics import median


def summarize(out):
    meta=json.loads((out/'metadata.json').read_text());assert meta['system'].startswith('Linux')
    records=json.loads((out/'records.json').read_text());assert len(records)==24 and all(r['valid'] for r in records)
    restored=json.loads((out/'fault-around-restored.json').read_text())
    assert restored['original']==restored['restored']==meta['fault_around_original']
    for r in records:
        assert r['process']['exit_code']==0 and not r['process']['timed_out']
        assert r['fault_around_bytes']==(meta['fault_around_original'] if r['mode']=='default' else meta['page_size'])
    result={'source_commit':meta['source_commit'],'restored':restored,'comparisons':[],'trace_counts':[]}
    def distribution(xs):return {'median':median(xs),'min':min(xs),'max':max(xs),'values':xs}
    for kind in ['stock','phase']:
        rs={m:[r for r in records if r['kind']==kind and r['mode']==m and not r['name'].endswith('-initial')] for m in ['default','single']}
        assert all(len(v)==4 for v in rs.values())
        phases=['process'] if kind=='stock' else ['process','load','finalize','private_tables','public_table','imported_entries','mark_persistent_before','initialize_extensions','mark_persistent_after']
        for label in phases:
            row={'kind':kind,'phase':label}
            for mode,runs in rs.items():
                metrics=[]
                for r in runs:
                    if label=='process':
                        d=r['process'];m={k:d[k] for k in ['wall_seconds','user_seconds','system_seconds','minor_faults','major_faults']}
                    else:
                        d=next(s['inclusive'] for s in r['inner']['spans'] if s['label']==label)
                        m={k+'_seconds':d[k+'_ns']/1e9 for k in ['wall','user','system']};m.update({k:d[k] for k in ['minor_faults','major_faults']})
                    m['cpu_seconds']=m['user_seconds']+m['system_seconds'];metrics.append(m)
                row[mode]={k:distribution([m[k] for m in metrics]) for k in metrics[0]}
            row['paired_wall_ratio']=distribution([b/a for a,b in zip(row['default']['wall_seconds']['values'],row['single']['wall_seconds']['values'])])
            result['comparisons'].append(row)
    faults=json.loads((out/'fault-summary.json').read_text())
    traces=[r for r in faults['runs'] if 'trace' in r];assert len(traces)==4
    for r in traces:
        t=r['trace'];assert not t.get('incomplete')
        result['trace_counts'].append({'name':r['name'],'faults':t['faults'],'phase_count_checks':t['phase_count_checks'],'buckets':t['buckets']})
    return result


def markdown(s):
    lines=['# File fault-around comparison','',f"Source `{s['source_commit']}`; original window {s['restored']['original']} bytes, restored after the experiment.",'',
           '| Measurement | Default median s | One-page median s | Paired time ratio range | Default minor faults | One-page minor faults |','|---|---:|---:|---:|---:|---:|']
    for r in s['comparisons']:
        a=r['default'];b=r['single'];p=r['paired_wall_ratio']
        lines.append(f"| {r['kind']}: {r['phase']} | {a['wall_seconds']['median']:.6f} | {b['wall_seconds']['median']:.6f} | {p['min']:.3f}–{p['max']:.3f} | {a['minor_faults']['median']:g} | {b['minor_faults']['median']:g} |")
    lines+=['','Initial runs excluded; stock and phase timings have no tracer. The knob affects all file mappings on this disposable runner, including native images. Trace diagnostics are separate.']
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args();s=summarize(a.directory)
    (a.directory/'around-summary.json').write_text(json.dumps(s,indent=2)+'\n')
    (a.directory/'around-summary.md').write_text(markdown(s));print(markdown(s))
