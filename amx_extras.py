"""Audio Mixer extras: Soundboard, Web Companion, Profiles, Tuner, MIDI instruments, Settings page."""
import copy
import http.server
import json
import math
import os
import queue
import random
import shutil
import socket
import socketserver
import subprocess
import threading
import time
import zipfile
from array import array

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

try:
    import numpy as np
except ImportError:      # the tuner needs numpy; the soundboard works without it (slower)
    np = None

CONF_DIR = os.path.join(GLib.get_user_config_dir(), "audio-mixer")
SOUNDS_DIR = os.path.join(CONF_DIR, "sounds")
RATE = 48000


def _clear(box):
    while (c := box.get_first_child()):
        box.remove(c)


def file_dialog(parent, title, save=False, multiple=False, done=None, name=None, patterns=None):
    dlg = Gtk.FileDialog(title=title)
    if name:
        dlg.set_initial_name(name)
    if patterns:
        f = Gtk.FileFilter()
        f.set_name(patterns[0])
        for p in patterns[1:]:
            f.add_pattern(p)
        store = Gio.ListStore.new(Gtk.FileFilter)
        store.append(f)
        dlg.set_filters(store)
    win = parent.get_root() if parent is not None else None

    def fin(d, res):
        try:
            if save:
                r = d.save_finish(res)
            elif multiple:
                r = d.open_multiple_finish(res)
            else:
                r = d.open_finish(res)
        except GLib.Error:
            return
        if multiple:
            paths = [r.get_item(i).get_path() for i in range(r.get_n_items())]
            done([p for p in paths if p])
        elif r and r.get_path():
            done(r.get_path())
    if save:
        dlg.save(win, None, fin)
    elif multiple:
        dlg.open_multiple(win, None, fin)
    else:
        dlg.open(win, None, fin)


def _write(win, path, fn, ok_msg):
    """Write a file from a dialog callback, reporting failure instead of raising."""
    try:
        with open(path, "w") as f:
            fn(f)
    except OSError as e:
        win.toast(f"Couldn't save: {e.strerror or e}")
        return
    win.toast(ok_msg)


try:                       # resolved before fork (see amx_effects._pdeathsig)
    import ctypes as _ct
    import signal as _sig
    _prctl = _ct.CDLL(None).prctl
except (OSError, AttributeError):
    _prctl = None


def _pdeathsig():
    """Child side of fork: a synth must not outlive the mixer."""
    if _prctl is not None:
        _prctl(1, int(_sig.SIGTERM))


