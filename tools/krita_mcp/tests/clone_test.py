# Automated Clone Stamp test, run inside Krita via the bridge:
#   python kcall.py exec "DEPTH='U16'" ; python kcall.py exec -f tests/clone_test.py
# Creates a test document (DEPTH = U8/U16/F32), samples the red left half with
# Ctrl+click, paints a stroke into the gray right half and checks the pixels.
# Uses REAL mouse/keyboard input (see realinput.py): Krita's input manager
# ignores synthetic Qt mouse events for painting.
import struct
import sys

sys.path.insert(0, r"D:\_Code\Krita\tools\krita_mcp\tests")
import importlib
import realinput
importlib.reload(realinput)

from PyQt6.QtWidgets import QApplication
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

DEPTH = globals().get("DEPTH", "U16")
MIRROR = globals().get("MIRROR", False)   # red on the right, sample right, paint left
W, H = 400, 300
k = Krita.instance()
win = k.activeWindow()

doc = k.createDocument(W, H, f"clone_test_{DEPTH}", "RGBA", DEPTH, "", 72.0)
win.addView(doc)
QApplication.processEvents()
layer = doc.activeNode()

# fill: left half red, right half mid gray (opaque)
fmt = {"U8": "B", "U16": "H", "F32": "f"}[DEPTH]
one = {"U8": 255, "U16": 65535, "F32": 1.0}[DEPTH]
half = one // 2 if DEPTH != "F32" else 0.5


def px(r, g, b, a):            # Krita stores BGRA for RGBA color spaces
    return struct.pack("<4" + fmt, b, g, r, a)


red, grayp = px(one, 0, 0, one) * (W // 2), px(half, half, half, one) * (W // 2)
row = grayp + red if MIRROR else red + grayp
layer.setPixelData(row * H, 0, 0, W, H)
doc.refreshProjection()
doc.waitForDone()

k.action("KritaShape/KisToolCloneStamp").trigger()
QApplication.processEvents()

view = win.activeView()
cw = [w for w in win.qwindow().centralWidget().findChildren(QOpenGLWidget) if w.isVisible()][0]
ri = realinput.RealInput(view, cw)
ri.raise_krita()

SX, DX = (300, 100) if MIRROR else (100, 300)
ri.click(SX, 150, ctrl=True)                                # source on red
ri.drag([(DX + i * 4, 150) for i in range(11)])             # stroke on gray
doc.waitForDone()
ri.pump(0.3)
ri.lower_krita()


def read(x, y):
    b, g, r, a = struct.unpack("<4" + fmt, bytes(layer.pixelData(x, y, 1, 1)))
    return tuple(round(v / one, 3) for v in (r, g, b, a))


gray = (round(half / one, 3),) * 3 + (1.0,)
center = read(DX + 20, 150)
outside = read(DX + 20, 20)
print(f"mirror={MIRROR} depth={DEPTH}  stroke center={center}  untouched={outside}")
ok = center[0] > 0.9 and center[1] < 0.1 and outside == gray
print("RESULT:", "PASS" if ok else "FAIL")
