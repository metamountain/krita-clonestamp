"""Check a plugin zip with Krita's own importer code (plugin_importer.py), outside Krita.

    python tools/check_plugin_zip.py <zip> [<zip> ...]

Prints OK / FAIL per zip. Run before every release: a zip that unpacks fine can still be
rejected by Krita ("No plugins found in archive"), e.g. without directory entries.
"""
import sys, types, os, tempfile, importlib.util
# stand-ins for Krita-only imports used by plugin_importer.py
krita = types.ModuleType("krita"); krita.Krita = None
sys.modules["krita"] = krita
builtins_i18n = lambda s: s
import builtins; builtins.i18n = builtins_i18n
for m in ("PyQt6", "PyQt6.QtCore"):
    sys.modules.setdefault(m, types.ModuleType(m))
sys.modules["PyQt6.QtCore"].QStandardPaths = None
src = r"C:\Program Files\Krita (x64)\share\krita\pykrita\plugin_importer\plugin_importer.py"
spec = importlib.util.spec_from_file_location("plugin_importer", src)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
for z in sys.argv[1:]:
    tmp = tempfile.mkdtemp(); os.makedirs(os.path.join(tmp, "pykrita")); os.makedirs(os.path.join(tmp, "actions"))
    imp = mod.PluginImporter(z, tmp, lambda p: True)
    try:
        names = [p["name"] for p in imp.import_all()]
        files = sorted(os.path.relpath(os.path.join(r, f), tmp) for r, _, fs in os.walk(tmp) for f in fs)
        print("OK  ", os.path.basename(z), names, files)
    except Exception as e:
        print("FAIL", os.path.basename(z), type(e).__name__, e)
