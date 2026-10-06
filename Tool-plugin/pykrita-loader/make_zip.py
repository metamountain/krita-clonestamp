r"""Build clonestamp_tool-<krita>-windows-x64.zip for Tools > Scripts > Import Python Plugin.

    python make_zip.py <path\to\kritatoolclonestamp.dll> <out.zip>

Krita's importer (plugins/python/plugin_importer/plugin_importer.py) needs an
explicit directory entry "clonestamp_tool/" in the archive -- zipfile does not
write directory entries on its own, and without it the import fails with
"No plugins found in archive".
"""
import os
import sys
import zipfile

here = os.path.dirname(os.path.abspath(__file__))
dll, out = sys.argv[1], sys.argv[2]
with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
    z.writestr("clonestamp_tool/", "")
    z.writestr("clonestamp_tool/lib/", "")
    z.write(os.path.join(here, "clonestamp_tool.desktop"), "clonestamp_tool.desktop")
    z.write(os.path.join(here, "clonestamp_tool", "__init__.py"), "clonestamp_tool/__init__.py")
    z.write(os.path.join(here, "clonestamp_tool", "manual.html"), "clonestamp_tool/manual.html")
    z.write(dll, "clonestamp_tool/lib/kritatoolclonestamp.dll")
print(out)
