# Automated Clone Stamp brush-option tests, run inside Krita via the bridge:
#   python kcall.py exec -f tests/brush_tests.py
# Same harness style as clone_test.py: each test opens its own 400x300 RGBA U8
# document (left half red, right half gray 128), samples the red side with
# Ctrl+click, paints into the gray side, then reads the pixels back.
# Uses REAL mouse input (see realinput.py): Krita's input manager ignores
# synthetic Qt mouse events for painting.
import struct
import sys
import traceback

sys.path.insert(0, r"D:\_Code\Krita\tools\krita_mcp\tests")
import importlib
import realinput
importlib.reload(realinput)

from PyQt6.QtWidgets import QApplication, QSpinBox, QCheckBox, QToolButton, QComboBox
from PyQt6.QtOpenGLWidgets import QOpenGLWidget

W, H = 400, 300
ONE = 255
GRAY = 128
k = Krita.instance()
win = k.activeWindow()


def px(r, g, b, a):            # Krita stores BGRA for RGBA color spaces
    return struct.pack("<4B", b, g, r, a)


def opts():
    # The tool-options widget: parent of the "Spacing: " slider (all sliders
    # share that top-level options widget as their parent).
    for sb in win.qwindow().findChildren(QSpinBox):
        if sb.prefix().startswith("Spacing: "):
            return sb.parentWidget()
    return None


def set_slider(prefix, value):
    o = opts()
    for sb in o.findChildren(QSpinBox):
        if sb.prefix() == prefix:
            sb.setValue(value)
            break
    QApplication.processEvents()


def set_check(text, on):
    o = opts()
    for cb in o.findChildren(QCheckBox):
        if cb.text() == text:
            cb.setChecked(on)
            break
    QApplication.processEvents()


def click_button(text):
    o = opts()
    for btn in o.findChildren(QToolButton):
        if btn.text() == text:
            btn.click()
            break
    QApplication.processEvents()


def set_tip(index):
    o = opts()
    for cb in o.findChildren(QComboBox):
        if cb.itemText(0) == "Round":
            cb.setCurrentIndex(index)
            break
    QApplication.processEvents()