# =========================================================================== soundboard
class Soundboard:
    """Pads that play audio clips into a mixer channel. State lives in cfg["soundboard"]."""
    AUDIO = ["Audio files", "*.wav", "*.mp3", "*.ogg", "*.oga", "*.opus", "*.flac", "*.m4a", "*.aac",
             "*.webm", "*.mp4", "*.mkv"]

    def __init__(self, app):
        self.app = app
        self._pcm = {}        # sid -> ((path, mtime), samples)
        self._peaks = {}      # (sid, n, start, end) -> list
        self.playing = {}     # sid -> [(proc, t0, dur)]
        self._lock = threading.Lock()

    @property
    def board(self):
        b = self.app.eng.cfg.setdefault("soundboard", {})
        b.setdefault("channel", None)
        b.setdefault("volume", 100)
        b.setdefault("sounds", [])
        for s in b["sounds"]:
            s.setdefault("start", 0.0)
            s.setdefault("end", None)
            s.setdefault("vol", 100)
        return b

    def sound(self, sid):
        sid = str(sid)
        return next((s for s in self.board["sounds"] if s["id"] == sid or s["name"].lower() == sid.lower()), None)

    # ---- audio data
    @staticmethod
    def _decode(path):
        if shutil.which("ffmpeg"):
            try:
                r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", path, "-f", "f32le", "-ac", "2",
                                    "-ar", str(RATE), "pipe:1"], capture_output=True, timeout=120)
                if r.returncode == 0 and r.stdout:
                    return r.stdout
            except (OSError, subprocess.TimeoutExpired):
                pass
        try:                                   # plain 16-bit WAV without ffmpeg
            import wave
            with wave.open(path) as w:
                if w.getsampwidth() != 2:
                    return None
                raw = array("h")
                raw.frombytes(w.readframes(w.getnframes()))
                ch, sr = w.getnchannels(), w.getframerate()
            out = array("f")
            step = sr / RATE
            frames = len(raw) // ch
            i = 0.0
            while int(i) < frames:
                j = int(i) * ch
                left = raw[j] / 32768
                right = raw[j + 1] / 32768 if ch > 1 else left
                out.append(left)
                out.append(right)
                i += step
            return out.tobytes()
        except Exception:
            return None

    def pcm(self, s):
        """float32 interleaved stereo samples (numpy array, or array('f'))."""
        try:
            key = (s["file"], os.path.getmtime(s["file"]))
        except OSError:
            return None
        with self._lock:
            hit = self._pcm.get(s["id"])
        if hit and hit[0] == key:
            return hit[1]
        raw = self._decode(s["file"])
        if raw is None:
            return None
        if np is not None:
            data = np.frombuffer(raw, dtype=np.float32)
        else:
            data = array("f")
            data.frombytes(raw)
        with self._lock:
            self._pcm[s["id"]] = (key, data)
        return data

    def cached(self, s):
        hit = self._pcm.get(s["id"])
        return hit[1] if hit else None

    def preload(self, sounds, done):
        def work():
            for s in sounds:
                self.pcm(s)
                GLib.idle_add(done, s["id"])
        threading.Thread(target=work, daemon=True).start()

    def duration(self, s):
        d = self.cached(s)
        return len(d) / 2 / RATE if d is not None else 0.0

    def peaks(self, s, n):
        d = self.cached(s)
        if d is None or not len(d):
            return None
        key = (s["id"], n, len(d))
        if key in self._peaks:
            return self._peaks[key]
        frames = len(d) // 2
        per = max(1, frames // n)
        if np is not None:
            mono = np.abs(d[: frames * 2].reshape(-1, 2)).max(axis=1)
            mono = mono[: per * (frames // per)].reshape(-1, per).max(axis=1)
            out = [float(x) for x in mono[:n]]
        else:
            out = []
            for i in range(min(n, frames // per)):
                seg = d[i * per * 2:(i + 1) * per * 2]
                out.append(max(max(seg), -min(seg)) if seg else 0.0)
        top = max(out) if out else 1
        out = [x / top if top > 0 else 0 for x in out]
        self._peaks[key] = out
        return out

    # ---- playback
    def target(self):
        cid = self.board.get("channel")
        ch = next((c for c in self.app.eng.cfg["channels"] if c["id"] == cid and c["type"] == "virtual"), None)
        return f"amx_ch_{ch['id']}" if ch else None

    def play(self, sid):
        s = self.sound(sid)
        if not s:
            return False
        d = self.pcm(s)
        if d is None:
            return False
        frames = len(d) // 2
        a = max(0, min(frames, int((s.get("start") or 0) * RATE)))
        b = frames if s.get("end") is None else max(a, min(frames, int(s["end"] * RATE)))
        g = s.get("vol", 100) / 100 * self.board.get("volume", 100) / 100
        seg = d[a * 2:b * 2]
        if np is not None:
            data = (seg * np.float32(g)).astype(np.float32).tobytes()
        else:
            data = array("f", (x * g for x in seg)).tobytes()
        cmd = ["paplay", "--raw", "--format=float32le", f"--rate={RATE}", "--channels=2",
               "--client-name=amx-soundboard", f"--stream-name={s['name']}", "--latency-msec=20"]
        tgt = self.target()
        if tgt:
            cmd += ["-d", tgt]
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            return False

        def feed():
            try:
                p.stdin.write(data)
                p.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        threading.Thread(target=feed, daemon=True).start()
        self.playing.setdefault(s["id"], []).append((p, time.monotonic(), (b - a) / RATE))
        return True

    def _prune(self):
        for sid in list(self.playing):
            self.playing[sid] = [x for x in self.playing[sid] if x[0].poll() is None]
            if not self.playing[sid]:
                del self.playing[sid]

    def is_playing(self, sid):
        self._prune()
        return bool(self.playing.get(sid))

    def progress(self, sid):
        self._prune()
        now = time.monotonic()
        return [min(1.0, (now - t0) / dur) if dur > 0 else 1.0 for _p, t0, dur in self.playing.get(sid, [])]

    def stop(self, sid):
        s = self.sound(sid)
        for p, *_ in self.playing.pop(s["id"] if s else str(sid), []):
            p.kill()

    def stop_all(self):
        for sid in list(self.playing):
            for p, *_ in self.playing.pop(sid, []):
                p.kill()

    # ---- editing
    def new_id(self):
        used = {s["id"] for s in self.board["sounds"]}
        while True:
            i = str(random.randint(1000, 9999))
            if i not in used:
                return i

    def add_files(self, paths):
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        added = []
        for p in paths:
            base = os.path.basename(p)
            dest = os.path.join(SOUNDS_DIR, base)
            n = 1
            while os.path.exists(dest) and not os.path.samefile(dest, p):
                stem, ext = os.path.splitext(base)
                dest = os.path.join(SOUNDS_DIR, f"{stem}-{n}{ext}")
                n += 1
            if not os.path.exists(dest):
                shutil.copy2(p, dest)
            s = {"id": self.new_id(), "name": os.path.splitext(base)[0][:40], "file": dest,
                 "start": 0.0, "end": None, "vol": 100}
            self.board["sounds"].append(s)
            added.append(s)
        return added

    def remove(self, sid):
        s = self.sound(sid)
        if not s:
            return
        self.stop(s["id"])
        self.board["sounds"] = [x for x in self.board["sounds"] if x["id"] != s["id"]]
        if os.path.dirname(os.path.abspath(s["file"])) == SOUNDS_DIR and \
                not any(x["file"] == s["file"] for x in self.board["sounds"]):
            try:
                os.unlink(s["file"])
            except OSError:
                pass
        self._pcm.pop(s["id"], None)

    def export_zip(self, path):
        b = copy.deepcopy(self.board)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            for s in b["sounds"]:
                arc = f"sounds/{s['id']}-{os.path.basename(s['file'])}"
                if os.path.exists(s["file"]):
                    z.write(s["file"], arc)
                s["file"] = arc
            b.pop("channel", None)
            z.writestr("soundboard.json", json.dumps(b, indent=2))

    def import_zip(self, path):
        os.makedirs(SOUNDS_DIR, exist_ok=True)
        with zipfile.ZipFile(path) as z:
            b = json.loads(z.read("soundboard.json"))
            n = 0
            for s in b.get("sounds", []):
                arc = s.get("file", "")
                if not arc.startswith("sounds/") or ".." in arc or arc not in z.namelist():
                    continue
                dest = os.path.join(SOUNDS_DIR, os.path.basename(arc))
                stem, ext = os.path.splitext(dest)
                k = 1
                while os.path.exists(dest):              # never overwrite a sound already on the board
                    dest = f"{stem}-{k}{ext}"
                    k += 1
                with z.open(arc) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)
                used = {x["id"] for x in self.board["sounds"]}
                sid = str(s.get("id")) if str(s.get("id")) not in used else self.new_id()
                self.board["sounds"].append({"id": sid, "name": str(s.get("name", "Sound"))[:40], "file": dest,
                                             "start": float(s.get("start") or 0), "end": s.get("end"),
                                             "vol": int(s.get("vol", 100))})
                n += 1
        return n


class Waveform(Gtk.DrawingArea):
    """A clip's waveform, with its trim region and play position(s)."""

    def __init__(self, board, s, height=46, editable=False, on_trim=None):
        super().__init__(hexpand=True)
        self.set_content_height(height)
        self.board, self.s, self.editable, self.on_trim = board, s, editable, on_trim
        self.set_draw_func(self._draw)
        self._drag = None
        if editable:
            g = Gtk.GestureDrag()
            g.connect("drag-begin", self._begin)
            g.connect("drag-update", self._update)
            g.connect("drag-end", lambda *_: setattr(self, "_drag", None))
            self.add_controller(g)
            self.set_cursor_from_name("col-resize")

    def _pos(self, t, w):
        dur = self.board.duration(self.s) or 1
        return w * t / dur

    def _begin(self, _g, x, _y):
        w = self.get_width()
        dur = self.board.duration(self.s)
        if not dur:
            return
        a = self._pos(self.s.get("start") or 0, w)
        b = self._pos(self.s["end"] if self.s.get("end") is not None else dur, w)
        self._drag = ("start" if abs(x - a) <= abs(x - b) else "end", x)
        self._update(None, 0, 0)

    def _update(self, _g, dx, _dy):
        if not self._drag:
            return
        which, x0 = self._drag
        dur = self.board.duration(self.s)
        t = max(0.0, min(dur, (x0 + dx) / max(1, self.get_width()) * dur))
        end = self.s["end"] if self.s.get("end") is not None else dur
        if which == "start":
            self.s["start"] = round(min(t, end - 0.05), 3)
        else:
            self.s["end"] = round(max(t, (self.s.get("start") or 0) + 0.05), 3)
        self.queue_draw()
        if self.on_trim:
            self.on_trim()

    def _draw(self, _a, cr, w, h):
        dur = self.board.duration(self.s)
        n = max(20, int(w / 3))
        pk = self.board.peaks(self.s, n)
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.rectangle(0, 0, w, h)
        cr.fill()
        if not pk:
            cr.set_source_rgba(1, 1, 1, 0.3)
            cr.set_font_size(10)
            cr.move_to(6, h / 2 + 4)
            cr.show_text("loading…" if os.path.exists(self.s["file"]) else "file missing")
            return
        a = self._pos(self.s.get("start") or 0, w)
        b = self._pos(self.s["end"] if self.s.get("end") is not None else dur, w)
        bw = w / len(pk)
        for i, v in enumerate(pk):
            x = i * bw
            inside = a <= x <= b
            cr.set_source_rgba(0.21, 0.68, 0.96, 0.95 if inside else 0.25)
            hh = max(1, v * (h - 4))
            cr.rectangle(x, (h - hh) / 2, max(1, bw - 1), hh)
            cr.fill()
        if self.editable:
            for x in (a, b):
                cr.set_source_rgb(1, 0.85, 0.3)
                cr.rectangle(x - 1.5, 0, 3, h)
                cr.fill()
        span = max(1e-6, (b - a))
        for f in self.board.progress(self.s["id"]):
            cr.set_source_rgb(1, 1, 1)
            cr.rectangle(a + span * f, 0, 2, h)
            cr.fill()


class SoundboardPage(Gtk.Box):
    def __init__(self, win, board):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8, margin_bottom=16,
                         margin_start=20, margin_end=20)
        self.win, self.board = win, board
        top = Gtk.Box(spacing=10)
        top.append(Gtk.Label(label="Soundboard", xalign=0, hexpand=True, css_classes=["page-title"]))
        stop = Gtk.Button(css_classes=["destructive-action"], valign=Gtk.Align.CENTER)
        stop.set_child(Gtk.Label(label="Stop all"))
        stop.connect("clicked", lambda *_: board.stop_all())
        add = Gtk.Button(css_classes=["suggested-action"], valign=Gtk.Align.CENTER)
        add.set_child(Gtk.Label(label="+ Add sounds"))
        add.connect("clicked", lambda *_: file_dialog(self, "Add sounds", multiple=True, done=self._add,
                                                      patterns=Soundboard.AUDIO))
        top.append(stop)
        top.append(add)
        self.append(top)
        self.append(Gtk.Label(
            label="Click a pad to play it (again to layer another copy); right-click or ⋯ to trim it. Each sound "
                  "has a 4-digit id for Stream Deck buttons and the command line: "
                  f"{ctl_hint()} play <id>.", xalign=0, wrap=True, css_classes=["sub"]))
        self.routing = Gtk.Box(spacing=12, css_classes=["cell"])
        self.append(self.routing)
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.flow = Gtk.FlowBox(selection_mode=Gtk.SelectionMode.NONE, homogeneous=True, max_children_per_line=6,
                                min_children_per_line=1, row_spacing=10, column_spacing=10,
                                valign=Gtk.Align.START)
        sw.set_child(self.flow)
        self.append(sw)
        self.waves = []
        self.rebuild()
        board.preload(board.board["sounds"], lambda _sid: [w.queue_draw() for w in self.waves])
        GLib.timeout_add(60, self._tick)

    def _tick(self):
        if self.get_mapped() and (self.board.playing or self._was_playing):
            for w in self.waves:
                w.queue_draw()
        self._was_playing = bool(self.board.playing)
        return True
    _was_playing = False

    def _add(self, paths):
        added = self.board.add_files(paths)
        self.win.changed()
        self.rebuild()
        self.board.preload(added, lambda _sid: [w.queue_draw() for w in self.waves])

    def build_routing(self):
        r = self.routing
        _clear(r)
        cfg = self.win.cfg
        b = self.board.board
        chans = [c for c in cfg["channels"] if c["type"] == "virtual"]
        opts = [("Default output", None)] + [(c["name"], c["id"]) for c in chans] + [("+ New “Soundboard” channel", "new")]
        vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        vb.append(Gtk.Label(label="Plays on", xalign=0, css_classes=["sub"]))
        dd = Gtk.DropDown.new_from_strings([o[0] for o in opts])
        vals = [o[1] for o in opts]
        dd.set_selected(vals.index(b.get("channel")) if b.get("channel") in vals else 0)

        def pick(d, _p):
            v = vals[d.get_selected()]
            if v == "new":
                ch = {"id": os.urandom(3).hex(), "name": "Soundboard", "type": "virtual",
                      "icon": "audio-speakers-symbolic"}
                cfg["channels"].append(ch)
                for m in cfg["mixes"]:     # heard in every mix that has outputs or a mic, at full level
                    m["levels"][ch["id"]] = {"vol": 100, "mute": False, "on": True}
                b["channel"] = ch["id"]
                self.win.structure_changed()
            else:
                b["channel"] = v
                self.win.changed()
            GLib.idle_add(lambda: (self.build_routing(), False)[1])
        dd.connect("notify::selected", pick)
        vb.append(dd)
        r.append(vb)
        vb2 = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2, hexpand=True)
        vb2.append(Gtk.Label(label="Board volume", xalign=0, css_classes=["sub"]))
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 100, 1)
        sc.add_css_class("plain")
        sc.set_value(b.get("volume", 100))
        sc.connect("value-changed", lambda s: (b.__setitem__("volume", round(s.get_value())), self.win.changed()))
        vb2.append(sc)
        r.append(vb2)
        ch = next((c for c in chans if c["id"] == b.get("channel")), None)
        if ch:   # which mixes hear the board, and how loud: this is the channel's row in the mixer
            mixes = Gtk.Box(spacing=6)
            for m in cfg["mixes"]:
                cell = self.win.eng.cell(m, ch)
                tb = Gtk.ToggleButton(active=cell["on"], tooltip_text=f"Send the soundboard to {m['name']}")
                tb.set_child(Gtk.Label(label=m["name"]))

                def tog(btn, cell=cell):
                    cell["on"] = btn.get_active()
                    self.win.structure_changed()
                tb.connect("toggled", tog)
                mixes.append(tb)
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.append(Gtk.Label(label="Heard in (levels: the channel's row on the Mixes page)", xalign=0,
                                 css_classes=["sub"]))
            box.append(mixes)
            r.append(box)

    def rebuild(self):
        self.build_routing()
        _clear(self.flow)
        self.waves = []
        sounds = self.board.board["sounds"]
        if not sounds:
            self.flow.append(Gtk.Label(label="No sounds yet. Add some with “+ Add sounds”.", css_classes=["sub"],
                                       margin_top=40))
            return
        for s in sounds:
            self.flow.append(self.pad(s))

    def pad(self, s):
        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6, css_classes=["cell", "sb-pad"])
        card.set_size_request(190, -1)
        head = Gtk.Box(spacing=6)
        head.append(Gtk.Label(label=s["name"], xalign=0, hexpand=True, ellipsize=3, css_classes=["chname"]))
        head.append(Gtk.Label(label=s["id"], css_classes=["sub", "numeric"], tooltip_text="Sound id"))
        more = Gtk.Button(icon_name="view-more-symbolic", css_classes=["flat", "dimbtn"], tooltip_text="Edit")
        more.connect("clicked", lambda *_: self.edit(s))
        head.append(more)
        card.append(head)
        w = Waveform(self.board, s)
        self.waves.append(w)
        card.append(w)
        row = Gtk.Box(spacing=6, homogeneous=True)
        play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Play (layers on repeat)")
        play.connect("clicked", lambda *_: self.board.play(s["id"]) or self.win.toast("Couldn't play that file"))
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        stop.connect("clicked", lambda *_: self.board.stop(s["id"]))
        row.append(play)
        row.append(stop)
        card.append(row)
        g = Gtk.GestureClick(button=3)
        g.connect("pressed", lambda *_: self.edit(s))
        card.add_controller(g)
        g1 = Gtk.GestureClick(button=1)
        g1.connect("released", lambda _g, _n, x, y: card.pick(x, y, Gtk.PickFlags.DEFAULT) in (w, card) and
                   (self.board.play(s["id"]) or self.win.toast("Couldn't play that file")))
        card.add_controller(g1)
        return card

    def edit(self, s):
        dlg = Adw.Dialog(content_width=640, title="Edit sound")
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14, margin_top=20, margin_bottom=20,
                      margin_start=22, margin_end=22)
        dlg.set_child(box)
        name = Gtk.Entry(text=s["name"], css_classes=["dlg-name"])
        name.connect("changed", lambda e: e.get_text().strip() and (s.__setitem__("name", e.get_text().strip()[:40]),
                                                                    self.win.changed()))
        box.append(name)
        info = Gtk.Label(xalign=0, css_classes=["sub"])
        box.append(info)

        def upd_info():
            dur = self.board.duration(s)
            end = s["end"] if s.get("end") is not None else dur
            info.set_label(f"Id {s['id']}  ·  plays {s.get('start', 0):.2f}s – {end:.2f}s of {dur:.2f}s  ·  "
                           f"drag the yellow handles to trim")
        wave = Waveform(self.board, s, height=120, editable=True, on_trim=lambda: (upd_info(), self.win.changed()))
        box.append(wave)
        upd_info()
        ctl = Gtk.Box(spacing=10)
        play = Gtk.Button(icon_name="media-playback-start-symbolic", tooltip_text="Preview")
        play.connect("clicked", lambda *_: self.board.play(s["id"]))
        stop = Gtk.Button(icon_name="media-playback-stop-symbolic", tooltip_text="Stop")
        stop.connect("clicked", lambda *_: self.board.stop(s["id"]))
        reset = Gtk.Button(label="Reset trim", css_classes=["flat"])

        def do_reset(*_):
            s["start"], s["end"] = 0.0, None
            wave.queue_draw()
            upd_info()
            self.win.changed()
        reset.connect("clicked", do_reset)
        ctl.append(play)
        ctl.append(stop)
        ctl.append(reset)
        ctl.append(Gtk.Label(label="Clip volume", css_classes=["sub"], margin_start=12))
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 200, 1)
        sc.add_css_class("plain")
        sc.set_hexpand(True)
        sc.set_draw_value(True)
        sc.set_value(s.get("vol", 100))
        sc.connect("value-changed", lambda x: (s.__setitem__("vol", round(x.get_value())), self.win.changed()))
        ctl.append(sc)
        box.append(ctl)
        t = GLib.timeout_add(60, lambda: (wave.queue_draw(), True)[1])
        bottom = Gtk.Box(spacing=10)
        rm = Gtk.Button(label="Delete sound", css_classes=["destructive-action"])

        def delete(*_):
            self.board.remove(s["id"])
            self.win.changed()
            dlg.close()
        rm.connect("clicked", delete)
        bottom.append(rm)
        bottom.append(Gtk.Box(hexpand=True))
        close = Gtk.Button(label="Done", css_classes=["suggested-action"])
        close.connect("clicked", lambda *_: dlg.close())
        bottom.append(close)
        box.append(bottom)
        dlg.connect("closed", lambda *_: (GLib.source_remove(t), self.rebuild()))
        dlg.present(self.win)
        if not self.board.duration(s):
            self.board.preload([s], lambda _i: (wave.queue_draw(), upd_info()))


