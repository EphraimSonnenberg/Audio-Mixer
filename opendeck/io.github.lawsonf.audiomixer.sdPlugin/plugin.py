#!/usr/bin/env python3
"""Audio Mixer plugin for OpenDeck / Stream Deck.

Talks to OpenDeck over the Stream Deck SDK websocket and to the Audio Mixer app over its local
control socket. Standard library + pycairo only (pycairo is already needed by the mixer).

Actions: Channel (mute / volume, key or dial), Mix (mute / volume, key or dial),
         System sounds (toggle the catch-all), Show mixer (show / hide the window).
"""
import base64
import io
import json
import os
import selectors
import shutil
import socket
import struct
import subprocess
import sys
import time

PLUGIN = "io.github.lawsonf.audiomixer"
A_CHANNEL, A_MIX = f"{PLUGIN}.channel", f"{PLUGIN}.mix"
A_SYSTEM, A_WINDOW = f"{PLUGIN}.system", f"{PLUGIN}.window"
SOCK = os.path.join(os.environ.get("XDG_RUNTIME_DIR") or "/tmp", "audio-mixer.sock")
BLUE, GREY, RED = (0.208, 0.682, 0.961), (0.55, 0.55, 0.55), (1.0, 0.42, 0.42)


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- drawing
def draw_glyph(cr, color, waves=3, cross=False, dim=False):
    """The speaker logo, centred in a 144x144 box."""
    import math
    cr.save()
    cr.translate(72, 80)
    cr.scale(1.5, 1.5)
    cr.translate(-34.5, -37.5)           # logo is drawn in a 75x75 box around (34.5, 37.5)
    r, g, b = color
    cr.set_source_rgba(r, g, b, 0.45 if dim else 1)
    cr.set_line_width(5)
    cr.set_line_join(1)                  # round
    cr.set_line_cap(1)
    cr.move_to(39.389, 13.769)
    for x, y in ((22.235, 28.606), (6, 28.606), (6, 47.699), (21.989, 47.699), (39.389, 62.75)):
        cr.line_to(x, y)
    cr.close_path()
    cr.fill_preserve()
    cr.stroke()
    for i, (rad, ang) in enumerate(((19.5, 33.3), (30, 36.4), (38.8, 38.8))):
        if i < waves and not cross:
            cr.new_path()
            cr.arc(31.5, 38.3, rad, -math.radians(ang), math.radians(ang))
            cr.stroke()
    if cross:
        cr.set_source_rgb(*RED)
        cr.new_path()
        for (x1, y1, x2, y2) in ((48, 26, 66, 50), (66, 26, 48, 50)):
            cr.move_to(x1, y1)
            cr.line_to(x2, y2)
        cr.stroke()
    cr.restore()


def render_png(vol=None, muted=False, on=True, size=144, waves=None, label_color=None):
    """Key image: speaker glyph with a volume bar on top. Returns PNG bytes."""
    import cairo
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 144, 144)
    cr = cairo.Context(surf)
    cr.set_source_rgb(0.13, 0.13, 0.13)
    cr.paint()
    if vol is not None:
        x, y, w, h = 16, 12, 112, 9
        for frac, col in ((1.0, (0.28, 0.28, 0.28)), (vol / 100 if on else 0, RED if muted else BLUE)):
            if frac <= 0:
                continue
            cr.new_path()
            ww = max(h, w * frac)
            cr.arc(x + h / 2, y + h / 2, h / 2, 1.5708, 4.7124)
            cr.arc(x + ww - h / 2, y + h / 2, h / 2, -1.5708, 1.5708)
            cr.close_path()
            cr.set_source_rgb(*col)
            cr.fill()
    nwaves = waves if waves is not None else (0 if not vol else 1 if vol <= 33 else 2 if vol <= 66 else 3)
    color = GREY if muted else (label_color or BLUE)
    draw_glyph(cr, color, waves=nwaves, cross=muted, dim=not on)
    if size != 144:
        out = cairo.ImageSurface(cairo.FORMAT_ARGB32, size, size)
        c2 = cairo.Context(out)
        c2.scale(size / 144, size / 144)
        c2.set_source_surface(surf, 0, 0)
        c2.paint()
        surf = out
    buf = io.BytesIO()
    surf.write_to_png(buf)
    return buf.getvalue()


