"""Read process counters externally; CPU is normalized to nanoseconds."""
import json
import os
from pathlib import Path
import subprocess
import sys
import time


def usage(pid, spec):
    if sys.platform == 'darwin':
        return json.loads(subprocess.check_output([spec['usage_helper'], str(pid)], text=True))
    fields = Path(f'/proc/{pid}/stat').read_text().rsplit(')', 1)[1].split()
    tick_ns = 1_000_000_000 // os.sysconf('SC_CLK_TCK')
    return {'backend': '/proc/PID/stat', 'clock_resolution_ns': tick_ns,
            'user_ns': int(fields[11])*tick_ns, 'system_ns': int(fields[12])*tick_ns,
            'minor_faults': int(fields[7]), 'major_faults': int(fields[9]),
            'resident_bytes': int(fields[21])*os.sysconf('SC_PAGE_SIZE')}


def check_units(spec):
    before = usage(os.getpid(), spec)
    start = time.process_time()
    while time.process_time() - start < 0.3:
        pass
    expected = time.process_time() - start
    after = usage(os.getpid(), spec)
    observed = sum(after[k]-before[k] for k in ('user_ns','system_ns'))/1e9
    if not 0.8 < observed/expected < 1.2:
        raise RuntimeError(f'CPU counter unit check failed: {observed=} {expected=}')
    return {'backend':after['backend'], 'clock_resolution_ns':after['clock_resolution_ns'],
            'observer_cpu_seconds':observed, 'process_time_seconds':expected}
