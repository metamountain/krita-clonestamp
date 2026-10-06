"""MCP server for Krita – talks to the kritamcp bridge plugin (127.0.0.1:50017).

Tools:
  ping            – is Krita running with the bridge loaded?
  execute_python  – run Python inside Krita (GUI thread, persistent namespace)
  screenshot      – PNG of the Krita main window as it appears on screen
  canvas_image    – PNG of the active document's merged image (scaled)
  get_errors      – uncaught Python exceptions caught inside Krita since start
  plugin_status   – Python plugins: enabled flag, loaded?, import errors

Start (Claude Code / Qwen): uv run --with "mcp<2" python krita_mcp.py
Bridge install: copy plugin/kritamcp* to %APPDATA%\\krita\\pykrita, enable it in
Settings > Configure Krita > Python Plugin Manager, restart Krita.
"""
import base64
import socket

from mcp.server.fastmcp import FastMCP, Image

HOST, PORT = "127.0.0.1", 50017
TERM = "\n<<END>>"
mcp = FastMCP("krita")

HINT = ("Krita not reachable. Start Krita with the 'KritaMCP Bridge' Python plugin "
        "enabled (Settings > Configure Krita > Python Plugin Manager).")


def _send(code: str, timeout: float = 120.0) -> tuple[str, str]:
    try:
        with socket.create_connection((HOST, PORT), timeout=5) as s:
            s.settimeout(timeout)
            s.sendall((code + TERM + "\n").encode("utf-8"))
            buf = b""
            while TERM.encode() not in buf:
                chunk = s.recv(1 << 20)
                if not chunk:
                    break
                buf += chunk
    except OSError as e:
        return "ERR", f"{HINT} ({e})"
    text = buf.decode("utf-8", "replace").split(TERM)[0]
    status, _, body = text.partition("\n")
    return status.strip(), body


def _png(code: str) -> Image:
    status, body = _send(code)
    if status != "OK":
        raise RuntimeError(body)
    return Image(data=base64.b64decode(body.strip()), format="png")


_B64_OF = """
from PyQt6.QtCore import QBuffer, QIODevice
import base64 as _b64
def _b64png(img):
    buf = QBuffer(); buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG"); return _b64.b64encode(bytes(buf.data())).decode()
"""


@mcp.tool()
def ping() -> str:
    """Checks whether Krita is running with the KritaMCP bridge; returns version and open documents."""
    status, body = _send("k = Krita.instance()\n"
                         "f'Krita {k.version()} | docs: {[d.name() for d in k.documents()]}'")
    return f"{status}: {body}"


@mcp.tool()
def execute_python(code: str) -> str:
    """Runs Python inside Krita on the GUI thread. `Krita` is predefined; the namespace persists
    between calls. stdout is returned; a trailing expression is printed like at a REPL.
    Krita 6 uses PyQt6 (scoped enums, e.g. Qt.MouseButton.LeftButton)."""
    status, body = _send(code)
    return f"{status}\n{body}"


@mcp.tool()
def screenshot() -> Image:
    """PNG of the Krita main window exactly as on screen (includes the OpenGL canvas, dockers,
    cursor overlays drawn by plugins)."""
    return _png(_B64_OF + """
from PyQt6.QtWidgets import QApplication
w = Krita.instance().activeWindow().qwindow()
scr = w.screen()
_b64png(scr.grabWindow(0, w.frameGeometry().x(), w.frameGeometry().y(),
                       w.frameGeometry().width(), w.frameGeometry().height()))
""")


@mcp.tool()
def canvas_image(max_size: int = 1024) -> Image:
    """PNG of the active document's merged projection, scaled to fit max_size."""
    return _png(_B64_OF + f"""
d = Krita.instance().activeDocument()
assert d is not None, "no active document"
_b64png(d.thumbnail({max_size}, {max_size}))
""")


@mcp.tool()
def get_errors(clear: bool = False) -> str:
    """Uncaught Python exceptions raised inside Krita (any plugin) since the bridge started."""
    status, body = _send("print('\\n'.join(f'[{t}] {e}' for t, e in _errors) or 'no errors')"
                         + ("\n_errors.clear()" if clear else ""))
    return f"{status}\n{body}"


@mcp.tool()
def plugin_status(name: str = "") -> str:
    """Python plugins known to Krita: enabled flag in kritarc, whether loaded, and the import
    traceback for plugins that failed. Pass a plugin folder name to try importing it fresh."""
    code = r"""
import os, sys, importlib, traceback, configparser
pyk = os.path.join(os.environ['APPDATA'], 'krita', 'pykrita')
rc = configparser.ConfigParser(strict=False, interpolation=None)
# KConfig files start with section-less keys
rc.read_string('[__top__]\n' + open(os.path.join(os.environ['LOCALAPPDATA'], 'kritarc'),
                                    encoding='utf-8', errors='replace').read())
py = rc['python'] if rc.has_section('python') else {}
for f in sorted(os.listdir(pyk)):
    if f.endswith('.desktop'):
        n = f[:-8]
        print(f"{n:24} enabled={py.get('enable_' + n, '?'):6} loaded={n in sys.modules}")
"""
    if name:
        code += (f"\ntry:\n    importlib.import_module('{name}')\n    print('import {name}: OK')\n"
                 f"except Exception:\n    print('import {name} FAILED:'); traceback.print_exc(file=sys.stdout)\n")
    status, body = _send(code)
    return f"{status}\n{body}"


if __name__ == "__main__":
    mcp.run()