def new_doc(name):
    d = k.createDocument(W, H, name, "RGBA", "U8", "", 72.0)
    win.addView(d)
    QApplication.processEvents()
    l = d.activeNode()
    red = px(ONE, 0, 0, ONE) * (W // 2)
    grayp = px(GRAY, GRAY, GRAY, ONE) * (W // 2)
    l.setPixelData((red + grayp) * H, 0, 0, W, H)
    d.refreshProjection()
    d.waitForDone()
    k.action("KritaShape/KisToolCloneStamp").trigger()
    QApplication.processEvents()
    view = win.activeView()
    cw = [w for w in win.qwindow().centralWidget().findChildren(QOpenGLWidget) if w.isVisible()][0]
    ri = realinput.RealInput(view, cw)
    ri.raise_krita()
    return d, l, ri


def read(layer, x, y):
    b, g, r, a = struct.unpack("<4B", bytes(layer.pixelData(x, y, 1, 1)))
    return (r, g, b, a)


def reset_tool():
    click_button("Hard")
    set_slider("Size: ", 60)
    set_slider("Opacity: ", 100)
    set_slider("Flow: ", 100)
    set_slider("Spacing: ", 10)
    set_check("Airbrush", False)


def test_opacity_cap():
    doc, layer, ri = new_doc("brush_opacity_cap")
    try:
        reset_tool()
        set_slider("Opacity: ", 50)
        set_slider("Flow: ", 100)
        ri.pump(0.1)
        ri.click(100, 150, ctrl=True)
        ri.pump(0.1)
        pts = []
        for x in range(280, 321, 4):
            pts.append((x, 150))
        for x in range(316, 279, -4):
            pts.append((x, 150))
        for x in range(284, 321, 4):
            pts.append((x, 150))
        ri.drag(pts)
        doc.waitForDone()
        r, g, b, a = read(layer, 300, 150)
        ok = 179 <= r <= 203
        return ("opacity_cap", ok, f"red={r} (expect ~191±12, not full red)")
    finally:
        doc.setModified(False)
        doc.close()


def test_flow_buildup():
    doc, layer, ri = new_doc("brush_flow_buildup")
    try:
        reset_tool()
        set_slider("Opacity: ", 100)
        set_slider("Flow: ", 20)
        ri.pump(0.1)
        ri.click(100, 150, ctrl=True)
        ri.pump(0.1)
        ri.drag([(300, 150), (300, 150)])
        doc.waitForDone()
        r, g, b, a = read(layer, 300, 150)
        ok = 140 < r < 230
        return ("flow_buildup", ok, f"red={r} (expect 140..230, single low-flow dab)")
    finally:
        doc.setModified(False)
        doc.close()


def test_square_tip():
    doc, layer, ri = new_doc("brush_square_tip")
    try:
        reset_tool()
        set_tip(1)
        set_slider("Hard: ", 100)
        set_slider("Size: ", 60)
        ri.pump(0.1)
        ri.click(100, 150, ctrl=True)
        ri.pump(0.1)
        ri.drag([(300, 150), (300, 150)])
        doc.waitForDone()
        r, g, b, a = read(layer, 326, 176)
        ok = r > 200
        return ("square_tip", ok, f"corner(326,176) red={r} (expect >200, square fills corner)")
    finally:
        doc.setModified(False)
        doc.close()


def test_round_tip_corner():
    doc, layer, ri = new_doc("brush_round_tip")
    try:
        reset_tool()
        click_button("Hard")
        set_slider("Size: ", 60)
        ri.pump(0.1)
        ri.click(100, 150, ctrl=True)
        ri.pump(0.1)
        ri.drag([(300, 150), (300, 150)])
        doc.waitForDone()
        r, g, b, a = read(layer, 326, 176)
        ok = r < 150
        return ("round_tip_corner", ok, f"corner(326,176) red={r} (expect <150, round leaves corner gray)")
    finally:
        doc.setModified(False)
        doc.close()


def test_airbrush_buildup():
    doc, layer, ri = new_doc("brush_airbrush_on")
    try:
        reset_tool()
        click_button("Airbrush")
        set_slider("Size: ", 60)
        ri.pump(0.1)
        ri.click(100, 150, ctrl=True)
        ri.pump(0.1)
        ri.move(300, 150, 0.1)
        realinput.user32.mouse_event(realinput.LDOWN, 0, 0, 0, 0)
        ri.pump(1.5)
        realinput.user32.mouse_event(realinput.LUP, 0, 0, 0, 0)
        ri.pump(0.2)
        doc.waitForDone()
        r, g, b, a = read(layer, 300, 150)
        ok1 = r > 200
    finally:
        doc.setModified(False)
        doc.close()

    # comparison: same hold, but Airbrush disabled -> must NOT build up
    doc2, layer2, ri2 = new_doc("brush_airbrush_off")
    try:
        reset_tool()
        click_button("Airbrush")
        set_check("Airbrush", False)
        set_slider("Size: ", 60)
        ri2.pump(0.1)
        ri2.click(100, 150, ctrl=True)
        ri2.pump(0.1)
        ri2.move(300, 150, 0.1)
        realinput.user32.mouse_event(realinput.LDOWN, 0, 0, 0, 0)
        ri2.pump(1.5)
        realinput.user32.mouse_event(realinput.LUP, 0, 0, 0, 0)
        ri2.pump(0.2)
        doc2.waitForDone()
        r2, g2, b2, a2 = read(layer2, 300, 150)
        ok2 = r2 < 160
    finally:
        doc2.setModified(False)
        doc2.close()

    ok = ok1 and ok2
    return ("airbrush_buildup", ok,
            f"held red={r} (expect >200) | no-airbrush red={r2} (expect <160)")


def test_spacing_gapless():
    doc, layer, ri = new_doc("brush_spacing_gapless")
    try:
        reset_tool()
        click_button("Hard")
        set_slider("Size: ", 20)
        set_slider("Spacing: ", 100)
        ri.pump(0.1)
        ri.click(100, 150, ctrl=True)
        ri.pump(0.1)
        ri.drag([(250, 150), (300, 150), (350, 150)])
        doc.waitForDone()
        r1, *_ = read(layer, 275, 150)
        r2, *_ = read(layer, 325, 150)
        ok = r1 > 200 and r2 > 200
        return ("spacing_gapless", ok, f"mid(275)={r1} mid(325)={r2} (expect both >200, dabs interpolated along path)")
    finally:
        doc.setModified(False)
        doc.close()


TESTS = [
    test_opacity_cap,
    test_flow_buildup,
    test_square_tip,
    test_round_tip_corner,
    test_airbrush_buildup,
    test_spacing_gapless,
]


def main():
    results = []
    for t in TESTS:
        try:
            name, ok, details = t()
        except Exception:
            name = t.__name__
            ok = False
            details = "EXCEPTION: " + traceback.format_exc(limit=1).strip().splitlines()[-1]
        results.append((name, ok, details))

    wname = max([len(n) for n, _, _ in results] + [len("NAME")])
    print(f"{'NAME':<{wname}}  {'RESULT':<6}  details")
    print("-" * (wname + 10 + 40))
    passed = 0
    for name, ok, details in results:
        if ok:
            passed += 1
        print(f"{name:<{wname}}  {'PASS' if ok else 'FAIL':<6}  {details}")
    print("-" * (wname + 10 + 40))
    print(f"{passed}/{len(results)} passed")
    return results


main()
