"""KritaMCP bridge – runs inside Krita, listens on 127.0.0.1:50017.

Protocol (same as the 3ds Max bridge): client sends Python source followed by
"\n<<END>>\n"; reply is "OK\n<output>" or "ERR\n<traceback>" followed by the
same terminator. Code runs on Krita's GUI thread in a persistent namespace, so
state survives between calls. If the last statement is an expression, its
repr() is appended to the output (like the interactive prompt).

Localhost only. Every uncaught Python exception in Krita (any plugin) is also
kept in a ring buffer, readable through the bridge as `_errors`.
"""
import ast
import contextlib
import io
import sys
import time
import traceback

from krita import Extension, Krita
from PyQt6.QtCore import QTimer
from PyQt6.QtNetwork import QHostAddress, QTcpServer

PORT = 50017
TERM = b"\n<<END>>"

_errors = []          # (timestamp, text) of uncaught exceptions
_prev_hook = sys.excepthook


def _hook(etype, value, tb):
    _errors.append((time.strftime("%H:%M:%S"),
                    "".join(traceback.format_exception(etype, value, tb))))
    del _errors[:-50]
    _prev_hook(etype, value, tb)


sys.excepthook = _hook


def _run(code, ns):
    out = io.StringIO()
    try:
        tree = ast.parse(code, "<mcp>", "exec")
        last = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last = ast.Expression(tree.body.pop().value)
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            exec(compile(tree, "<mcp>", "exec"), ns)
            if last is not None:
                val = eval(compile(last, "<mcp>", "eval"), ns)
                if val is not None:
                    print(repr(val) if not isinstance(val, str) else val)
        return "OK\n" + out.getvalue()
    except BaseException:
        return "ERR\n" + out.getvalue() + traceback.format_exc()


class KritaMcpBridge(Extension):
    def __init__(self, parent):
        super().__init__(parent)
        self._buffers = {}
        self._ns = {"Krita": Krita, "_errors": _errors, "__name__": "__mcp__"}
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_connection)
        # retry so a lingering socket from a crashed instance doesn't block us
        self._listen_tries = 0
        self._listen()

    def _listen(self):
        if self._server.listen(QHostAddress(QHostAddress.SpecialAddress.LocalHost), PORT):
            print(f"[kritamcp] listening on 127.0.0.1:{PORT}")
        elif self._listen_tries < 10:
            self._listen_tries += 1
            QTimer.singleShot(1000, self._listen)

    def setup(self):
        pass

    def createActions(self, window):
        pass

    def _on_connection(self):
        while self._server.hasPendingConnections():
            sock = self._server.nextPendingConnection()
            self._buffers[sock] = b""
            sock.readyRead.connect(lambda s=sock: self._on_ready(s))
            sock.disconnected.connect(lambda s=sock: self._buffers.pop(s, None))
            sock.disconnected.connect(sock.deleteLater)

    def _on_ready(self, sock):
        self._buffers[sock] = self._buffers.get(sock, b"") + bytes(sock.readAll())
        buf = self._buffers[sock]
        if TERM not in buf:
            return
        code = buf.split(TERM)[0].decode("utf-8", "replace")
        self._buffers[sock] = b""
        reply = _run(code, self._ns)
        sock.write(reply.encode("utf-8") + TERM + b"\n")
        sock.flush()


Krita.instance().addExtension(KritaMcpBridge(Krita.instance()))
