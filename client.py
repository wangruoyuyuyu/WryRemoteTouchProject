#!/usr/bin/env python3
"""
RemoteTouch USBIP Client
=========================
Connects to a RemoteTouch server, receives touch + mouse events.
Exposes a virtual HID touchscreen via USB/IP, and controls the
local mouse directly via pymouse.

Usage:
    python client.py [touch_host] [touch_port]

    Then from a USBIP client machine:
        usbip list   -r <this_ip>
        usbip attach -r <this_ip> -b 1-1   # touch
"""

import socket
import struct
import threading
import ast
import time
import sys
import queue
import logging

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("RemoteTouch")

# ──────────────────────────────────────────────
# Configuration
# ──────────────────────────────────────────────
TOUCH_HOST = sys.argv[1] if len(sys.argv) > 1 else "localhost"
TOUCH_PORT = int(sys.argv[2]) if len(sys.argv) > 2 else 1309
USBIP_PORT = 3240
MAX_CONTACTS = 5
HID_XY_MAX = 32767
CONTACT_STALE_SEC = 3.0


def get_screen_size():
    try:
        import ctypes
        return ctypes.windll.user32.GetSystemMetrics(0), ctypes.windll.user32.GetSystemMetrics(1)
    except Exception:
        pass
    return 1920, 1080


# ──────────────────────────────────────────────
# HID Report Descriptors
# ──────────────────────────────────────────────

_FINGER_BLOCK = bytes([
    0x09, 0x22, 0xA1, 0x02, 0x09, 0x42, 0x09, 0x32,
    0x15, 0x00, 0x25, 0x01, 0x75, 0x01, 0x95, 0x02, 0x81, 0x02,
    0x95, 0x06, 0x75, 0x01, 0x81, 0x03,
    0x09, 0x51, 0x15, 0x00, 0x26, 0xFF, 0x00, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
    0x05, 0x01, 0x09, 0x30, 0x15, 0x00, 0x26, 0xFF, 0x7F, 0x35, 0x00, 0x46, 0x00, 0x00,
    0x75, 0x10, 0x95, 0x01, 0x81, 0x02, 0x09, 0x31, 0x81, 0x02, 0x05, 0x0D, 0xC0,
])


def build_touch_report_desc(n=MAX_CONTACTS):
    d = bytearray([0x05, 0x0D, 0x09, 0x04, 0xA1, 0x01, 0x85, 0x01])
    for _ in range(n):
        d += _FINGER_BLOCK
    d += bytes([
        0x09, 0x54, 0x15, 0x00, 0x25, n, 0x75, 0x08, 0x95, 0x01, 0x81, 0x02,
        0x09, 0x55, 0x15, 0x00, 0x25, n, 0x75, 0x08, 0x95, 0x01, 0xB1, 0x02, 0xC0,
    ])
    return bytes(d)


# ──────────────────────────────────────────────
# pymouse controller
# ──────────────────────────────────────────────

import ctypes as _ct
from pymouse import PyMouse
_MOUSEEVENTF_WHEEL = 0x0800

class MouseController:
    def __init__(self):
        self._m = PyMouse()
        self._sw, self._sh = self._m.screen_size()
        log.info("pymouse screen: %d × %d", self._sw, self._sh)

    def move(self, mx, my, win_w, win_h):
        sx = int(mx / win_w * self._sw) if win_w > 0 else 0
        sy = int(my / win_h * self._sh) if win_h > 0 else 0
        self._m.move(max(0, min(self._sw - 1, sx)), max(0, min(self._sh - 1, sy)))

    def press(self, mx, my, win_w, win_h, button=1):
        self.move(mx, my, win_w, win_h)
        self._m.press(self._m.position()[0], self._m.position()[1], button)

    def release(self, mx, my, win_w, win_h, button=1):
        self.move(mx, my, win_w, win_h)
        self._m.release(self._m.position()[0], self._m.position()[1], button)

    def scroll(self, delta):
        _ct.windll.user32.mouse_event(_MOUSEEVENTF_WHEEL, 0, 0, delta * 120, 0)


_KEYEVENTF_KEYUP = 0x0002

