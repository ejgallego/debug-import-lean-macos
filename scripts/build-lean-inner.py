#!/usr/bin/env python3
"""Relink the pinned Lean shell and release archives, rebuilding only Environment."""
import argparse
import difflib
import hashlib
import importlib.util
import json
from pathlib import Path
import platform
import subprocess
import sys

PIN='58774429865502f05c63239266aac30ef1e91ef7'

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True)
    p.add_argument('--libcxx-include',type=Path)
    p.add_argument('--dlsym',action='store_true',help='also rebuild the interpreter and time its RTLD_DEFAULT lookups')
    args=p.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=True)
    if (out/'build.json').exists():p.error('use a fresh build output')
    prefix=Path(subprocess.check_output(['lean','--print-prefix'],text=True).strip())
    src=args.source.resolve()/'src';original=src/'Lean/Environment.lean'
    installed=prefix/'src/lean/Lean/Environment.lean'
    if original.read_bytes()!=installed.read_bytes():raise RuntimeError('downloaded and installed Environment differ')
    version=subprocess.check_output([str(prefix/'bin/lean'),'--version'],text=True)
    if PIN not in version:raise RuntimeError('unexpected Lean version')
    spec=importlib.util.spec_from_file_location('instrument','scripts/instrument-lean-environment.py')
    mod=importlib.util.module_from_spec(spec);spec.loader.exec_module(mod)
    patched=out/'patched/Lean/Environment.lean';patched.parent.mkdir(parents=True)
    patched.write_text(mod.instrument(original.read_text()))
    (out/'Environment.patch').write_text(''.join(difflib.unified_diff(original.read_text().splitlines(True),patched.read_text().splitlines(True),fromfile='a/src/Lean/Environment.lean',tofile='b/src/Lean/Environment.lean')))
    (out/'githash.h').write_text(f'#define LEAN_GITHASH "{PIN}"\n')
    commands=[]
    def run(command):
        command=list(map(str,command));commands.append(command);print(' '.join(command),flush=True)
        subprocess.run(command,check=True)
    cxx=['clang++','-O3','-DNDEBUG','-DLEAN_MULTI_THREAD','-DLEAN_EXPORTING','-DLEAN_BUILD_TYPE="Release"','-std=c++20']
    if args.libcxx_include:cxx+=['-nostdinc++','-isystem',str(args.libcxx_include)]
    else:cxx+=['-stdlib=libc++']
    cxx+=['-I',src,'-I',prefix/'include','-I',prefix/'include/lean','-I',out]
    run(cxx+['-c',src/'util/shell.cpp','-o',out/'shell.o'])
    run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-DLEAN_EXPORTING','-I',prefix/'include','-c','repro/lean-inner-timing.c','-o',out/'inner-timing.o'])
    if args.dlsym:
        run(['cc','-O2','-std=c11','-Wall','-Wextra','-Werror','-c','repro/lean-dlsym-timing.c','-o',out/'dlsym-timing.o'])
    for name,lean_source,root in [('control',original,src),('instrumented',patched,patched.parents[1])]:
        generated=out/(name+'.c');obj=out/(name+'.o');dest=out/name
        run([prefix/'bin/lean','--root='+str(root),'-c',generated,lean_source])
        run([prefix/'bin/leanc','-O3','-DLEAN_EXPORTING','-c',generated,'-o',obj])
        (dest/'bin').mkdir(parents=True);(dest/'lib').symlink_to(prefix/'lib',target_is_directory=True)
        inputs=[src/'shell/lean.cpp',out/'shell.o',obj]
        if args.dlsym:
            ir=src/'library/ir_interpreter.cpp';text=ir.read_text()
            if name=='instrumented':
                assert text.count('return dlsym(RTLD_DEFAULT, sym);')==1
                text='extern "C" void *lean_observe_dlsym(char const *);\n'+text.replace('return dlsym(RTLD_DEFAULT, sym);','return lean_observe_dlsym(sym);')
            rebuilt=out/(name+'-ir.cpp');rebuilt.write_text(text)
            run(cxx+['-c',rebuilt,'-o',out/(name+'-ir.o')])
            inputs+=[out/(name+'-ir.o')]
            if name=='instrumented':inputs+=[out/'dlsym-timing.o']
        if name=='instrumented':inputs+=[out/'inner-timing.o']
        export='-Wl,-export_dynamic' if sys.platform=='darwin' else '-Wl,--export-dynamic'
        run([prefix/'bin/leanc','-O3',*inputs,export,'-o',dest/'bin/lean'])
        run([dest/'bin/lean','--version'])
    files=[original,patched,src/'util/shell.cpp',src/'shell/lean.cpp',Path('repro/lean-inner-timing.c'),Path('repro/lean-fault-regions.h'),
           Path(__file__),Path('scripts/instrument-lean-environment.py'),out/'control.c',out/'instrumented.c',
           out/'control/bin/lean',out/'instrumented/bin/lean',prefix/'bin/lean']
    files+=list((prefix/'lib/lean').glob('*.a'))
    if args.dlsym:files += [src/'library/ir_interpreter.cpp',out/'control-ir.cpp',out/'instrumented-ir.cpp',Path('repro/lean-dlsym-timing.c')]
    meta={'pin':PIN,'lean':version,'system':platform.platform(),'commands':commands,
          'stock':str(prefix/'bin/lean'),'control':str(out/'control/bin/lean'),
          'instrumented':str(out/'instrumented/bin/lean'),
          'compilers':{c:subprocess.check_output([c,'--version'],text=True) for c in ['cc','clang++']},
          'hashes':{str(f):hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    (out/'build.json').write_text(json.dumps(meta,indent=2)+'\n')

if __name__=='__main__':main()
