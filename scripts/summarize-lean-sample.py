#!/usr/bin/env python3
"""Summarize Apple's sample call tree without counting idle Lean threads as work."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re


def parse(text):
    body = text.split('Call graph:\n', 1)[1].split('\nTotal number in stack', 1)[0]
    nodes = []
    stack = []
    for line in body.splitlines():
        match = re.match(r'^([ +!|:]*)\b(\d+) (.+)$', line)
        if not match:
            if line.strip():
                raise ValueError(f'unrecognized call-graph row: {line}')
            continue
        indent, count, frame = len(match[1]), int(match[2]), match[3]
        while stack and nodes[stack[-1]]['indent'] >= indent:
            stack.pop()
        parent = stack[-1] if stack else None
        node = {'indent': indent, 'count': count, 'self': count,
                'frame': frame.split('  (in ', 1)[0], 'parent': parent}
        if parent is not None:
            nodes[parent]['self'] -= count
        nodes.append(node)
        stack.append(len(nodes)-1)
    if not nodes or any(n['self'] < 0 for n in nodes):
        raise ValueError('invalid counts in sample call tree')
    return nodes


def ancestry(nodes, index):
    frames = []
    while index is not None:
        node = nodes[index]
        frames.append(node['frame'])
        index = node['parent']
    return frames


def category(frames):
    if '__mmap' in frames:
        return 'artifact mmap' if 'lean_compacted_region_read' in frames else 'other mmap'
    if 'l_Lean_finalizeImport' in frames:
        if 'lean_mark_persistent' in frames:
            return 'mark persistent'
        if any('finalizePersistentExtensions' in f for f in frames):
            return 'initialize persistent extensions'
        if any('setImportedEntries' in f for f in frames):
            return 'load extension entries'
        return 'other finalizeImport (including constant tables)'
    if any('region_reader::' in f for f in frames):
        return 'compacted region reader'
    if frames[0] in ['read', '__open', 'open', 'fstat', 'stat', 'lstat', 'access', 'close']:
        return 'filesystem calls outside finalizeImport'
    if any(f in ['__psynch_cvwait', '__ulock_wait', 'kevent'] for f in frames):
        return 'wait'
    return 'other'


def summarize(nodes):
    threads = {}
    for index, node in enumerate(nodes):
        if not node['self']:
            continue
        frames = ancestry(nodes, index)
        thread = threads.setdefault(frames[-1], {'samples': 0, 'import_samples': 0,
                                               'categories': Counter(), 'leaves': Counter()})
        weight = node['self']
        thread['samples'] += weight
        thread['import_samples'] += weight if 'l_Lean_importModules' in frames else 0
        thread['categories'][category(frames)] += weight
        thread['leaves'][frames[0]] += weight
    for root in (n for n in nodes if n['parent'] is None):
        if threads[root['frame']]['samples'] != root['count']:
            raise ValueError('thread sample totals are not conserved')
    candidates = [name for name, t in threads.items() if t['import_samples']]
    if not candidates:
        raise ValueError('no symbolized Lean import thread found')
    selected = max(candidates, key=lambda name: threads[name]['import_samples'])
    thread = threads[selected]
    return {'selected_thread': selected, 'selected_thread_samples': thread['samples'],
            'import_samples': thread['import_samples'],
            'categories': [{'name': k, 'samples': v, 'percent': 100*v/thread['samples']}
                           for k,v in thread['categories'].most_common()],
            'top_leaves': thread['leaves'].most_common(20),
            'threads': {name: {'samples': t['samples'], 'import_samples': t['import_samples']}
                        for name,t in threads.items()},
            'method': 'Disjoint call-tree self counts grouped by ancestry on the thread with most importModules samples. Wall-stack observations, not CPU percentages or exact seconds.'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('profile', type=Path)
    args = parser.parse_args()
    print(json.dumps(summarize(parse(args.profile.read_text())), indent=2))


if __name__ == '__main__':
    main()
