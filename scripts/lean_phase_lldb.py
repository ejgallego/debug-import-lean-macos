"""LLDB phase boundaries for an unmodified Lean binary. Imported by lldb, not Python."""
import json
import os
from pathlib import Path
import struct
import time
import traceback
import lldb

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


def entered(frame, location, _dict):
    start_callback = time.monotonic_ns()
    try:
        thread = frame.GetThread()
        process = thread.GetProcess()
        target = process.GetTarget()
        phase = entries[location.GetBreakpoint().GetID()]
        caller = thread.GetFrameAtIndex(1)
        if not caller.IsValid() or caller.GetPC() == lldb.LLDB_INVALID_ADDRESS:
            raise RuntimeError('cannot unwind phase return address')
        bp = target.BreakpointCreateByAddress(caller.GetPC())
        bp.SetThreadID(thread.GetThreadID())
        bp.SetOneShot(True)
        bp.SetScriptCallbackFunction(__name__+'.returned')
        event = {'phase': phase, 'thread_id': thread.GetThreadID(), 'pid': process.GetProcessID(),
                 'entry_ns': start_callback, 'entry_counters': counters(process),
                 'return_address': caller.GetPC(), 'entry_pc': frame.GetPC()}
        returns[bp.GetID()] = event
        events.append(event)
        save()
        event['resume_ns'] = time.monotonic_ns()
        event['entry_callback_ns'] = event['resume_ns']-start_callback
        return False
    except Exception:
        errors.append(traceback.format_exc()); save(); return True


def returned(frame, location, _dict):
    start_callback = time.monotonic_ns()
    try:
        event = returns.pop(location.GetBreakpoint().GetID())
        event['return_ns'] = start_callback
        event['wall_seconds'] = (start_callback-event['resume_ns'])/1e9
        event['return_counters'] = counters(frame.GetThread().GetProcess())
        event['counter_delta'] = {k:event['return_counters'][k]-event['entry_counters'][k] for k in NAMES}
        event['return_callback_ns'] = time.monotonic_ns()-start_callback
        save()
        return False
    except Exception:
        errors.append(traceback.format_exc()); save(); return True


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
        bp.SetScriptCallbackFunction(__name__+'.entered')
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
    elif process.GetState() != lldb.eStateExited:
        errors.append(f'unexpected process stop: state={process.GetState()}, {process.GetSelectedThread().GetStopDescription(1024)}')
        process.Kill()
    completed = {'events': events, 'errors': errors, 'exit_code': process.GetExitStatus(),
                 'pid': process.GetProcessID(), 'debugger_wall_seconds': time.monotonic()-started,
                 'note': 'Phase wall excludes entry callback body but includes debugger stop/resume transport; counters are sums of syscall elapsed times in the target. No ASLR disabling.'}
    output.write_text(json.dumps(completed, indent=2)+'\n')
    valid = not errors and completed['exit_code'] == 0 and not returns
    valid &= sorted(e['phase'] for e in events) == ['finalize', 'load']
    valid &= all('wall_seconds' in e for e in events)
    if not valid:
        raise RuntimeError('incomplete phase timing; inspect '+str(output))