_img_cache = {}


def data_uri(**kw):
    key = tuple(sorted(kw.items()))
    if key not in _img_cache:
        _img_cache[key] = "data:image/png;base64," + base64.b64encode(render_png(**kw)).decode()
    return _img_cache[key]


# --------------------------------------------------------------------------- websocket (client)
class WS:
    def __init__(self, port):
        self.sock = socket.create_connection(("127.0.0.1", port), timeout=10)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall((f"GET / HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                           f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
                           f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise ConnectionError("handshake closed")
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n")[0] + b" ":
            raise ConnectionError("websocket upgrade refused: %r" % head[:80])
        self.buf, self.frag = rest, b""
        self.sock.settimeout(None)

    def send(self, obj):
        self._frame(1, json.dumps(obj).encode())

    def _frame(self, opcode, payload):
        n = len(payload)
        hdr = bytes([0x80 | opcode])
        if n < 126:
            hdr += bytes([0x80 | n])
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        body = bytes(b ^ mask[i & 3] for i, b in enumerate(payload))
        self.sock.sendall(hdr + mask + body)

    def messages(self):
        """Read what is available and return the complete text messages."""
        data = self.sock.recv(65536)
        if not data:
            raise ConnectionError("websocket closed")
        self.buf += data
        out = []
        while len(self.buf) >= 2:
            b0, b1 = self.buf[0], self.buf[1]
            fin, op, masked, n, i = b0 & 0x80, b0 & 0x0F, b1 & 0x80, b1 & 0x7F, 2
            if n == 126:
                if len(self.buf) < 4:
                    break
                n, i = struct.unpack(">H", self.buf[2:4])[0], 4
            elif n == 127:
                if len(self.buf) < 10:
                    break
                n, i = struct.unpack(">Q", self.buf[2:10])[0], 10
            key = b""
            if masked:
                key, i = self.buf[i:i + 4], i + 4
            if len(self.buf) < i + n:
                break
            payload, self.buf = self.buf[i:i + n], self.buf[i + n:]
            if key:
                payload = bytes(b ^ key[j & 3] for j, b in enumerate(payload))
            if op == 8:
                raise ConnectionError("websocket closed by OpenDeck")
            if op == 9:
                self._frame(10, payload)
            elif op in (1, 2):
                self.frag = payload
            elif op == 0:
                self.frag += payload
            if op in (0, 1, 2) and fin:
                out.append(self.frag.decode("utf-8", "replace"))
                self.frag = b""
        return out


# --------------------------------------------------------------------------- mixer link
class Mixer:
    def __init__(self):
        self.sock, self.buf, self.state, self.next_try = None, b"", None, 0.0

    def connect(self):
        try:
            s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            s.settimeout(0.5)
            s.connect(SOCK)
            s.sendall(b'{"cmd":"subscribe"}\n')
        except OSError:
            self.sock = None
            self.next_try = time.time() + 2
            return False
        self.sock, self.buf = s, b""
        return True

    def close(self):
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock, self.state, self.next_try = None, None, time.time() + 2

    def read(self):
        """Returns True when the mixer's state changed."""
        try:
            data = self.sock.recv(65536)
        except socket.timeout:
            return False
        except OSError:
            data = b""
        if not data:
            self.close()
            return True
        self.buf += data
        changed = False
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("event") == "state":
                self.state, changed = msg, True
        return changed

    def send(self, obj):
        if not self.sock:
            return False
        try:
            self.sock.sendall(json.dumps(obj).encode() + b"\n")
            return True
        except OSError:
            self.close()
            return False


# --------------------------------------------------------------------------- plugin logic
def find(items, ident):
    return next((x for x in items or [] if x["id"] == ident), None)


class Plugin:
    def __init__(self, ws, uuid, register_event):
        self.ws, self.uuid = ws, uuid
        self.mixer = Mixer()
        self.ctx = {}     # context -> {"action", "settings", "controller", "last"}
        self.pi = {}      # context -> action (property inspectors currently open)
        ws.send({"event": register_event, "uuid": uuid})

    # ---- what an action should currently show
    def view(self, c):
        st, s, act = self.mixer.state, c["settings"], c["action"]
        if act == A_WINDOW:
            return {"title": "Mixer", "image": dict(vol=None, on=bool(st)), "feedback": None}
        if st is None:
            return {"title": "Mixer\noffline", "image": dict(vol=None, on=False),
                    "feedback": {"title": "Mixer", "value": "offline", "indicator": 0}}
        if act == A_SYSTEM:
            on = st.get("system_default")
            sysch = next((x for x in st["channels"] if x.get("system")), None)
            return {"title": f"{sysch['name'] if sysch else 'System'}\n{'ON' if on else 'OFF'}",
                    "image": dict(vol=None, on=bool(on), muted=False, waves=3 if on else 0),
                    "feedback": None}
        if act == A_MIX:
            m = find(st["mixes"], s.get("mix"))
            if not m:
                return {"title": "Pick\na mix", "image": dict(vol=None, on=False),
                        "feedback": {"title": "Mix", "value": "pick one", "indicator": 0}}
            name, vol, muted, on = m["name"], m["vol"], m["mute"], True
        else:   # channel
            ch = find(st["channels"], s.get("channel"))
            if not ch:
                return {"title": "Pick\na channel", "image": dict(vol=None, on=False),
                        "feedback": {"title": "Channel", "value": "pick one", "indicator": 0}}
            name, vol, muted, on = ch["name"], ch["vol"], ch["mute"], True
            scope = s.get("scope") or "master"
            if scope != "master":
                cell = st["cells"].get(scope, {}).get(ch["id"])
                if cell:
                    vol, muted, on = cell["vol"], cell["mute"] or ch["mute"], cell["on"]
        value = "Muted" if muted else (f"{vol}%" if on else "Off")
        return {"title": f"{name}\n{value}",
                "image": dict(vol=vol, muted=muted, on=on),
                "feedback": {"title": name, "value": value, "indicator": 0 if muted or not on else vol}}

    def render(self, context, force=False):
        c = self.ctx.get(context)
        if not c:
            return
        v = self.view(c)
        key = json.dumps(v, sort_keys=True)
        if key == c["last"] and not force:
            return
        c["last"] = key
        if c["controller"] == "Encoder" and v["feedback"]:
            self.ws.send({"event": "setFeedback", "context": context, "payload": v["feedback"]})
        else:
            self.ws.send({"event": "setImage", "context": context,
                          "payload": {"image": data_uri(**v["image"]), "target": 0}})
            self.ws.send({"event": "setTitle", "context": context,
                          "payload": {"title": v["title"], "target": 0}})

    def render_all(self):
        for context in list(self.ctx):
            self.render(context)
        st = self.mixer.state
        for context, action in self.pi.items():
            self.ws.send({"event": "sendToPropertyInspector", "action": action, "context": context,
                          "payload": {"state": st}})

    # ---- what a key press / dial turn does
    def command(self, c, mode=None, ticks=0):
        s, act = c["settings"], c["action"]
        step = int(s.get("step") or 5)
        if act == A_WINDOW:
            if not self.mixer.sock:
                self.launch_mixer()
                return True
            return self.mixer.send({"cmd": "window", "action": s.get("mode") or "toggle"})
        if act == A_SYSTEM:
            m = s.get("mode") or "toggle"
            return self.mixer.send({"cmd": "system", "enable": "toggle" if m == "toggle" else m == "on"})
        target = s.get("mix") if act == A_MIX else s.get("channel")
        if not target:       # nothing chosen in the settings panel yet
            return False
        base = {"cmd": "mix", "id": target} if act == A_MIX else {"cmd": "channel", "id": target}
        scope = s.get("scope") or "master"
        if act == A_CHANNEL and scope != "master":
            base["mix"] = scope
        mode = mode or s.get("keyMode") or "mute"
        if ticks:
            return self.mixer.send({**base, "delta": ticks * step})
        if mode == "mute":
            return self.mixer.send({**base, "mute": "toggle"})
        if mode == "up":
            return self.mixer.send({**base, "delta": step})
        if mode == "down":
            return self.mixer.send({**base, "delta": -step})
        if mode == "set":
            return self.mixer.send({**base, "vol": int(50 if s.get("setTo") in (None, "") else s["setTo"])})
        if mode == "route" and act == A_CHANNEL and "mix" in base:
            return self.mixer.send({**base, "on": "toggle"})
        return False

    @staticmethod
    def launch_mixer():
        try:
            if shutil.which("gtk-launch"):
                subprocess.Popen(["gtk-launch", "io.github.lawsonf.AudioMixer"],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            else:
                script = os.path.expanduser("~/audio-mixer/audio_mixer.py")
                subprocess.Popen([sys.executable, script], stdout=subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, start_new_session=True)
        except OSError as e:
            log("could not launch the mixer:", e)

    # ---- events from OpenDeck
    def on_message(self, text):
        try:
            m = json.loads(text)
        except ValueError:
            return
        ev, context = m.get("event"), m.get("context")
        p = m.get("payload") or {}
        if ev == "willAppear":
            self.ctx[context] = {"action": m["action"], "settings": p.get("settings") or {},
                                 "controller": p.get("controller") or "Keypad", "last": None}
            self.render(context, force=True)
        elif ev == "willDisappear":
            self.ctx.pop(context, None)
        elif ev == "didReceiveSettings" and context in self.ctx:
            self.ctx[context]["settings"] = p.get("settings") or {}
            self.render(context, force=True)
        elif ev in ("keyDown", "dialDown") and context in self.ctx:
            c = self.ctx[context]
            c["settings"] = p.get("settings") or c["settings"]
            ok = self.command(c, mode="mute" if ev == "dialDown" else None)
            if not ok:
                self.ws.send({"event": "showAlert", "context": context})
        elif ev == "dialRotate" and context in self.ctx:
            c = self.ctx[context]
            c["settings"] = p.get("settings") or c["settings"]
            if c["action"] in (A_CHANNEL, A_MIX):
                self.command(c, ticks=int(p.get("ticks") or 0))
        elif ev == "propertyInspectorDidAppear":
            self.pi[context] = m.get("action")
            self.ws.send({"event": "sendToPropertyInspector", "action": m.get("action"),
                          "context": context, "payload": {"state": self.mixer.state}})
        elif ev == "propertyInspectorDidDisappear":
            self.pi.pop(context, None)
        elif ev == "sendToPlugin":
            self.ws.send({"event": "sendToPropertyInspector", "action": m.get("action"),
                          "context": context, "payload": {"state": self.mixer.state}})

    # ---- main loop
    def run(self):
        sel = selectors.DefaultSelector()
        sel.register(self.ws.sock, selectors.EVENT_READ, "ws")
        reg_sock, reg_fd = None, None
        while True:
            now = time.time()
            if self.mixer.sock is None and now >= self.mixer.next_try:
                if self.mixer.connect():
                    log("connected to the mixer")
                self.render_all()
            if self.mixer.sock is not reg_sock:      # keep the selector in step with the link
                if reg_fd is not None:
                    try:
                        sel.unregister(reg_fd)
                    except (KeyError, ValueError, OSError):
                        pass
                reg_sock, reg_fd = self.mixer.sock, None
                if reg_sock is not None:
                    reg_fd = reg_sock.fileno()
                    sel.register(reg_fd, selectors.EVENT_READ, "mixer")
            for key, _ in sel.select(timeout=0.5):
                if key.data == "ws":
                    for text in self.ws.messages():
                        self.on_message(text)
                else:
                    was = self.mixer.sock
                    if self.mixer.read():
                        if self.mixer.sock is None and was is not None:
                            log("lost the mixer")
                        self.render_all()


def main():
    args = dict(zip(sys.argv[1::2], sys.argv[2::2]))
    port, uuid = int(args["-port"]), args["-pluginUUID"]
    event = args.get("-registerEvent", "registerPlugin")
    while True:
        try:
            Plugin(WS(port), uuid, event).run()
        except (ConnectionError, OSError) as e:
            log("connection to OpenDeck lost:", e)
            return   # OpenDeck restarts the plugin when it wants it


if __name__ == "__main__":
    main()