# =========================================================================== profiles
PROFILE_KEYS = ("channels", "mixes", "system_default", "bypass", "rec_rules")


def profiles(cfg):
    return cfg.setdefault("profiles", {})


def save_profile(cfg, name):
    profiles(cfg)[name] = {k: copy.deepcopy(cfg.get(k)) for k in PROFILE_KEYS if k in cfg}
    cfg["profile"] = name


def switch_profile(cfg, name):
    p = profiles(cfg).get(name)
    if not p:
        return False
    cur = cfg.get("profile")
    if cur and cur in profiles(cfg):          # remember the edits made to the profile we leave
        save_profile(cfg, cur)
    for k in PROFILE_KEYS:
        if k in p:
            cfg[k] = copy.deepcopy(p[k])
    cfg["profile"] = name
    return True


# =========================================================================== MIDI instruments
class Instruments:
    """One FluidSynth per channel that has a MIDI instrument; it plays into that channel and
    listens to every MIDI input (ALSA sequencer, auto-connected)."""

    def __init__(self):
        self.procs = {}     # ch_id -> (Popen, sf2, sink)

    def pids(self):
        return {str(p.pid) for p, *_ in self.procs.values() if p.poll() is None}

    def sync(self, channels, sinks):
        want = {}
        for ch in channels:
            m = ch.get("midi") or {}
            sink = f"amx_ch_{ch['id']}"
            if ch["type"] == "virtual" and m.get("on") and m.get("sf2") and os.path.exists(m["sf2"]) \
                    and sink in sinks:
                want[ch["id"]] = (m["sf2"], sink, float(m.get("gain", 0.6)))
        for cid in list(self.procs):
            p, sf2, sink, gain = self.procs[cid]
            if want.get(cid) != (sf2, sink, gain) or p.poll() is not None:
                p.terminate()
                try:
                    p.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    p.kill()
                del self.procs[cid]
        if not shutil.which("fluidsynth"):
            return
        for cid, (sf2, sink, gain) in want.items():
            if cid in self.procs:
                continue
            try:
                p = subprocess.Popen(
                    ["fluidsynth", "-i", "-s", "-a", "pulseaudio", "-m", "alsa_seq", "-g", f"{gain:.2f}",
                     "-r", str(RATE), "-o", "midi.autoconnect=1", "-o", f"audio.pulseaudio.device={sink}",
                     "-o", "audio.period-size=128", "-o", "audio.periods=2", sf2],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    start_new_session=True, preexec_fn=_pdeathsig)
            except OSError:
                continue
            self.procs[cid] = (p, sf2, sink, gain)

    def stop_all(self):
        self.sync([], set())


