# Real OS input for GUI tests inside Krita (Windows). Krita's input manager
# ignores synthetic Qt mouse events for painting, so tests drive the real
# cursor via user32. Moves the user's mouse -- only use with their OK.
import ctypes
import time

from PyQt6.QtCore import QPointF
from PyQt6.QtWidgets import QApplication

user32 = ctypes.windll.user32
VK_CONTROL, VK_MENU, KEYUP = 0x11, 0x12, 0x2
LDOWN, LUP = 0x0002, 0x0004
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_FLAGS = 0x0001 | 0x0002 | 0x0010  # NOSIZE | NOMOVE | NOACTIVATE


class RealInput:
    def __init__(self, view, canvas_widget):
        self.view = view
        self.cw = canvas_widget

    def raise_krita(self):
        # Make Krita topmost for the test so real clicks can't land on another
        # window (e.g. a terminal overlapping the canvas). Setting topmost on
        # its own window needs no foreground rights and no synthetic keys --
        # an Alt-tap trick left Krita's input manager with a "running"
        # shortcut and crashed the next addView. The first click then
        # activates Krita normally. Call lower_krita() when done.
        self.hwnd = int(self.cw.window().winId())
        user32.SetWindowPos(self.hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_FLAGS)
        self.pump(0.2)
        # One click on the status bar activates the window (harmless spot);
        # otherwise the first canvas click is consumed as activation.
        win = self.cw.window()
        sb = win.statusBar()
        p = sb.mapToGlobal(sb.rect().center())
        dpr = self.cw.devicePixelRatioF()
        user32.SetCursorPos(int(p.x() * dpr), int(p.y() * dpr))
        self.pump(0.1)
        user32.mouse_event(LDOWN, 0, 0, 0, 0)
        user32.mouse_event(LUP, 0, 0, 0, 0)
        self.pump(0.3)

    def lower_krita(self):
        if getattr(self, "hwnd", None):
            user32.SetWindowPos(self.hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_FLAGS)

    def pump(self, secs=0.0):
        end = time.time() + secs
        while True:
            QApplication.processEvents()
            if time.time() >= end:
                break
            time.sleep(0.005)

    def screen_pt(self, x, y):
        # Transforms are read fresh: a new view settles its zoom/pan only
        # after a few event loop rounds.
        i2f = self.view.flakeToImageTransform().inverted()[0]
        f2w = self.view.flakeToCanvasTransform()
        dpr = self.cw.devicePixelRatioF()
        p = self.cw.mapToGlobal(f2w.map(i2f.map(QPointF(x, y))))
        return int(round(p.x() * dpr)), int(round(p.y() * dpr))

    def move(self, x, y, settle=0.015):
        user32.SetCursorPos(*self.screen_pt(x, y))
        self.pump(settle)

    def click(self, x, y, ctrl=False):
        self.move(x, y, 0.1)
        if ctrl:
            user32.keybd_event(VK_CONTROL, 0, 0, 0)
            self.pump(0.1)
        user32.mouse_event(LDOWN, 0, 0, 0, 0)
        self.pump(0.08)
        user32.mouse_event(LUP, 0, 0, 0, 0)
        self.pump(0.08)
        if ctrl:
            user32.keybd_event(VK_CONTROL, 0, KEYUP, 0)
            self.pump(0.1)

    def drag(self, points, step_delay=0.015):
        self.move(*points[0], 0.1)
        user32.mouse_event(LDOWN, 0, 0, 0, 0)
        self.pump(0.05)
        for x, y in points[1:]:
            self.move(x, y, step_delay)
        user32.mouse_event(LUP, 0, 0, 0, 0)
        self.pump(0.2)
