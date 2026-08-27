from PySide6 import QtWidgets, QtCore, QtGui
import socket
import threading
import queue
import sys

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    _PM_REMOVE = 0x0001
    _WM_HOTKEY = 0x0312
    _MOD_ALT = 0x0001
    _MOD_CONTROL = 0x0002
    _HOTKEYS = [(0x0002 | 0x0001, 0x4D, "Ctrl+Alt+M"),
                (0x0002 | 0x0001, 0x4E, "Ctrl+Alt+N"),  # fallback (WeType grabs Ctrl+Alt+M)
                (0x0002 | 0x0001, 0x42, "Ctrl+Alt+B")]  # fallback 2


class HotkeyThread(threading.Thread):
    """Global hotkey via RegisterHotKey + message loop, with fallbacks."""

    _HOTKEYS = _HOTKEYS

    def __init__(self, callback):
        super().__init__(daemon=True)
        self._callback = callback
        self._started = threading.Event()
        self._ok = False
        self.key_name = None

    def run(self):
        for i, (mods, vk, name) in enumerate(self._HOTKEYS, start=1):
            if _user32.RegisterHotKey(None, i, mods, vk):
                self._hotkey_id = i
                self.key_name = name
                self._ok = True
                break
        self._started.set()
        if not self._ok:
            return
        msg = wintypes.MSG()
        while _user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == _WM_HOTKEY and msg.wParam == self._hotkey_id:
                self._callback()



class MsgTypes:
    PRESS = 0
    MOVE = 1
    RELEASE = 2
    MOUSE_MOVE = 3
    MOUSE_PRESS = 4
    MOUSE_RELEASE = 5
    MOUSE_WHEEL = 6
    KEY_PRESS = 7
    KEY_RELEASE = 8


