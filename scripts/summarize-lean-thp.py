#!/usr/bin/env python3
"""Report paired THP timing controls separately from page census and tracing."""
import argparse
import json
from pathlib import Path
from statistics import median


def distribution(xs):
    return {'median':median(xs),'min':min(xs),'max':max(xs),'values':xs}


def summarize(out):
    meta=json.loads((out/'metadata.json').read_text());records=json.loads((out/'records.json').read_text())
    repetitions=meta['repetitions'];expected=4+4*repetitions+4+(0 if meta['no_trace'] else 4)
    assert len(records)==expected and all(r['valid'] for r in records), 'incomplete experiment'
    assert json.loads((out/'thp-settings-after.json').read_text())==meta['thp_settings']
    result={'source_commit':meta['source_commit'],'system':meta['system'],'run_order':[r['name'] for r in records],
            'host_settings_unchanged':True,'comparisons':[],'census':[],'trace_diagnostics':[],'host_thp_deltas':[]}
    for r in records:
        assert r['process']['exit_code']==0 and not r['process']['timed_out']
        assert r['launch']['after']==int(r['mode']=='disabled')
        if r['kind']!='trace':assert r['launch']['pid']==r['process']['pid']
        def counters(path):return {k:int(v) for k,v in map(str.split,path.read_text().splitlines())}
        before=counters(out/(r['name']+'-before-vm-1.txt'));after=counters(out/(r['name']+'-after-vm-1.txt'))
        result['host_thp_deltas'].append({'name':r['name'],**{k:after[k]-before[k] for k in ['thp_fault_alloc','thp_fault_fallback','thp_collapse_alloc']}})
    for kind in ['stock','phase']:
        selected={mode:[r for r in records if r['kind']==kind and r['mode']==mode and not r['name'].endswith('-initial')] for mode in ['default','disabled']}
        assert all(len(v)==repetitions for v in selected.values())
        labels=['process'] if kind=='stock' else ['process','load','finalize','prepare_modules','private_tables','public_table','imported_entries','mark_persistent_before','initialize_extensions','mark_persistent_after']
        for label in labels:
            row={'kind':kind,'phase':label}
            metrics={}
            for mode,rs in selected.items():
                vals=[]
                for r in rs:
                    if label=='process':
                        p=r['process'];d={k:p[k] for k in ['wall_seconds','user_seconds','system_seconds','minor_faults','major_faults']}
                    else:
                        p=next(s['inclusive'] for s in r['inner']['spans'] if s['label']==label)
                        d={k+'_seconds':p[k+'_ns']/1e9 for k in ['wall','user','system']}
                        d.update({k:p[k] for k in ['minor_faults','major_faults']})
                    d['cpu_seconds']=d['user_seconds']+d['system_seconds'];vals.append(d)
                metrics[mode]={k:distribution([d[k] for d in vals]) for k in vals[0]}
            row.update(metrics)
            row['paired_wall_ratio']=distribution([metrics['disabled']['wall_seconds']['values'][i]/metrics['default']['wall_seconds']['values'][i] for i in range(repetitions)])
            result['comparisons'].append(row)
    for r in records:
        if r['kind']=='census':
            d=json.loads((out/(r['name']+'-events.json')).read_text())
            assert d['thp_disable_initial']==d['thp_disable_final']==int(r['mode']=='disabled')
            points=[{'kind':e['kind'],'phase':e['label'],'anon_huge_bytes':e['anon_huge_bytes'],'anonymous_bytes':e['anonymous_bytes'],'census_ns':e['census_ns']} for e in d['events'] if e['thp_valid']]
            assert len(points)==8
            if r['mode']=='disabled':assert all(p['anon_huge_bytes']==0 for p in points)
            result['census'].append({'name':r['name'],'mode':r['mode'],'points':points,'total_census_seconds':sum(p['census_ns'] for p in points)/1e9})
    result['default_huge_pages_observed']=any(p['anon_huge_bytes'] for r in result['census'] if r['mode']=='default' for p in r['points'])
    if not meta['no_trace']:
        faults=json.loads((out/'fault-summary.json').read_text())
        traces=[r for r in faults['runs'] if 'trace' in r];assert len(traces)==4
        for r in traces:
            assert not r['trace'].get('incomplete')
            result['trace_diagnostics'].append({'name':r['name'],'trace':r['trace']})
    return result


def markdown(result):
    lines=['# Process-only THP comparison','',f"Source: `{result['source_commit']}`. All values below exclude warmups.",
           'Stock and phase runs have no tracer or huge-page census. Census and traces are separate diagnostics.','',
           '| Measurement | Default median s | Disabled median s | Paired wall ratio range | Default minor faults | Disabled minor faults |',
           '|---|---:|---:|---:|---:|---:|']
    for r in result['comparisons']:
        a=r['default'];b=r['disabled'];ratio=r['paired_wall_ratio']
        lines.append(f"| {r['kind']}: {r['phase']} | {a['wall_seconds']['median']:.6f} | {b['wall_seconds']['median']:.6f} | {ratio['min']:.3f}–{ratio['max']:.3f} | {a['minor_faults']['median']:g} | {b['minor_faults']['median']:g} |")
    lines+=['','| Census run | Phase boundary | Anonymous MiB | AnonHugePages MiB | Census ms |','|---|---|---:|---:|---:|']
    for r in result['census']:
        for p in r['points']:
            lines.append(f"| {r['name']} | {p['kind']} {p['phase']} | {p['anonymous_bytes']/1048576:.3f} | {p['anon_huge_bytes']/1048576:.3f} | {p['census_ns']/1e6:.3f} |")
    lines+=['','| Host counters during diagnostic | THP fault allocations | THP fault fallbacks |','|---|---:|---:|']
    for r in result['host_thp_deltas']:
        if r['name'].startswith('trace'):
            lines.append(f"| {r['name']} | {r['thp_fault_alloc']} | {r['thp_fault_fallback']} |")
    lines+=['','Host counters are not target-attributed. Host THP settings remained unchanged. AnonHugePages is a resident snapshot, not cumulative allocation or every possible THP size.']
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('directory',type=Path);a=p.parse_args()
    result=summarize(a.directory)
    (a.directory/'thp-summary.json').write_text(json.dumps(result,indent=2)+'\n')
    (a.directory/'thp-summary.md').write_text(markdown(result))
    print(markdown(result))
