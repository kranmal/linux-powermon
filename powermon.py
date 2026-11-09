#!/usr/bin/env python3
"""Linux Powermon: transparent desktop overlay showing electricity use and cost.

LINUX ONLY. Reads real CPU (zenergy / RAPL) and AMD GPU power sensors, adds a
user-set estimate for everything else (board, RAM, drives, fans), divides by PSU
efficiency to estimate wall draw, and multiplies by your unit rate (price per kWh).

Drag with left mouse. Right-click for menu (settings, reset, opacity, quit).
Usage totals persist in ~/.local/share/powermon/usage.json (counts only while running).
"""
import argparse
import glob
import json
import math
import os
import sys
import time
from collections import deque
from datetime import date

import psutil
from PySide6.QtCore import QProcess, QRectF, Qt, QTimer
from PySide6.QtGui import QAction, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFormLayout, QLabel, QLineEdit,
                               QMenu, QWidget)

HIST = 60  # samples (1/s)
W, H = 420, 282
CFG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "powermon")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "powermon")
CFG_FILE = os.path.join(CFG_DIR, "config.json")
USAGE_FILE = os.path.join(DATA_DIR, "usage.json")
BASE_FILE = os.path.join(DATA_DIR, "baseline.json")  # avg wall watts with saver OFF
DEFAULTS = {"rate": 25.0, "currency": "£", "other_w": 35.0, "psu_eff": 90.0}
CPU_TDP, CPU_IDLE = 65.0, 10.0  # fallback estimate when no CPU energy sensor


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f)
        os.replace(tmp, path)
    except OSError:
        pass


def rd(path, default=None):
    try:
        with open(path) as f:
            return float(f.read().strip())
    except (OSError, ValueError):
        return default


def hwmon(name):
    for p in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            if open(f"{p}/name").read().strip() == name:
                return p
        except OSError:
            pass
    return None


def find_cpu_energy():
    """Return list of energy counter files (microjoules) that sum to CPU package energy."""
    z = hwmon("zenergy")
    if z:
        for f in sorted(glob.glob(f"{z}/energy*_label")):
            try:
                if open(f).read().strip().startswith("Esocket"):
                    return [f.replace("_label", "_input")]
            except OSError:
                pass
    rapl = [f for f in glob.glob("/sys/class/powercap/intel-rapl:?/energy_uj")
            if os.access(f, os.R_OK)]
    return rapl


def saver_helper():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in ("/usr/local/bin/powermon-saver", os.path.expanduser("~/.local/bin/powermon-saver"),
              os.path.join(here, "powermon-saver")):
        if os.access(p, os.X_OK):
            return p
    return None


def eco_active():
    gov = open_first("/sys/devices/system/cpu/cpu0/cpufreq/scaling_governor")
    boost = open_first("/sys/devices/system/cpu/cpufreq/boost")
    return gov == "powersave" and boost in (None, "0")


def open_first(path):
    try:
        with open(path) as f:
            return f.read().strip()
    except OSError:
        return None


def find_gpu_power():
    a = hwmon("amdgpu")
    if a:
        for n in ("power1_average", "power1_input"):
            if os.path.exists(f"{a}/{n}"):
                return f"{a}/{n}"
    return None


class SettingsDialog(QDialog):
    def __init__(self, cfg, first=False):
        super().__init__()
        self.setWindowTitle("Powermon settings")
        form = QFormLayout(self)
        if first:
            form.addRow(QLabel("Welcome! Enter your electricity tariff.\n"
                               "Unit rate is on your bill, e.g. 24.5p per kWh."))
        self.cur = QLineEdit(cfg["currency"])
        self.cur.setMaxLength(3)
        self.rate = QDoubleSpinBox()
        self.rate.setRange(0.0, 1000.0)
        self.rate.setDecimals(2)
        self.rate.setSuffix(" pence/cents per kWh")
        self.rate.setValue(cfg["rate"])
        self.other = QDoubleSpinBox()
        self.other.setRange(0.0, 1000.0)
        self.other.setSuffix(" W")
        self.other.setValue(cfg["other_w"])
        self.eff = QDoubleSpinBox()
        self.eff.setRange(50.0, 100.0)
        self.eff.setSuffix(" %")
        self.eff.setValue(cfg["psu_eff"])
        form.addRow("Currency symbol", self.cur)
        form.addRow("Unit rate", self.rate)
        form.addRow("Other parts (board, RAM,\ndrives, fans, est.)", self.other)
        form.addRow("PSU efficiency", self.eff)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)

    def values(self):
        return {"rate": self.rate.value(), "currency": self.cur.text() or "£",
                "other_w": self.other.value(), "psu_eff": self.eff.value()}


