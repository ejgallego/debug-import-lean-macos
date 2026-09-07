#!/usr/bin/env python3
"""Validate nested phase snapshots and report inclusive and exclusive resources."""
import argparse
import json
from pathlib import Path

METRICS = ('wall_ns','user_ns','system_ns','minor_faults','major_faults',
           'input_blocks','voluntary_switches','involuntary_switches')
STAGES = ('prepare_modules','private_tables','public_table','initial_extension_states',
          'assemble_base','imported_entries','mark_persistent_before','initialize_extensions',
          'mark_persistent_after')


def summarize(data):
    if data['overflow_or_error']:
        raise ValueError('target reported event overflow or resource error')
    stack=[]; spans=[]; counts={}
    for event in data['events']:
        if event['kind']=='C':
            if event['label'] in counts: raise ValueError('duplicate count')
            counts[event['label']]=event['value']
        elif event['kind']=='B':
            stack.append((event,{k:0 for k in METRICS}))
        elif event['kind']=='E':
            if not stack: raise ValueError('end without begin')
            start,children=stack.pop()
            if start['label']!=event['label']: raise ValueError('unbalanced phase nesting')
            inclusive={k:event[k]-start[k] for k in METRICS}
            exclusive={k:inclusive[k]-children[k] for k in METRICS}
            if any(v<0 for v in inclusive.values()) or any(v<0 for v in exclusive.values()):
                raise ValueError('negative phase counter')
            if stack:
                for k in METRICS:stack[-1][1][k]+=inclusive[k]
            spans.append({'label':event['label'],'parent':stack[-1][0]['label'] if stack else None,
                          'inclusive':inclusive,'exclusive':exclusive})
        else:raise ValueError('unknown event kind')
    if stack:raise ValueError('unfinished phases')
    if [s['label'] for s in spans if s['parent'] is None]!=['load','finalize']:
        raise ValueError('expected one complete load and finalize')
    if tuple(s['label'] for s in spans if s['parent']=='finalize')!=STAGES:
        raise ValueError('incomplete finalization stage coverage')
    if set(counts)!={'modules','private_constants','public_constants','extra_constant_names','initial_extensions'}:
        raise ValueError('missing workload cardinalities')
    if any(v<=0 for v in counts.values()):raise ValueError('empty workload cardinality')
    extensions=[s for s in spans if s['label'].startswith('extension:')]
    if len(extensions)<counts['initial_extensions'] or not any(s['label']=='run_init_attributes' for s in spans):
        raise ValueError('missing extension initialization work')
    return {'counts':counts,'extension_count':len(extensions),'spans':spans}


def markdown(summary):
    lines=['| Phase | Wall s | User s | Kernel s | Minor faults | Major faults |',
           '|---|---:|---:|---:|---:|---:|']
    top=[s for s in summary['spans'] if not s['parent'] or s['parent']=='finalize']
    extensions=sorted((s for s in summary['spans'] if s['label'].startswith('extension:')),
                      key=lambda s:s['inclusive']['wall_ns'],reverse=True)[:12]
    for s in top+extensions:
        d=s['inclusive']
        lines.append(f"| {s['label']} | {d['wall_ns']/1e9:.6f} | {d['user_ns']/1e9:.6f} | {d['system_ns']/1e9:.6f} | {d['minor_faults']} | {d['major_faults']} |")
    return '\n'.join(lines)+'\n'


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('input',type=Path)
    p.add_argument('--output',type=Path);args=p.parse_args()
    s=summarize(json.loads(args.input.read_text()))
    if args.output:args.output.write_text(json.dumps(s,indent=2)+'\n')
    print(json.dumps(s['counts']));print(markdown(s))