# =========================================================================== tuner
NOTES = ["C", "C♯", "D", "D♯", "E", "F", "F♯", "G", "G♯", "A", "A♯", "B"]
TUNINGS = {
    "Chromatic": [],
    "Guitar (standard)": ["E2", "A2", "D3", "G3", "B3", "E4"],
    "Guitar (drop D)": ["D2", "A2", "D3", "G3", "B3", "E4"],
    "Bass (4-string)": ["E1", "A1", "D2", "G2"],
    "Bass (5-string)": ["B0", "E1", "A1", "D2", "G2"],
    "Ukulele": ["G4", "C4", "E4", "A4"],
    "Violin": ["G3", "D4", "A4", "E5"],
}


def note_midi(name):
    n = name.replace("#", "♯")
    octave = int(n[-1])
    return NOTES.index(n[:-1]) + 12 * (octave + 1)


def detect_pitch(x, sr):
    """McLeod pitch method (NSDF peak picking). Returns Hz or None."""
    x = x - x.mean()
    if float(np.sqrt(np.mean(x * x))) < 0.004:
        return None
    n = len(x)
    f = np.fft.rfft(x, 2 * n)
    acf = np.fft.irfft(f * np.conj(f))[:n]
    sq = x * x
    cs = np.cumsum(sq)
    tau = np.arange(n)
    m = cs[n - 1 - tau] + (cs[n - 1] - np.concatenate(([0.0], cs[:-1]))[tau])
    nsdf = 2 * acf / np.maximum(m, 1e-12)
    lo, hi = int(sr / 2100), min(n - 2, int(sr / 25))
    seg = nsdf[:hi]
    # key maxima: the highest point of each positive lobe after the first zero crossing
    neg = np.where(seg < 0)[0]
    if not len(neg):
        return None
    start = neg[0]
    peaks, i = [], start
    while i < hi:
        if seg[i] > 0:
            j = i
            while j < hi and seg[j] > 0:
                j += 1
            k = i + int(np.argmax(seg[i:j]))
            if k >= lo:
                peaks.append(k)
            i = j
        else:
            i += 1
    if not peaks:
        return None
    top = max(seg[k] for k in peaks)
    if top < 0.6:
        return None
    k = next(k for k in peaks if seg[k] >= 0.9 * top)
    if 0 < k < n - 1:
        a, b, c = nsdf[k - 1], nsdf[k], nsdf[k + 1]
        d = a - 2 * b + c
        k = k + (0.5 * (a - c) / d if d else 0)
    return sr / k if k > 0 else None