class Powermon(QWidget):
    def __init__(self, cfg, demo=False):
        super().__init__()
        self.demo = demo
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnBottomHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowTitle("powermon")
        self.resize(W, H)
        self.cfg = cfg
        self.bg_alpha = 90
        self.cpu_files = find_cpu_energy()
        self.gpu_file = find_gpu_power()
        self.cpu_est = not self.cpu_files
        self.hist = deque([0.0] * HIST, maxlen=HIST)
        self.p = {"cpu": 0.0, "gpu": 0.0, "other": 0.0, "dc": 0.0, "wall": 0.0}
        self._e = self._read_energy()
        self._t = time.monotonic()
        self.session_kwh = 0.0
        self.usage = load_json(USAGE_FILE, {})  # "YYYY-MM-DD" -> wall kWh
        if demo:
            self.usage = {date.today().isoformat(): 1.84, date.today().strftime("%Y-%m-01"): 39.5}
            self.session_kwh = 0.62
        self.base = load_json(BASE_FILE, {"wall": 0.0, "n": 0})  # learned "normal" draw
        self._save_t = time.monotonic()
        self._seen = False
        self.btn_rect = QRectF()
        self.eco_rect = QRectF()
        self.eco = eco_active()
        self.eco_busy = False
        self.eco_msg = ""
        self.eco_proc = None
        psutil.cpu_percent()
        t = QTimer(self)
        t.timeout.connect(self.sample)
        t.start(1000)

    def _read_energy(self):
        vals = [rd(f) for f in self.cpu_files]
        return None if not vals or None in vals else sum(vals)

    # ---- sampling ----
    def sample(self):
        now = time.monotonic()
        dt = now - self._t
        self._t = now
        cpu = None
        if not self.cpu_est:
            e = self._read_energy()
            if e is not None and self._e is not None and e >= self._e and dt > 0:
                cpu = (e - self._e) / 1e6 / dt
            self._e = e
        if cpu is None:
            cpu = CPU_IDLE + psutil.cpu_percent() / 100 * (CPU_TDP - CPU_IDLE)
            if not self.cpu_est and self.p["cpu"]:
                cpu = self.p["cpu"]  # transient read glitch: keep last good value
        if not self.eco_busy:
            self.eco = False if self.demo else eco_active()
        gpu_uw = rd(self.gpu_file, 0.0) if self.gpu_file else 0.0
        gpu = gpu_uw / 1e6
        if self.demo:
            cpu = 62 + 28 * math.sin(now / 5)
            gpu = 95 + 45 * math.sin(now / 9 + 1)
        other = self.cfg["other_w"]
        dc = cpu + gpu + other
        wall = dc / (self.cfg["psu_eff"] / 100)
        self.p = {"cpu": cpu, "gpu": gpu, "other": other, "dc": dc, "wall": wall}
        if not self._seen and self.demo:
            self.hist.extend(
                (cpu + gpu + other) / (self.cfg["psu_eff"] / 100) for cpu, gpu in
                ((62 + 28 * math.sin((now - i) / 5), 95 + 45 * math.sin((now - i) / 9 + 1))
                 for i in range(HIST, 0, -1)))
            self._seen = True
        if not self._seen:
            self.hist.extend([wall] * HIST)
            self._seen = True
        self.hist.append(wall)

        if self.demo:
            self.eco = True
            self.base = {"wall": wall + 41.0, "n": 999}
        elif not self.eco and not self.eco_busy:
            n = self.base["n"] + 1  # running mean at first, then ~5 min moving average
            self.base["n"] = n
            self.base["wall"] += max(1 / n, 0.003) * (wall - self.base["wall"])
        kwh = wall * dt / 3.6e6
        if self.demo:
            self.update()
            return
        self.session_kwh += kwh
        today = date.today().isoformat()
        self.usage[today] = self.usage.get(today, 0.0) + kwh
        if now - self._save_t > 60:
            self.save()
        self.update()

    def save(self):
        self._save_t = time.monotonic()
        save_json(USAGE_FILE, self.usage)
        save_json(BASE_FILE, self.base)

    def saved_watts(self):
        """Watts saved vs the learned normal draw; None until a baseline exists."""
        if self.base["n"] < 30:
            return None
        return max(0.0, self.base["wall"] - self.p["wall"])

    def month_kwh(self):
        prefix = date.today().strftime("%Y-%m")
        return sum(v for k, v in self.usage.items() if k.startswith(prefix))

    def cost(self, kwh):
        return kwh * self.cfg["rate"] / 100

    def money(self, v):
        c = self.cfg["currency"]
        return f"{c}{v:,.2f}" if v < 1000 else f"{c}{v:,.0f}"

    # ---- drawing helpers (same look as sysmon) ----
    def text(self, p, x, y, s, color=QColor("white"), bold=False, size=10,
             align=Qt.AlignLeft, width=0):
        f = QFont("Noto Sans Mono", size)
        f.setBold(bold)
        p.setFont(f)
        r = QRectF(x, y - 14, width or 300, 18)
        p.setPen(QColor(0, 0, 0, 200))
        p.drawText(r.translated(1, 1), align | Qt.AlignVCenter, s)
        p.setPen(color)
        p.drawText(r, align | Qt.AlignVCenter, s)

    def bar(self, p, x, y, w, pct, color):
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 255, 255, 40))
        p.drawRoundedRect(QRectF(x, y, w, 6), 3, 3)
        p.setBrush(color)
        p.drawRoundedRect(QRectF(x, y, w * min(max(pct, 0), 100) / 100, 6), 3, 3)

    def spark(self, p, x, y, w, h, data, color, maxv):
        data = list(data)
        n = len(data)
        path = QPainterPath()
        for i, v in enumerate(data):
            px = x + w * i / (n - 1)
            py = y + h - h * min(v / maxv, 1.0)
            path.moveTo(px, py) if i == 0 else path.lineTo(px, py)
        fill = QPainterPath(path)
        fill.lineTo(x + w, y + h)
        fill.lineTo(x, y + h)
        fill.closeSubpath()
        c = QColor(color)
        c.setAlpha(50)
        p.setPen(Qt.NoPen)
        p.fillPath(fill, c)
        p.setPen(QPen(color, 1.5))
        p.setBrush(Qt.NoBrush)
        p.drawPath(path)

    def section(self, p, x, y, w, title):
        self.text(p, x, y, title, QColor("#9ad1ff"), bold=True, size=9)
        p.setPen(QPen(QColor(255, 255, 255, 50), 1))
        p.drawLine(int(x + len(title) * 7 + 10), int(y - 5), int(x + w), int(y - 5))

    @staticmethod
    def watt_color(w):
        if w < 120:
            return QColor("#5fe3a1")
        if w < 250:
            return QColor("#ffcf5c")
        return QColor("#ff6b6b")

    def row(self, p, x, y, w, label, kwh, cost):
        self.text(p, x, y, label, QColor("#cccccc"), size=9)
        self.text(p, x + 50, y, f"{kwh:7.2f} kWh", bold=True, size=9)
        self.text(p, 0, y, cost, QColor("#ffcf5c"), bold=True, size=9,
                  align=Qt.AlignRight, width=x + w)

    def button(self, p, rect, label, col, filled=False):
        fill = QColor(col)
        fill.setAlpha(60 if filled else 25)
        p.setPen(QPen(col, 1))
        p.setBrush(fill)
        p.drawRoundedRect(rect, 8, 8)
        self.text(p, rect.x(), rect.y() + 19, label, col, bold=True, size=9,
                  align=Qt.AlignHCenter, width=rect.width())

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        pw = self.p
        wall = pw["wall"]
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(10, 12, 18, self.bg_alpha))
        p.drawRoundedRect(QRectF(0, 0, W, self.height()), 14, 14)

        cw = (W - 42) // 2            # column width
        lx, rx = 14, 14 + cw + 14     # left / right column x
        c = self.cfg["currency"]
        sub = "p" if c == "£" else "¢" if c in "$€" else ""

        y = 26
        self.text(p, 14, y, time.strftime("%H:%M:%S"), size=16, bold=True)
        self.text(p, 0, y, f"{self.cfg['rate']:g}{sub}/kWh", QColor("#ffcf5c"), bold=True,
                  size=10, align=Qt.AlignHCenter, width=W)
        self.text(p, 0, y, time.strftime("%a %d %b"), QColor("#cccccc"),
                  align=Qt.AlignRight, width=W - 14)

        # left: power now
        y = 56
        self.section(p, lx, y, cw, "POWER NOW")
        col = self.watt_color(wall)
        self.text(p, lx, y + 30, f"{wall:4.0f} W", col, bold=True, size=20)
        self.text(p, 0, y + 30, f"{self.money(self.cost(wall / 1000))}/h", QColor("#ffcf5c"),
                  bold=True, size=12, align=Qt.AlignRight, width=lx + cw)
        self.spark(p, lx, y + 44, cw, 38, self.hist, col, max(max(self.hist), 100.0))

        # right: breakdown
        self.section(p, rx, y, cw, "BREAKDOWN *=est")
        rows = [("CPU" + ("*" if self.cpu_est else ""), pw["cpu"], QColor("#5fb3ff")),
                ("GPU" + ("" if self.gpu_file else "?"), pw["gpu"], QColor("#c58bff")),
                ("Other*", pw["other"], QColor("#8a93a6")),
                ("PSU loss*", wall - pw["dc"], QColor("#ff9f5c"))]
        ry = y + 26
        for label, w, rc in rows:
            self.text(p, rx, ry, label, QColor("#cccccc"), size=9)
            self.bar(p, rx + 66, ry - 8, cw - 66 - 46, 100 * w / wall if wall else 0, rc)
            self.text(p, 0, ry, f"{w:4.0f} W", rc, bold=True, size=9,
                      align=Qt.AlignRight, width=rx + cw)
            ry += 19

        # usage / projection
        y = 156
        self.section(p, lx, y, cw, "USAGE")
        self.section(p, rx, y, cw, "IF LEFT ON")
        for i, (label, kwh) in enumerate((("Today", self.usage.get(date.today().isoformat(), 0.0)),
                                          ("Session", self.session_kwh),
                                          ("Month", self.month_kwh()))):
            self.row(p, lx, y + 24 + i * 21, cw, label, kwh, self.money(self.cost(kwh)))
        for i, (label, hours) in enumerate((("Day", 24), ("Month", 24 * 30), ("Year", 24 * 365))):
            kwh = wall * hours / 1000
            self.row(p, rx, y + 24 + i * 21, cw, label, kwh, self.money(self.cost(kwh)))

        # buttons
        by = 240
        self.eco_rect = QRectF(14, by, 264, 28)
        if self.eco_busy:
            label = "APPLYING…"
        elif self.eco_msg:
            label = self.eco_msg
        else:
            sv = self.saved_watts()
            if not self.eco:
                label = "ENERGY SAVER: OFF (click = on)"
            elif sv is None:
                label = "SAVER ON: no baseline (click = off)"
            else:
                label = f"SAVER ON: saving ~{sv:.0f} W (click = off)"
        self.button(p, self.eco_rect, label,
                    QColor("#5fe3a1") if self.eco else QColor("#ffcf5c"), self.eco)
        self.btn_rect = QRectF(14 + 264 + 12, by, W - 28 - 264 - 12, 28)
        self.button(p, self.btn_rect, "SETTINGS", QColor("#9ad1ff"))

    # ---- interaction ----
    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.btn_rect.contains(e.position()):
            self.settings()
        elif self.eco_rect.contains(e.position()):
            self.toggle_eco()
        elif self.windowHandle():
            self.windowHandle().startSystemMove()

    def contextMenuEvent(self, e):
        m = QMenu(self)
        s = QAction("Settings (rate, extras)…", m)
        s.triggered.connect(self.settings)
        m.addAction(s)
        eco = QAction("Energy saver: turn " + ("off" if self.eco else "on"), m)
        eco.triggered.connect(self.toggle_eco)
        m.addAction(eco)
        r = QAction("Reset today's + session totals", m)
        r.triggered.connect(self.reset)
        m.addAction(r)
        m.addSeparator()
        for label, a in (("Opacity: clear", 0), ("Opacity: light", 60),
                         ("Opacity: medium", 130), ("Opacity: dark", 200)):
            act = QAction(label, m)
            act.triggered.connect(lambda _=False, a=a: self.set_alpha(a))
            m.addAction(act)
        m.addSeparator()
        q = QAction("Quit", m)
        q.triggered.connect(QApplication.quit)
        m.addAction(q)
        m.exec(e.globalPos())

    def toggle_eco(self):
        if self.eco_busy:
            return
        helper = saver_helper()
        if not helper:
            self.eco_msg = "powermon-saver not installed"
            QTimer.singleShot(4000, self.clear_msg)
            self.update()
            return
        self.eco_busy, self.eco_msg = True, ""
        self.eco_proc = QProcess(self)
        self.eco_proc.finished.connect(self.eco_done)
        self.eco_proc.start("pkexec", [helper, "off" if self.eco else "on"])
        self.update()

    def eco_done(self, code, _status):
        self.eco_busy = False
        self.eco = eco_active()
        if code != 0:  # 126/127 = auth cancelled/failed
            self.eco_msg = "cancelled / not authorised"
            QTimer.singleShot(4000, self.clear_msg)
        self.update()

    def clear_msg(self):
        self.eco_msg = ""
        self.update()

    def settings(self):
        d = SettingsDialog(self.cfg)
        if d.exec():
            self.cfg = d.values()
            save_json(CFG_FILE, self.cfg)
            self.update()

    def reset(self):
        self.usage.pop(date.today().isoformat(), None)
        self.session_kwh = 0.0
        self.save()
        self.update()

    def set_alpha(self, a):
        self.bg_alpha = a
        self.update()


def main():
    if not sys.platform.startswith("linux"):
        sys.exit("Linux Powermon only runs on Linux.")
    ap = argparse.ArgumentParser(description="Transparent electricity-cost overlay (Linux only).")
    ap.add_argument("--demo", action="store_true", help="fake data, no saving (screenshots)")
    ap.add_argument("--setup", action="store_true", help="re-ask tariff settings at start")
    args, qt_args = ap.parse_known_args()
    app = QApplication([sys.argv[0]] + qt_args)
    app.setApplicationName("powermon")
    app.setDesktopFileName("powermon")
    cfg = {**DEFAULTS, **load_json(CFG_FILE, {})}
    if args.demo:
        cfg = {**DEFAULTS, "rate": 21.82}
    elif args.setup or not os.path.exists(CFG_FILE):
        d = SettingsDialog(cfg, first=True)
        if not d.exec():
            if not os.path.exists(CFG_FILE):
                sys.exit("Tariff not set; cancelled.")
        else:
            cfg = d.values()
            save_json(CFG_FILE, cfg)
    w = Powermon(cfg, demo=args.demo)
    if not args.demo:
        app.aboutToQuit.connect(w.save)
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