class KeyboardController:
    """Simulate keyboard via Win32 keybd_event. VK codes from Qt nativeVirtualKey."""
    def press(self, vk):
        _ct.windll.user32.keybd_event(vk, 0, 0, 0)

    def release(self, vk):
        _ct.windll.user32.keybd_event(vk, 0, _KEYEVENTF_KEYUP, 0)



# ──────────────────────────────────────────────
# USB descriptor builder
# ──────────────────────────────────────────────

def _str_desc(s):
    raw = s.encode("utf-16-le")
    return struct.pack("BB", 2 + len(raw), 0x03) + raw


def build_descriptors(hid_report_desc, vid, pid, product_str, interval=10, max_pkt=64):
    dev = struct.pack("<BBHBBBBHHHBBBB",
        18, 0x01, 0x0200, 0, 0, 0, 64, vid, pid, 0x0100, 1, 2, 0, 1)
    hid = struct.pack("<BBHBBBH", 9, 0x21, 0x0111, 0, 1, 0x22, len(hid_report_desc))
    ep = struct.pack("<BBBBHB", 7, 0x05, 0x81, 0x03, max_pkt, interval)
    iface = struct.pack("<BBBBBBBBB", 9, 0x04, 0, 0, 1, 0x03, 0, 0, 0)
    cfg = bytearray(struct.pack("<BBHBBBBB", 9, 0x02, 0, 1, 1, 0, 0x80, 50))
    cfg += iface + hid + ep
    struct.pack_into("<H", cfg, 2, len(cfg))
    strings = [b"\x04\x03\x09\x04", _str_desc("RemoteTouch"), _str_desc(product_str)]
    return dev, bytes(cfg), strings


# ──────────────────────────────────────────────
# HID Device classes
# ──────────────────────────────────────────────

class TouchDevice:
    BUSID = b"1-1" + b"\x00" * 29
    PATH  = b"/sys/devices/usb/1-1" + b"\x00" * 236

    def __init__(self):
        desc = build_touch_report_desc()
        self.hid_report_desc = desc
        self.dev_desc, self.cfg_desc, self.strings = build_descriptors(
            desc, 0x1234, 0x5678, "Virtual Touch Screen")
        self._EMPTY = b"\x01" + b"\x00" * 30 + b"\x00"
        self._cached_report = self._EMPTY
        self._last_sent = None
        self._ep1_held = None  # (seqnum, devid, sock, monotonic)
        self._lock = threading.Lock()
        self._contacts = {}
        self._contact_times = {}
        self._last_cleanup = 0.0

    def set_touch(self, cid, x, y):
        with self._lock:
            self._contacts[cid] = (x, y)
            self._contact_times[cid] = time.monotonic()
            self._rebuild()
        self._cleanup()
        self._try_send_held()

    def release_touch(self, cid):
        with self._lock:
            self._contacts.pop(cid, None)
            self._contact_times.pop(cid, None)
            self._rebuild()
        self._try_send_held()

    def _try_send_held(self):
        h = self._ep1_held
        if h and self._cached_report != self._last_sent:
            self._ep1_held = None
            seqnum, devid, sock, _ = h
            try:
                _ret_submit(sock, seqnum, devid, 1, 1, self._cached_report)
                self._last_sent = self._cached_report
            except (ConnectionResetError, BrokenPipeError, OSError):
                pass

    def _rebuild(self):
        r = bytearray(32)
        r[0] = 0x01
        n = 0
        for cid in sorted(self._contacts)[:MAX_CONTACTS]:
            x, y = self._contacts[cid]
            off = 1 + n * 6
            r[off] = 0x03
            r[off + 1] = cid & 0xFF
            struct.pack_into("<HH", r, off + 2, x & 0xFFFF, y & 0xFFFF)
            n += 1
        r[31] = len(self._contacts) & 0xFF
        self._cached_report = bytes(r)

    def _cleanup(self):
        now = time.monotonic()
        if now - self._last_cleanup < 1.0:
            return
        self._last_cleanup = now
        with self._lock:
            stale = [c for c, t in self._contact_times.items() if now - t > CONTACT_STALE_SEC]
            for c in stale:
                del self._contacts[c]
                del self._contact_times[c]
                log.warning("Auto-released stale contact %d", c)
            if stale:
                self._rebuild()



# ──────────────────────────────────────────────
# USB/IP server (touch device only)
# ──────────────────────────────────────────────

def recv_exact(sock, n):
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None
        buf += chunk
    return buf