class TunerWindow(Adw.Window):
    def __init__(self, win):
        super().__init__(title="Tuner", default_width=520, default_height=440, transient_for=win)
        self.win, self.proc, self.watch = win, None, None
        self.buf = np.zeros(8192, dtype=np.float32) if np is not None else None
        self.freq, self.cents, self.note, self.target_name = None, 0.0, "—", None
        self.smooth = None
        tv = Adw.ToolbarView()
        tv.add_top_bar(Adw.HeaderBar(css_classes=["flat"]))
        self.set_content(tv)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8, margin_bottom=16,
                      margin_start=18, margin_end=18)
        tv.set_content(box)
        if np is None:
            box.append(Gtk.Label(label="The tuner needs numpy (python-numpy).", css_classes=["sub"]))
            return
        cfg = win.cfg.setdefault("tuner", {})
        row = Gtk.Box(spacing=8)
        srcs = self.sources()
        self.src_vals = [s[1] for s in srcs]
        self.src_dd = Gtk.DropDown.new_from_strings([s[0] for s in srcs] or ["No inputs"])
        first_hw = next((c["source"] for c in win.cfg["channels"] if c["type"] == "hw"), None)
        pick = cfg.get("source") if cfg.get("source") in self.src_vals else first_hw
        if pick in self.src_vals:
            self.src_dd.set_selected(self.src_vals.index(pick))
        self.src_dd.set_hexpand(True)
        self.src_dd.connect("notify::selected", lambda *_: self.start())
        row.append(self.src_dd)
        self.mode_dd = Gtk.DropDown.new_from_strings(list(TUNINGS))
        modes = list(TUNINGS)
        self.mode_dd.set_selected(modes.index(cfg["mode"]) if cfg.get("mode") in modes else 0)
        self.mode_dd.connect("notify::selected", lambda *_: self.build_strings())
        row.append(self.mode_dd)
        box.append(row)
        row2 = Gtk.Box(spacing=8)
        row2.append(Gtk.Label(label="Concert pitch A4 =", css_classes=["sub"]))
        self.a4 = Gtk.SpinButton.new_with_range(415, 466, 1)
        self.a4.set_value(cfg.get("a4", 440))
        self.a4.connect("value-changed", lambda s: (cfg.__setitem__("a4", int(s.get_value())), win.changed()))
        row2.append(self.a4)
        row2.append(Gtk.Label(label="Hz", css_classes=["sub"]))
        row2.append(Gtk.Box(hexpand=True))
        self.strings = Gtk.Box(spacing=4)
        row2.append(self.strings)
        box.append(row2)
        self.dial = Gtk.DrawingArea(vexpand=True)
        self.dial.set_draw_func(self._draw)
        box.append(self.dial)
        self.manual = None
        self.build_strings()
        self.start()
        self.tick = GLib.timeout_add(70, self._analyse)
        self.connect("close-request", lambda *_: (self.stop(final=True), False)[1])

    def sources(self):
        eng = self.win.eng
        out = []
        for s in eng.sources:
            n = s["name"]
            if n.endswith(".monitor") or n.startswith(("amx_mix_", "amx_mic_")):
                continue
            out.append((s.get("description") or n, n))
        return out

    def build_strings(self):
        _clear(self.strings)
        mode = list(TUNINGS)[self.mode_dd.get_selected()]
        self.win.cfg["tuner"]["mode"] = mode
        self.manual = None
        notes = TUNINGS[mode]
        if not notes:
            return
        auto = Gtk.ToggleButton(label="Auto", active=True)
        self.strings.append(auto)
        auto.connect("toggled", lambda b: b.get_active() and setattr(self, "manual", None))
        for n in notes:
            b = Gtk.ToggleButton(label=n.replace("♯", "#"), group=auto)
            b.connect("toggled", lambda bb, n=n: bb.get_active() and setattr(self, "manual", n))
            self.strings.append(b)

    def start(self):
        self.stop()
        if not self.src_vals:
            return
        src = self.src_vals[min(self.src_dd.get_selected(), len(self.src_vals) - 1)]
        self.win.cfg["tuner"]["source"] = src
        self.win.changed()
        try:
            self.proc = subprocess.Popen(["parec", "-d", src, "--format=float32le", f"--rate={RATE}",
                                          "--channels=1", "--latency-msec=20", "--client-name=amx-tuner"],
                                         stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return
        os.set_blocking(self.proc.stdout.fileno(), False)
        self.watch = GLib.io_add_watch(self.proc.stdout.fileno(), GLib.PRIORITY_DEFAULT,
                                       GLib.IO_IN | GLib.IO_HUP | GLib.IO_ERR, self._data)

    def stop(self, final=False):
        if self.watch:
            GLib.source_remove(self.watch)
            self.watch = None
        if self.proc:
            self.proc.kill()
            self.proc.wait()
            self.proc = None
        if final and getattr(self, "tick", None):
            GLib.source_remove(self.tick)
            self.tick = None

    def _data(self, fd, _cond):
        try:
            data = os.read(fd, 65536)
        except BlockingIOError:
            return True
        except OSError:
            self.watch = None
            return False
        if not data:
            self.watch = None
            return False
        a = np.frombuffer(data[: len(data) // 4 * 4], dtype=np.float32)
        if len(a) >= len(self.buf):
            self.buf[:] = a[-len(self.buf):]
        else:
            self.buf = np.roll(self.buf, -len(a))
            self.buf[-len(a):] = a
        return True

    def _analyse(self):
        if not self.get_visible():
            return True
        mode = list(TUNINGS)[self.mode_dd.get_selected()]
        low = mode.startswith("Bass")
        f = detect_pitch(self.buf.astype(np.float64) if low else self.buf[-4096:].astype(np.float64), RATE)
        a4 = self.a4.get_value()
        if f:
            self.smooth = f if self.smooth is None or abs(math.log2(f / self.smooth)) > 0.08 \
                else self.smooth * 0.7 + f * 0.3
            f = self.smooth
            midi = 69 + 12 * math.log2(f / a4)
            notes = TUNINGS[mode]
            if self.manual:
                tgt = note_midi(self.manual)
            elif notes:
                tgt = min((note_midi(n) for n in notes), key=lambda m: abs(m - midi))
            else:
                tgt = round(midi)
            self.cents = max(-50.0, min(50.0, (midi - tgt) * 100))
            self.note = f"{NOTES[tgt % 12]}{tgt // 12 - 1}"
            self.freq = f
            self.target_hz = a4 * 2 ** ((tgt - 69) / 12)
        else:
            self.freq = None
        self.dial.queue_draw()
        return True

    def _draw(self, _a, cr, w, h):
        cx, cy, r = w / 2, h * 0.86, min(w * 0.45, h * 0.78)
        cr.set_line_width(2)
        for c in range(-50, 51, 5):
            ang = math.pi * (1.5 + c / 100 * 0.9)
            r1 = r * (0.86 if c % 25 else 0.8)
            cr.set_source_rgba(1, 1, 1, 0.5 if c % 25 == 0 else 0.2)
            cr.move_to(cx + r1 * math.cos(ang), cy + r1 * math.sin(ang))
            cr.line_to(cx + r * math.cos(ang), cy + r * math.sin(ang))
            cr.stroke()
        live = self.freq is not None
        good = live and abs(self.cents) < 5
        col = (0.45, 0.85, 0.35) if good else (0.21, 0.68, 0.96) if live else (0.4, 0.4, 0.4)
        ang = math.pi * (1.5 + (self.cents if live else 0) / 100 * 0.9)
        cr.set_source_rgb(*col)
        cr.set_line_width(4)
        cr.move_to(cx, cy)
        cr.line_to(cx + r * 0.92 * math.cos(ang), cy + r * 0.92 * math.sin(ang))
        cr.stroke()
        cr.arc(cx, cy, 7, 0, 2 * math.pi)
        cr.fill()
        cr.select_font_face("Sans", 0, 1)
        cr.set_font_size(min(64, r * 0.45))
        t = self.note if live else "—"
        ext = cr.text_extents(t)
        ty = cy - r * 0.35
        pad = 10
        cr.set_source_rgb(0.14, 0.14, 0.15)
        cr.rectangle(cx - ext.width / 2 - pad, ty + ext.y_bearing - pad, ext.width + 2 * pad, ext.height + 2 * pad)
        cr.fill()
        cr.set_source_rgb(*col) if live else cr.set_source_rgba(1, 1, 1, 0.5)
        cr.move_to(cx - ext.width / 2 - ext.x_bearing, ty)
        cr.show_text(t)
        cr.set_font_size(13)
        cr.select_font_face("Sans", 0, 0)
        sub = (f"{self.freq:.1f} Hz   ·   {self.cents:+.0f} cents   ·   target {self.target_hz:.1f} Hz"
               if live else "Play a note")
        ext = cr.text_extents(sub)
        cr.set_source_rgba(1, 1, 1, 0.6)
        cr.move_to(cx - ext.width / 2, cy - r * 0.18)
        cr.show_text(sub)


# =========================================================================== how to run the mixer
def installed():
    """True for the packaged install (/usr/share/audio-mixer with an `audio-mixer` command)."""
    return os.path.dirname(os.path.abspath(__file__)) == "/usr/share/audio-mixer" \
        and bool(shutil.which("audio-mixer"))


def run_command():
    """The command that starts this mixer, for showing to the user and for autostart."""
    if installed():
        return "audio-mixer"
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audio_mixer.py")
    return 'python3 "{}"'.format(script.replace("\\", "\\\\").replace('"', '\\"'))


def ctl_hint():
    """Short form of the control command for help texts."""
    return "audio-mixer ctl" if installed() else "python3 audio_mixer.py ctl"


# =========================================================================== start at login
AUTOSTART_FILE = os.path.join(GLib.get_user_config_dir(), "autostart", "io.github.lawsonf.AudioMixer.desktop")


def autostart_enabled():
    return os.path.exists(AUTOSTART_FILE)


def set_autostart(on, hidden=True):
    """Add or remove the XDG autostart entry. hidden: start in the tray instead of opening the window."""
    if not on:
        try:
            os.remove(AUTOSTART_FILE)
        except FileNotFoundError:
            pass
        return
    cmd = run_command()
    if hidden:
        cmd += " --hidden"
    os.makedirs(os.path.dirname(AUTOSTART_FILE), exist_ok=True)
    with open(AUTOSTART_FILE + ".tmp", "w") as f:
        f.write("[Desktop Entry]\nType=Application\nName=Audio Mixer\n"
                "Comment=Wave Link style software mixer for PipeWire\n"
                f"Exec={cmd}\nIcon=io.github.lawsonf.AudioMixer\nTerminal=false\n"
                "X-GNOME-Autostart-enabled=true\n")
    os.replace(AUTOSTART_FILE + ".tmp", AUTOSTART_FILE)


# =========================================================================== web companion
COMPANION_PAGE =os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "companion.html")


def companion_html():
    try:
        with open(COMPANION_PAGE, "rb") as f:
            return f.read()
    except OSError:
        return b"<!doctype html><title>Audio Mixer</title><p>web/companion.html is missing.</p>"

MANIFEST = json.dumps({"name": "Audio Mixer Console", "short_name": "Mixer", "display": "standalone", "start_url": "/",
                       "background_color": "#151517", "theme_color": "#151517", "icons": []})


class _Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


class Companion:
    """A phone / tablet remote served over HTTP: page + JSON commands + Server-Sent Events.
    No password: anyone who can reach the port can drive the mixer. Off unless switched on."""

    def __init__(self, app):
        self.app = app
        self.httpd = None
        self.state = "{}"
        self.meters = "{}"
        self.version = 0
        self.mversion = 0
        self.timer = None
        self.error = None

    @property
    def running(self):
        return self.httpd is not None

    def start(self, port):
        if self.httpd:
            return True
        comp = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body, ctype):
                b = body.encode() if isinstance(body, str) else body
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(b)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path in ("/", "/index.html"):
                    self._send(200, companion_html(), "text/html; charset=utf-8")
                elif self.path == "/manifest.json":
                    self._send(200, MANIFEST, "application/manifest+json")
                elif self.path == "/api/state":
                    self._send(200, comp.state, "application/json")
                elif self.path == "/api/events":
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    sv = mv = -1
                    last = time.monotonic()
                    try:
                        while comp.httpd is not None:
                            out = b""
                            if comp.version != sv:
                                sv = comp.version
                                out += b"event: state\ndata: " + comp.state.encode() + b"\n\n"
                            if comp.mversion != mv:
                                mv = comp.mversion
                                out += b"event: meters\ndata: " + comp.meters.encode() + b"\n\n"
                            if not out and time.monotonic() - last > 15:
                                out = b": ping\n\n"
                            if out:
                                self.wfile.write(out)
                                self.wfile.flush()
                                last = time.monotonic()
                            time.sleep(0.05)
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        pass
                else:
                    self._send(404, "not found", "text/plain")

            def do_POST(self):
                if self.path != "/api/cmd":
                    return self._send(404, "not found", "text/plain")
                try:
                    n = min(int(self.headers.get("Content-Length") or 0), 65536)
                    msg = json.loads(self.rfile.read(n) or b"{}")
                    if not isinstance(msg, dict):
                        raise ValueError
                except ValueError:
                    return self._send(400, '{"error":"bad json"}', "application/json")
                q = queue.Queue()

                def run():
                    try:
                        q.put(comp.app.control.command(msg))
                    except Exception as e:      # never let a remote command take the mixer down
                        q.put({"error": str(e)})
                    return False
                GLib.idle_add(run)
                try:
                    res = q.get(timeout=5)
                except queue.Empty:
                    res = {"error": "timeout"}
                self._send(200, json.dumps(res), "application/json")

        try:
            self.httpd = _Server(("0.0.0.0", int(port)), H)
        except OSError as e:
            self.error = str(e)
            self.httpd = None
            return False
        self.error = None
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.timer = GLib.timeout_add(100, self._update)
        self._update()
        return True

    def stop(self):
        h, self.httpd = self.httpd, None
        if self.timer:
            GLib.source_remove(self.timer)
            self.timer = None
        if h:
            threading.Thread(target=lambda: (h.shutdown(), h.server_close()), daemon=True).start()

    _n = 0

    def _update(self):
        if not self.httpd:
            return False
        try:
            st = json.dumps(self.app.control.state(), sort_keys=True)
        except Exception:
            return True
        if st != self.state:
            self.state = st
            self.version += 1
        self._n += 1
        win = self.app.win
        if win:
            lv = {}
            for cid, v in win.meters.level.items():
                lv[cid] = round(max(0.0, min(1.0, (20 * math.log10(v) + 54) / 54)) if v > 0.0005 else 0.0, 3)
            m = json.dumps(lv)
            if m != self.meters:
                self.meters = m
                self.mversion += 1
        return True

    @staticmethod
    def urls(port):
        out = []
        try:
            r = subprocess.run(["ip", "-o", "-4", "addr", "show"], capture_output=True, text=True, timeout=2)
            for line in r.stdout.splitlines():
                parts = line.split()
                if len(parts) >= 4 and parts[1] != "lo":
                    out.append(f"http://{parts[3].split('/')[0]}:{port}")
        except (OSError, subprocess.TimeoutExpired):
            pass
        out.append(f"http://{socket.gethostname()}.local:{port}")
        return out


# =========================================================================== settings page
LATENCIES = [("Lowest (3 ms buffers, ~4 ms)", 3), ("Low (5 ms buffers, ~9 ms) — default", 5),
             ("Relaxed (10 ms buffers, ~14 ms)", 10), ("Safe (20 ms buffers, ~35 ms)", 20),
             ("Very safe (40 ms buffers)", 40)]


class SettingsPage(Gtk.Box):
    def __init__(self, win):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=12, margin_top=8, margin_bottom=16,
                         margin_start=20, margin_end=20)
        self.win = win
        self.append(Gtk.Label(label="Settings", xalign=0, css_classes=["page-title"]))
        sw = Gtk.ScrolledWindow(vexpand=True, hscrollbar_policy=Gtk.PolicyType.NEVER)
        self.inner = Adw.Clamp(maximum_size=860)
        self.body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=22)
        self.inner.set_child(self.body)
        sw.set_child(self.inner)
        self.append(sw)
        self.rebuild()

    def group(self, title, desc=None):
        g = Adw.PreferencesGroup(title=title, description=desc)
        self.body.append(g)
        return g

    def rebuild(self):
        _clear(self.body)
        win, cfg, app = self.win, self.win.cfg, self.win.app

        # ---- startup
        g = self.group("Startup")
        hidden = cfg.get("autostart_hidden", True)
        at = Adw.SwitchRow(title="Start at login", subtitle="Open the mixer automatically when you log in",
                           active=autostart_enabled())
        th = Adw.SwitchRow(title="Start in the tray", subtitle="At login, run in the system tray without "
                                                              "opening the window",
                           active=hidden, sensitive=at.get_active())

        def apply_autostart(*_a):
            cfg["autostart_hidden"] = th.get_active()
            win.changed()
            th.set_sensitive(at.get_active())
            try:
                set_autostart(at.get_active(), th.get_active())
            except OSError as e:
                win.toast(f"Couldn't change autostart: {e}")
        at.connect("notify::active", apply_autostart)
        th.connect("notify::active", apply_autostart)
        g.add(at)
        g.add(th)

        # ---- audio
        g = self.group("Audio")
        vals = [v for _l, v in LATENCIES]
        cur = win.eng.link_latency()
        lat = Adw.ComboRow(title="Latency", subtitle="Buffer per routing hop. Lower is snappier; raise it if you "
                                                     "hear crackles.",
                           model=Gtk.StringList.new([l for l, _v in LATENCIES]),
                           selected=vals.index(cur) if cur in vals else 1)

        def set_lat(r, _p):
            cfg["latency_ms"] = vals[r.get_selected()]
            win.changed()
            win.eng.relink()
            win.toast(f"Latency set to {cfg['latency_ms']} ms buffers")
        lat.connect("notify::selected", set_lat)
        g.add(lat)
        fxh = win.eng.fx
        g.add(Adw.ActionRow(
            title="Effects engine",
            subtitle=("Ready" + (f" — {fxh.msg}" if fxh.msg else "") if fxh.ok else f"Unavailable: {fxh.msg}"),
            subtitle_lines=3))

        # ---- profiles
        g = self.group("Profiles", "A profile is a whole mixer setup: channels, levels, mutes, routing and "
                                   "effects. Switching keeps the edits you made to the profile you leave.")
        er = Adw.EntryRow(title="Save the current setup as…", show_apply_button=True)

        def save(e):
            n = e.get_text().strip()
            if n:
                save_profile(cfg, n)
                win.changed()
                win.toast(f"Saved profile “{n}”")
                GLib.idle_add(lambda: (self.rebuild(), False)[1])
        er.connect("apply", save)
        g.add(er)
        for name in sorted(profiles(cfg), key=str.lower):
            r = Adw.ActionRow(title=GLib.markup_escape_text(name),
                              subtitle="Current" if cfg.get("profile") == name else "")
            if cfg.get("profile") != name:
                b = Gtk.Button(label="Switch", valign=Gtk.Align.CENTER)
                b.connect("clicked", lambda _b, n=name: (win.switch_profile(n), self.rebuild()))
                r.add_suffix(b)
            ren = Gtk.MenuButton(icon_name="document-edit-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                 tooltip_text="Rename")
            pop = Gtk.Popover()
            ent = Gtk.Entry(text=name, margin_top=6, margin_bottom=6, margin_start=6, margin_end=6)

            def rename(e, n=name, pop=pop):
                new = e.get_text().strip()
                if new and new != n and new not in profiles(cfg):
                    profiles(cfg)[new] = profiles(cfg).pop(n)
                    if cfg.get("profile") == n:
                        cfg["profile"] = new
                    win.changed()
                pop.popdown()
                GLib.idle_add(lambda: (self.rebuild(), False)[1])
            ent.connect("activate", rename)
            pop.set_child(ent)
            ren.set_popover(pop)
            r.add_suffix(ren)
            ex = Gtk.Button(icon_name="document-save-as-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Export")

            def export(_b, n=name):
                def done(path):
                    _write(win, path, lambda f: json.dump({"audio_mixer_profile": n, "data": profiles(cfg)[n]},
                                                          f, indent=2), "Profile exported")
                file_dialog(self, "Export profile", save=True, done=done, name=f"{n}.json")
            ex.connect("clicked", export)
            r.add_suffix(ex)
            rm = Gtk.Button(icon_name="user-trash-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                            tooltip_text="Delete")

            def delete(_b, n=name):
                profiles(cfg).pop(n, None)
                if cfg.get("profile") == n:
                    cfg.pop("profile", None)
                win.changed()
                self.rebuild()
            rm.connect("clicked", delete)
            r.add_suffix(rm)
            g.add(r)
        imp = Adw.ButtonRow(title="Import a profile…")

        def do_import(*_):
            def done(path):
                try:
                    with open(path) as f:
                        d = json.load(f)
                    n, data = d["audio_mixer_profile"], d["data"]
                    if not isinstance(data, dict) or "channels" not in data:
                        raise ValueError
                except (OSError, ValueError, KeyError, TypeError):
                    win.toast("That file isn't an Audio Mixer profile")
                    return
                while n in profiles(cfg):
                    n += " (imported)"
                profiles(cfg)[n] = data
                win.changed()
                self.rebuild()
                win.toast(f"Imported profile “{n}”")
            file_dialog(self, "Import profile", done=done, patterns=["Profiles", "*.json"])
        imp.connect("activated", do_import)
        g.add(imp)

        # ---- companion
        comp = app.companion
        cc = cfg.setdefault("companion", {"port": 8787, "autostart": False})
        g = self.group("Web Companion",
                       "Control the mixer and the soundboard from a phone or tablet on your network, in a browser. "
                       "There is no password: anyone who can reach this computer on the port can change your mixer "
                       "while it runs. Leave it off on networks you don't control.")
        on = Adw.SwitchRow(title="Run the companion server", active=comp.running,
                           subtitle=f"Couldn't start: {comp.error}" if comp.error and not comp.running else "")

        def toggle(r, _p):
            if r.get_active():
                if not comp.start(cc.get("port", 8787)):
                    win.toast(f"Couldn't start: {comp.error}")
            else:
                comp.stop()
            GLib.idle_add(lambda: (self.rebuild(), False)[1])
        on.connect("notify::active", toggle)
        g.add(on)
        port = Adw.SpinRow.new_with_range(1024, 65535, 1)
        port.set_title("Port")
        port.set_value(cc.get("port", 8787))
        port.set_sensitive(not comp.running)
        port.connect("notify::value", lambda r, _p: (cc.__setitem__("port", int(r.get_value())), win.changed()))
        g.add(port)
        auto = Adw.SwitchRow(title="Start it with the mixer", active=bool(cc.get("autostart")))
        auto.connect("notify::active", lambda r, _p: (cc.__setitem__("autostart", r.get_active()), win.changed()))
        g.add(auto)
        if comp.running:
            for u in comp.urls(cc.get("port", 8787)):
                r = Adw.ActionRow(title=u, title_selectable=True)
                cp = Gtk.Button(icon_name="edit-copy-symbolic", valign=Gtk.Align.CENTER, css_classes=["flat"],
                                tooltip_text="Copy")
                cp.connect("clicked", lambda _b, u=u: (Gdk.Display.get_default().get_clipboard().set(u),
                                                       win.toast("Copied")))
                r.add_suffix(cp)
                g.add(r)

        # ---- backups
        g = self.group("Backups")
        r = Adw.ActionRow(title="Whole mixer configuration", subtitle="Channels, mixes, deck, profiles, settings")
        for label, fn in (("Export…", self.export_cfg), ("Import…", self.import_cfg)):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_, fn=fn: fn())
            r.add_suffix(b)
        g.add(r)
        r = Adw.ActionRow(title="Soundboard", subtitle="All sounds with their trims and levels, as one .zip")
        for label, fn in (("Export…", self.export_board), ("Import…", self.import_board)):
            b = Gtk.Button(label=label, valign=Gtk.Align.CENTER)
            b.connect("clicked", lambda *_, fn=fn: fn())
            r.add_suffix(b)
        g.add(r)

    def export_cfg(self):
        def done(path):
            _write(self.win, path, lambda f: json.dump(self.win.cfg, f, indent=2), "Configuration exported")
        file_dialog(self, "Export configuration", save=True, done=done, name="audio-mixer-config.json")

    def import_cfg(self):
        def done(path):
            try:
                with open(path) as f:
                    d = json.load(f)
                if not isinstance(d, dict) or not d.get("mixes") or d.get("channels") is None:
                    raise ValueError
            except (OSError, ValueError):
                self.win.toast("That file isn't an Audio Mixer configuration")
                return
            self.win.replace_config(d)
            self.rebuild()
        file_dialog(self, "Import configuration", done=done, patterns=["Configuration", "*.json"])

    def export_board(self):
        def done(path):
            try:
                self.win.app.board.export_zip(path)
            except OSError as e:
                self.win.toast(f"Couldn't export: {e.strerror or e}")
                return
            self.win.toast("Soundboard exported")
        file_dialog(self, "Export soundboard", save=True, done=done, name="soundboard.zip")

    def import_board(self):
        def done(path):
            try:
                n = self.win.app.board.import_zip(path)
            except (OSError, ValueError, KeyError, zipfile.BadZipFile):
                self.win.toast("That file isn't a soundboard export")
                return
            self.win.changed()
            self.win.soundboard_page.rebuild()
            self.win.toast(f"Imported {n} sound{'s' if n != 1 else ''}")
        file_dialog(self, "Import soundboard", done=done, patterns=["Soundboard", "*.zip"])