class MainWindow(QtWidgets.QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowFlags(QtCore.Qt.WindowStaysOnTopHint | QtCore.Qt.Tool)
        self.setAttribute(QtCore.Qt.WA_AcceptTouchEvents)
        self.setWindowOpacity(0.01)
        self.setTabletTracking(True)
        self.setMouseTracking(True)
        self._old_points = list()
        self._mouse_buttons = 0

        # Restore geometry
        settings = QtCore.QSettings("RemoteTouch", "RemoteTouch")
        geom = settings.value("geometry")
        if geom:
            self.restoreGeometry(geom)

        self.socket = socket.socket()
        self.socket.bind(("0.0.0.0", 1309))
        self._msg_queue = queue.Queue()
        threading.Thread(target=self.listen, daemon=True).start()

    def listen(self):
        self.socket.listen()
        while 1:
            c = self.socket.accept()[0]
            c.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            while 1:
                msg = self._msg_queue.get()
                c.send(str(msg).encode())

    def _put(self, msg_type, data):
        self._msg_queue.put({"msg_type": msg_type, "data": data})

    # ── Mouse (real only, ignore touch-synthesized) ──

    _NOT_SYNTH = QtCore.Qt.MouseEventSource.MouseEventNotSynthesized

    def mousePressEvent(self, event):
        if event.source() != self._NOT_SYNTH:
            return
        bit = {QtCore.Qt.LeftButton: 1, QtCore.Qt.RightButton: 2, QtCore.Qt.MiddleButton: 4}.get(event.button(), 0)
        self._mouse_buttons |= bit
        pos = event.position()
        self._put(MsgTypes.MOUSE_PRESS, (bit, (int(pos.x()), int(pos.y())), (self.width(), self.height())))

    def mouseReleaseEvent(self, event):
        if event.source() != self._NOT_SYNTH:
            return
        bit = {QtCore.Qt.LeftButton: 1, QtCore.Qt.RightButton: 2, QtCore.Qt.MiddleButton: 4}.get(event.button(), 0)
        self._mouse_buttons &= ~bit
        pos = event.position()
        self._put(MsgTypes.MOUSE_RELEASE, (bit, (int(pos.x()), int(pos.y())), (self.width(), self.height())))

    def mouseMoveEvent(self, event):
        if event.source() != self._NOT_SYNTH:
            return
        pos = event.position()
        self._put(MsgTypes.MOUSE_MOVE, (0, (int(pos.x()), int(pos.y())), (self.width(), self.height())))

    def wheelEvent(self, event):
        if event.source() != self._NOT_SYNTH:
            return
        delta = event.angleDelta().y() // 120
        if delta:
            self._put(MsgTypes.MOUSE_WHEEL, (delta, (0, 0), (self.width(), self.height())))

    # ── Keyboard ──

    def toggle_opacity(self):
        self._opacity_hi = not getattr(self, '_opacity_hi', False)
        # Run on the GUI thread via queued invocation (hotkey fires on worker thread)
        QtCore.QMetaObject.invokeMethod(
            self, "apply_opacity", QtCore.Qt.ConnectionType.QueuedConnection)

    @QtCore.Slot()
    def apply_opacity(self):
        self.setWindowOpacity(0.5 if getattr(self, '_opacity_hi', False) else 0.01)

    def keyPressEvent(self, event):
        mods = event.modifiers()
        if (event.nativeVirtualKey() == 0x4D  # 'M'
                and mods & QtCore.Qt.ControlModifier
                and mods & QtCore.Qt.AltModifier):
            self.toggle_opacity()
            return
        vk = event.nativeVirtualKey()
        self._put(MsgTypes.KEY_PRESS, (vk, (0, 0), (0, 0)))

    def keyReleaseEvent(self, event):
        vk = event.nativeVirtualKey()
        self._put(MsgTypes.KEY_RELEASE, (vk, (0, 0), (0, 0)))

    # ── Touch ──

    def event(self, event: QtCore.QEvent):
        if event.type() == QtCore.QEvent.Type.TouchBegin:
            point = QtGui.QTouchEvent.point(event, 0)
            event.accept()
            self._put(MsgTypes.PRESS, (point.id(), (point.pos().x(), point.pos().y()), (self.width(), self.height())))
            return True
        elif event.type() == QtCore.QEvent.Type.TouchUpdate:
            now = QtGui.QTouchEvent.touchPoints(event)
            if len(now) > len(self._old_points):
                for i in now:
                    found = any(i.id() == j.id() for j in self._old_points)
                    if not found:
                        self._put(MsgTypes.PRESS, (i.id(), (i.pos().x(), i.pos().y()), (self.width(), self.height())))
            elif len(now) < len(self._old_points):
                for i in self._old_points:
                    found = any(i.id() == j.id() for j in now)
                    if not found:
                        self._put(MsgTypes.RELEASE, (i.id(), (i.pos().x(), i.pos().y()), (self.width(), self.height())))
            else:
                for i in now:
                    self._put(MsgTypes.MOVE, (i.id(), (i.pos().x(), i.pos().y()), (self.width(), self.height())))
            self._old_points = QtGui.QTouchEvent.touchPoints(event)
        elif event.type() == QtCore.QEvent.Type.TouchEnd:
            point = QtGui.QTouchEvent.point(event, 0)
            self._put(MsgTypes.RELEASE, (point.id(), (point.pos().x(), point.pos().y()), (self.width(), self.height())))
        return super().event(event)

    def closeEvent(self, event):
        settings = QtCore.QSettings("RemoteTouch", "RemoteTouch")
        settings.setValue("geometry", self.saveGeometry())
        sys.exit()
        return super().closeEvent(event)


if __name__ == "__main__":
    qa = QtWidgets.QApplication(list())
    mw = MainWindow()
    if sys.platform == "win32":
        hk = HotkeyThread(mw.toggle_opacity)
        hk.start()
        hk._started.wait()  # block until registration attempted (thread start can be slow)
        if not hk._ok:
            print("Warning: no hotkey could be registered", file=sys.stderr)
        else:
            print(f"Toggle hotkey: {hk.key_name}")
    mw.show()
    qa.exec()