class USBIPServer:
    def __init__(self, port=USBIP_PORT):
        self.port = port
        self.devices = {}  # busid_bytes → device
        self.running = True

    def add(self, dev):
        self.devices[dev.BUSID[:3]] = dev  # key = "1-1" or "1-2"

    def start(self):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("0.0.0.0", self.port))
        srv.listen(5)
        names = ", ".join(d.BUSID.rstrip(b"\x00").decode() for d in self.devices.values())
        log.info("USB/IP server on :%d  devices: %s", self.port, names)

        threading.Thread(target=self._timeout_loop, daemon=True).start()

        while self.running:
            try:
                cli, addr = srv.accept()
                cli.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                threading.Thread(target=self._client, args=(cli, addr), daemon=True).start()
            except OSError:
                if self.running:
                    raise

    def _client(self, sock, addr):
        try:
            hdr = recv_exact(sock, 8)
            if not hdr:
                return
            ver, op, _ = struct.unpack(">HHI", hdr)
            if op == 0x8005:
                self._devlist(sock)
                sock.close()
            elif op == 0x8003:
                busid_raw = recv_exact(sock, 32)
                if busid_raw:
                    self._import(sock, busid_raw)
        except (ConnectionResetError, BrokenPipeError):
            pass
        except Exception as e:
            log.exception("Client error: %s", e)
            sock.close()

    def _devinfo(self, dev):
        """Common device info block (312 bytes)."""
        pkt = dev.PATH + dev.BUSID
        pkt += struct.pack(">III", 1, 1, 2)   # bus, dev, speed=Full
        pkt += struct.pack(">HHH",
            struct.unpack_from("<H", dev.dev_desc, 8)[0],   # vid
            struct.unpack_from("<H", dev.dev_desc, 10)[0],  # pid
            struct.unpack_from("<H", dev.dev_desc, 12)[0])  # bcd
        pkt += struct.pack("BBB", 0x03, 0, 0)  # class=HID
        pkt += struct.pack("BBB", 1, 1, 1)     # cfgval, ncfg, nif
        return pkt  # 312 bytes

    def _devlist(self, sock):
        devs = list(self.devices.values())
        pkt = struct.pack(">HHI", 0x0111, 0x0005, 0)
        pkt += struct.pack(">I", len(devs))
        for dev in devs:
            pkt += self._devinfo(dev)
            pkt += struct.pack("BBBB", 0x03, 0, 0, 0)  # interface desc
        sock.sendall(pkt)
        log.info("→ DEVLIST (%d devices)", len(devs))

    def _import(self, sock, busid_raw):
        busid_key = busid_raw[:3]
        dev = self.devices.get(busid_key)
        if not dev:
            log.warning("Unknown busid %s", busid_raw.rstrip(b"\x00"))
            sock.sendall(struct.pack(">HHI", 0x0111, 0x0003, 1))  # error
            sock.close()
            return

        pkt = struct.pack(">HHI", 0x0111, 0x0003, 0)
        pkt += self._devinfo(dev)
        sock.sendall(pkt)
        log.info("→ IMPORT %s (%s)", busid_key.decode(), dev.__class__.__name__)
        self._urb_loop(sock, dev)

    # ── URB loop (per-device) ──

    def _urb_loop(self, sock, dev):
        ep1_logged = [False]
        try:
            while self.running:
                cmd = recv_exact(sock, 48)
                if not cmd:
                    break
                cmd_id = struct.unpack(">I", cmd[:4])[0]
                if cmd_id == 0x01:
                    self._cmd_submit(sock, dev, cmd, ep1_logged)
                elif cmd_id == 0x02:
                    self._cmd_unlink(sock, cmd)
                else:
                    break
        except (ConnectionResetError, BrokenPipeError):
            pass
        finally:
            log.info("URB loop ended (%s)", dev.__class__.__name__)
            sock.close()

    def _cmd_submit(self, sock, dev, cmd, ep1_logged):
        (_, seqnum, devid, direction, endpoint,
         _, buf_len, _, _, _) = struct.unpack(">10I", cmd[:40])
        setup = cmd[40:48]

        if endpoint == 0:
            self._control_xfer(sock, dev, seqnum, devid, direction, buf_len, setup)
        elif endpoint == 1 and direction == 1:
            if dev._cached_report != dev._last_sent:
                _ret_submit(sock, seqnum, devid, 1, 1, dev._cached_report)
                dev._last_sent = dev._cached_report
                if not ep1_logged[0]:
                    ep1_logged[0] = True
                    log.info("First EP1 IN (%s, %d bytes)", dev.__class__.__name__, len(dev._cached_report))
            else:
                dev._ep1_held = (seqnum, devid, sock, time.monotonic())
        else:
            if direction == 0 and buf_len > 0:
                recv_exact(sock, buf_len)
            _ret_submit(sock, seqnum, devid, direction, endpoint, b"", status=-32)

    def _control_xfer(self, sock, dev, seqnum, devid, direction, buf_len, setup):
        bmRT, bReq, wValue, wIndex, wLength = struct.unpack("<BBHHH", setup)
        req_type = bmRT & 0x60
        desc_type = (wValue >> 8) & 0xFF
        desc_idx = wValue & 0xFF
        data = b""
        status = 0

        if direction == 0 and buf_len > 0:
            recv_exact(sock, buf_len)

        if req_type == 0x00:  # Standard
            if bReq == 0x06:  # GET_DESCRIPTOR
                if desc_type == 0x01:
                    data = dev.dev_desc[:wLength]
                elif desc_type == 0x02:
                    data = dev.cfg_desc[:wLength]
                elif desc_type == 0x03:
                    if desc_idx < len(dev.strings):
                        data = dev.strings[desc_idx][:wLength]
                    else:
                        status = -32
                elif desc_type == 0x21:
                    off = 9 + 9
                    dlen = dev.cfg_desc[off]
                    data = dev.cfg_desc[off:off + dlen][:wLength]
                elif desc_type == 0x22:
                    data = dev.hid_report_desc[:wLength]
                else:
                    status = -32
            elif bReq in (0x05, 0x09):  # SET_ADDRESS, SET_CONFIGURATION
                pass
            elif bReq == 0x08:
                data = b"\x01"
            else:
                status = -32
        elif req_type == 0x20:  # HID Class
            if bReq == 0x0A:    # SET_IDLE
                pass
            elif bReq == 0x01:  # GET_REPORT
                rtype = (wValue >> 8) & 0xFF
                if rtype == 0x03 and isinstance(dev, TouchDevice):
                    data = bytes([0x01, MAX_CONTACTS])
                else:
                    data = dev._cached_report
            elif bReq == 0x02:
                data = b"\x00"
            elif bReq == 0x03:
                data = b"\x00"
            else:
                status = -32
        else:
            status = -32

        _ret_submit(sock, seqnum, devid, 1, 0, data, status=status)

    def _timeout_loop(self):
        """Flush held EP1 requests after 100ms so the host doesn't timeout."""
        while self.running:
            time.sleep(0.05)
            for dev in self.devices.values():
                h = dev._ep1_held
                if h and time.monotonic() - h[3] > 0.1:
                    dev._ep1_held = None
                    seqnum, devid, sock, _ = h
                    try:
                        _ret_submit(sock, seqnum, devid, 1, 1, dev._cached_report)
                        dev._last_sent = dev._cached_report
                    except (ConnectionResetError, BrokenPipeError, OSError):
                        pass

    def _cmd_unlink(self, sock, cmd):
        seqnum = struct.unpack(">I", cmd[4:8])[0]
        for dev in self.devices.values():
            if dev._ep1_held and dev._ep1_held[0] == seqnum:
                dev._ep1_held = None
        reply = bytearray(48)
        struct.pack_into(">I", reply, 0, 0x04)
        struct.pack_into(">I", reply, 4, seqnum)
        struct.pack_into(">i", reply, 20, -104)
        sock.sendall(bytes(reply))

