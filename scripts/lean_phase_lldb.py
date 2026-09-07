"""LLDB phase boundaries for an unmodified Lean binary. Imported by lldb, not Python."""
import json
import os
from pathlib import Path
import struct
import time
import lldb
from process_usage import usage

NAMES = ['open_count', 'open_ns', 'header_read_count', 'header_read_ns', 'header_read_bytes',
         'mmap_count', 'mmap_ns', 'mmap_bytes', 'mmap_failed', 'mmap_wrong_address',
         'fallback_count', 'copy_read_count', 'copy_read_ns', 'copy_read_bytes',
         'fstat_count', 'fstat_ns', 'close_count', 'close_ns', 'untracked_fd_count']
entries = {}
returns = {}
events = []
errors = []
output = None
counter_address = None


def counters(process):
    global counter_address
    if counter_address is None:
        contexts = process.GetTarget().FindSymbols('lean_io_totals', lldb.eSymbolTypeData)
        if contexts.GetSize() != 1:
            raise RuntimeError(f'expected one lean_io_totals symbol, found {contexts.GetSize()}')
        counter_address = contexts.GetContextAtIndex(0).GetSymbol().GetStartAddress().GetLoadAddress(process.GetTarget())
    error = lldb.SBError()
    raw = process.ReadMemory(counter_address, 8*len(NAMES), error)
    if error.Fail() or len(raw) != 8*len(NAMES):
        raise RuntimeError(f'cannot read timing counters: {error}')
    return dict(zip(NAMES, struct.unpack('<'+'Q'*len(NAMES), raw)))


def save():
    output.write_text(json.dumps({'events': events, 'errors': errors}, indent=2)+'\n')


def run(debugger, spec_path):
    global output, entries, returns, events, errors, counter_address
    spec = json.loads(Path(spec_path).read_text())
    output = Path(spec['phase_output'])
    entries, returns, events, errors, counter_address = {}, {}, [], [], None
    debugger.SetAsync(False)
    debugger.HandleCommand('settings set target.disable-aslr false')
    debugger.HandleCommand('settings set target.skip-prologue false')
    target = debugger.CreateTarget(spec['command'][0])
    if not target.IsValid():
        raise RuntimeError('failed to create LLDB target')
    for phase, symbol in [('load', 'l_Lean_importModulesCore'), ('finalize', 'l_Lean_finalizeImport')]:
        bp = target.BreakpointCreateByName(symbol)
        entries[bp.GetID()] = phase
    launch = lldb.SBLaunchInfo(spec['command'][1:])
    env = dict(os.environ)
    env.update(spec['environment'])
    launch.SetEnvironmentEntries([k+'='+v for k,v in env.items()], True)
    launch.SetWorkingDirectory(os.getcwd())
    launch.SetLaunchFlags(launch.GetLaunchFlags() & ~lldb.eLaunchFlagDisableASLR)
    launch.AddOpenFileAction(0, '/dev/null', True, False)
    launch.AddOpenFileAction(1, spec['stdout'], False, True)
    launch.AddOpenFileAction(2, spec['stderr'], False, True)
    error = lldb.SBError()
    started = time.monotonic()
    process = target.Launch(launch, error)
    if error.Fail():
        errors.append(f'launch failed: {error}')
    # Handle stops synchronously. Registering a new Python breakpoint callback
    # inside another callback triggers an autogen-name KeyError in Apple's LLDB.
    while not errors and process.GetState() == lldb.eStateStopped:
        stopped_ns = time.monotonic_ns()
        thread = next((t for t in process if t.GetStopReason() == lldb.eStopReasonBreakpoint), None)
        if thread is None or thread.GetStopReasonDataCount() < 2:
            errors.append('unexpected non-breakpoint stop: '+str(process.GetSelectedThread().GetStopDescription(1024)))
            break
        bp_id = thread.GetStopReasonDataAtIndex(0)
        try:
            if bp_id in entries:
                caller = thread.GetFrameAtIndex(1)
                if not caller.IsValid() or caller.GetPC() == lldb.LLDB_INVALID_ADDRESS:
                    raise RuntimeError('cannot unwind phase return address')
                bp = target.BreakpointCreateByAddress(caller.GetPC())
                bp.SetThreadID(thread.GetThreadID())
                event = {'phase': entries[bp_id], 'thread_id': thread.GetThreadID(),
                         'pid': process.GetProcessID(), 'entry_ns': stopped_ns,
                         'entry_counters': counters(process), 'return_address': caller.GetPC()}
                event['entry_usage'] = usage(process.GetProcessID(), spec)
                returns[bp.GetID()] = event
                events.append(event)
                save()
                event['resume_ns'] = time.monotonic_ns()
                event['entry_handler_ns'] = event['resume_ns']-stopped_ns
            elif bp_id in returns:
                event = returns.pop(bp_id)
                target.BreakpointDelete(bp_id)
                event['return_ns'] = stopped_ns
                event['wall_seconds'] = (stopped_ns-event['resume_ns'])/1e9
                event['return_counters'] = counters(process)
                event['return_usage'] = usage(process.GetProcessID(), spec)
                event['usage_delta'] = {k:v-event['entry_usage'][k]
                                        for k,v in event['return_usage'].items()
                                        if k not in ('backend', 'clock_resolution_ns')}
                if any(v < 0 for k,v in event['usage_delta'].items() if k != 'resident_bytes'):
                    raise RuntimeError('non-monotonic process resource counter')
                event['counter_delta'] = {k:event['return_counters'][k]-event['entry_counters'][k] for k in NAMES}
                event['return_handler_ns'] = time.monotonic_ns()-stopped_ns
                save()
            else:
                raise RuntimeError(f'unexpected breakpoint {bp_id}')
            resumed = process.Continue()
            if resumed.Fail():
                errors.append('continue failed: '+str(resumed))
        except Exception as error:
            errors.append(str(error))
    if process.GetState() != lldb.eStateExited:
        errors.append(f'process did not exit normally: state={process.GetState()}')
        process.Kill()
    completed = {'events': events, 'errors': errors, 'exit_code': process.GetExitStatus(),
                 'pid': process.GetProcessID(), 'debugger_wall_seconds': time.monotonic()-started,
                 'note': 'Phase wall excludes entry handler body but includes debugger stop/resume transport; counters are sums of syscall elapsed times in the target. No ASLR disabling.'}
    output.write_text(json.dumps(completed, indent=2)+'\n')
    valid = not errors and completed['exit_code'] == 0 and not returns
    valid &= sorted(e['phase'] for e in events) == ['finalize', 'load']
    valid &= all('wall_seconds' in e for e in events)
    if not valid:
        raise RuntimeError('incomplete phase timing; inspect '+str(output))
