from PySide6 import QtWidgets, QtCore, QtGui
import socket
import threading
import queue
import sys


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

    def keyPressEvent(self, event):
        # Ctrl+Alt+M: toggle opacity for moving the window
        mods = event.modifiers()
        if (event.nativeVirtualKey() == 0x4D  # 'M'
                and mods & QtCore.Qt.ControlModifier
                and mods & QtCore.Qt.AltModifier):
            self._opacity_hi = not getattr(self, '_opacity_hi', False)
            self.setWindowOpacity(0.5 if self._opacity_hi else 0.01)
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
    mw.show()
    qa.exec()