def _ret_submit(sock, seqnum, devid, direction, endpoint, data, status=0):
    reply = bytearray(48)
    struct.pack_into(">I", reply,  0, 0x03)
    struct.pack_into(">I", reply,  4, seqnum)
    struct.pack_into(">I", reply,  8, devid)
    struct.pack_into(">I", reply, 12, direction)
    struct.pack_into(">I", reply, 16, endpoint)
    struct.pack_into(">i", reply, 20, status)
    struct.pack_into(">I", reply, 24, len(data))
    reply.extend(data)
    sock.sendall(reply)


# ──────────────────────────────────────────────
# Message parser
# ──────────────────────────────────────────────

def _extract_messages(buf):
    messages = []
    depth = 0
    start = None
    last_end = -1
    for i, ch in enumerate(buf):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    messages.append(ast.literal_eval(buf[start:i + 1]))
                except Exception as exc:
                    log.warning("Parse error: %s", exc)
                start = None
                last_end = i
    return messages, buf[last_end + 1:] if last_end >= 0 else buf


def _map_to_hid(x, y, win_w, win_h):
    hx = int(x / win_w * HID_XY_MAX) if win_w > 0 else 0
    hy = int(y / win_h * HID_XY_MAX) if win_h > 0 else 0
    return max(0, min(HID_XY_MAX, hx)), max(0, min(HID_XY_MAX, hy))


