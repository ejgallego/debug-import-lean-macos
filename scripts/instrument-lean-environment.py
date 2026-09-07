#!/usr/bin/env python3
"""Insert diagnostic IO boundaries into the exact pinned Lean.Environment source."""
import argparse
from pathlib import Path

PIN = '58774429865502f05c63239266aac30ef1e91ef7'


def instrument(source):
    def replace(old, new):
        nonlocal source
        if source.count(old) != 1:
            raise RuntimeError(f'expected one source anchor: {old[:100]!r}')
        source = source.replace(old, new)

    replace('namespace Lean\n', '''namespace Lean

@[extern "lean_import_phase_begin"]
opaque importPhaseBegin (label : @& String) : BaseIO Unit
@[extern "lean_import_phase_end"]
opaque importPhaseEnd (label : @& String) : BaseIO Unit
@[extern "lean_import_phase_count"]
opaque importPhaseCount (label : @& String) (count : UInt64) : BaseIO Unit
''')
    replace('    let (_, s) ← importModulesCore (globalLevel := level) imports arts |>.run\n    finalizeImport (leakEnv := leakEnv) (loadExts := loadExts) (level := level)\n      s imports opts trustLevel', '''    importPhaseBegin "load"
    let (_, s) ← importModulesCore (globalLevel := level) imports arts |>.run
    importPhaseEnd "load"
    importPhaseBegin "finalize"
    let env ← finalizeImport (leakEnv := leakEnv) (loadExts := loadExts) (level := level)
      s imports opts trustLevel
    importPhaseEnd "finalize"
    return env''')
    replace('  let modules := s.moduleNames.filterMap', '  importPhaseBegin "prepare_modules"\n  let modules := s.moduleNames.filterMap')
    replace('  let mut const2ModIdx : Std.HashMap Name ModuleIdx :=', '''  importPhaseCount "modules" modules.size.toUInt64
  importPhaseCount "private_constants" numPrivateConsts.toUInt64
  importPhaseCount "public_constants" numPublicConsts.toUInt64
  importPhaseCount "extra_constant_names" numExtraConsts.toUInt64
  importPhaseEnd "prepare_modules"
  importPhaseBegin "private_tables"
  let mut const2ModIdx : Std.HashMap Name ModuleIdx :=''')
    replace('  if isModule then\n    for mod in modules.filter', '  importPhaseEnd "private_tables"\n  importPhaseBegin "public_table"\n  if isModule then\n    for mod in modules.filter')
    replace('  let exts ← mkInitialExtensionStates\n  let privateConstants', '''  importPhaseEnd "public_table"
  importPhaseBegin "initial_extension_states"
  let exts ← mkInitialExtensionStates
  importPhaseCount "initial_extensions" exts.size.toUInt64
  importPhaseEnd "initial_extension_states"
  importPhaseBegin "assemble_base"
  let privateConstants''')
    replace('  let extensions ← setImportedEntries privateBase.extensions moduleData', '''  importPhaseEnd "assemble_base"
  importPhaseBegin "imported_entries"
  let extensions ← setImportedEntries privateBase.extensions moduleData''')
    replace('  if leakEnv then\n    /- Mark persistent', '  importPhaseEnd "imported_entries"\n  importPhaseBegin "mark_persistent_before"\n  if leakEnv then\n    /- Mark persistent')
    replace('  if loadExts then\n    env ← finalizePersistentExtensions env moduleData opts', '''  importPhaseEnd "mark_persistent_before"
  importPhaseBegin "initialize_extensions"
  if loadExts then
    env ← finalizePersistentExtensions env moduleData opts
  importPhaseEnd "initialize_extensions"
  importPhaseBegin "mark_persistent_after"
  if loadExts then''')
    replace('  return { env with importRealizationCtx? := some {', '  importPhaseEnd "mark_persistent_after"\n  return { env with importRealizationCtx? := some {')
    replace('      let extDescr := pExtDescrs[i]\n', '''      let extDescr := pExtDescrs[i]
      let phaseLabel := "extension:" ++ extDescr.name.toString
      importPhaseBegin phaseLabel
''')
    replace('      loop (i + 1) env\n', '      importPhaseEnd phaseLabel\n      loop (i + 1) env\n')
    replace('        runInitAttrs env opts\n', '''        importPhaseBegin "run_init_attributes"
        runInitAttrs env opts
        importPhaseEnd "run_init_attributes"
''')
    return source


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source', type=Path)
    p.add_argument('output', type=Path)
    args = p.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(instrument(args.source.read_text()))