# ──────────────────────────────────────────────
# Touch + mouse reader
# ──────────────────────────────────────────────

def reader(host, port, touch_dev, mouse_ctl, key_ctl):
    buf = ""
    while True:
        try:
            log.info("Connecting to %s:%d …", host, port)
            sock = socket.socket()
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.connect((host, port))
            log.info("Connected")

            buf = ""
            while True:
                raw = sock.recv(4096)
                if not raw:
                    break
                buf += raw.decode("utf-8", errors="replace")
                msgs, buf = _extract_messages(buf)
                for msg in msgs:
                    mt = msg.get("msg_type")
                    data = msg.get("data")
                    if not data or len(data) < 3:
                        continue

                    if mt == 0:        # TOUCH PRESS
                        cid, (tx, ty), (ww, wh) = data
                        hx, hy = _map_to_hid(tx, ty, ww, wh)
                        touch_dev.set_touch(cid, hx, hy)
                    elif mt == 1:      # TOUCH MOVE
                        cid, (tx, ty), (ww, wh) = data
                        hx, hy = _map_to_hid(tx, ty, ww, wh)
                        touch_dev.set_touch(cid, hx, hy)
                    elif mt == 2:      # TOUCH RELEASE
                        cid = data[0]
                        touch_dev.release_touch(cid)
                    elif mt == 3:      # MOUSE MOVE
                        _, (mx, my), (ww, wh) = data
                        mouse_ctl.move(mx, my, ww, wh)
                    elif mt == 4:      # MOUSE PRESS
                        btn_bit, (mx, my), (ww, wh) = data
                        mouse_ctl.press(mx, my, ww, wh, btn_bit.bit_length())
                    elif mt == 5:      # MOUSE RELEASE
                        btn_bit, (mx, my), (ww, wh) = data
                        mouse_ctl.release(mx, my, ww, wh, btn_bit.bit_length())
                    elif mt == 6:      # MOUSE WHEEL
                        delta = data[0]
                        mouse_ctl.scroll(delta)
                    elif mt == 7:      # KEY PRESS
                        vk = data[0]
                        key_ctl.press(vk)
                    elif mt == 8:      # KEY RELEASE
                        vk = data[0]
                        key_ctl.release(vk)

        except ConnectionRefusedError:
            log.warning("Connection refused — retrying in 2s")
            time.sleep(2)
        except (ConnectionResetError, BrokenPipeError, OSError) as e:
            log.warning("Disconnected: %s — retrying in 2s", e)
            time.sleep(2)


# ──────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────

def main():
    screen_w, screen_h = get_screen_size()
    log.info("══════════════════════════════════════════════════")
    log.info("  RemoteTouch USB/IP Client")
    log.info("  Local screen : %d × %d", screen_w, screen_h)
    log.info("  Touch server : %s:%d", TOUCH_HOST, TOUCH_PORT)
    log.info("  USB/IP port  : %d", USBIP_PORT)
    log.info("══════════════════════════════════════════════════")

    touch_dev = TouchDevice()
    mouse_ctl = MouseController()
    key_ctl = KeyboardController()

    server = USBIPServer()
    server.add(touch_dev)

    threading.Thread(
        target=reader,
        args=(TOUCH_HOST, TOUCH_PORT, touch_dev, mouse_ctl, key_ctl),
        daemon=True,
    ).start()

    try:
        server.start()
    except KeyboardInterrupt:
        log.info("Shutting down")
        server.running = False


if __name__ == "__main__":
    main()
