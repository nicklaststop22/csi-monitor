#!/usr/bin/env python3
"""
CSI Monitor - live Channel State Information dashboard for the ESP32-CSI-Tool.

Reads CSI_DATA frames from the AP (ESP32 running active_ap) over serial and shows:
  * a big scrolling WATERFALL heatmap  (subcarrier amplitude over time)
  * a live SPECTRUM line                (amplitude across subcarriers, now)
  * a MOTION TIMELINE                   (channel-motion intensity, scrolling)
  * a PRESENCE meter                    (rolling variance vs. adjustable threshold)
  * live stats (pkt/s, RSSI, frames) and a per-device (MAC) selector
  * one-click CSV recording

What the "motion" number means: how much the channel state is changing frame to
frame. A still room -> steady channel -> low motion. A person moving disturbs the
multipath -> the subcarrier amplitudes wobble -> motion rises. It detects THAT
someone is moving nearby; it does not and cannot locate them (one receive antenna).

Single-file, standard-library GUI (tkinter) + pyserial. Package with PyInstaller.
"""
import sys, time, math, threading, queue, collections, csv as csvmod

try:
    import serial
    from serial.tools import list_ports
except ImportError:
    serial = None
    list_ports = None

import tkinter as tk
from tkinter import ttk

BAUD = 921600

BG      = "#0a0e12"
PANEL   = "#0f151b"
GRID    = "#1c2a33"
GREEN   = "#39ff8b"
GREEN_D = "#1f7a47"
AMBER   = "#ffcc44"
RED     = "#ff4d4d"
CYAN    = "#45d6ff"
TXT     = "#bfe9d4"
TXT_DIM = "#5f7d70"


def amp_color(frac):
    """0..1 -> thermal gradient (dark teal -> green -> amber -> red)."""
    frac = max(0.0, min(1.0, frac))
    stops = [(0.0, (0x0a, 0x24, 0x1c)), (0.35, (0x1f, 0x7a, 0x47)),
             (0.65, (0x39, 0xff, 0x8b)), (0.85, (0xff, 0xcc, 0x44)),
             (1.0,  (0xff, 0x4d, 0x4d))]
    for i in range(len(stops) - 1):
        a, ca = stops[i]; b, cb = stops[i + 1]
        if frac <= b:
            t = (frac - a) / (b - a + 1e-9)
            r = int(ca[0] * (1 - t) + cb[0] * t)
            g = int(ca[1] * (1 - t) + cb[1] * t)
            bl = int(ca[2] * (1 - t) + cb[2] * t)
            return f"#{r:02x}{g:02x}{bl:02x}"
    return "#ff4d4d"


def parse_csi(line):
    if not line.startswith("CSI_DATA"):
        return None
    try:
        head, arr = line.split("[", 1)
        arr = arr.split("]", 1)[0]
        f = head.split(",")
        mac = f[2]; rssi = int(f[3])
        vals = [int(x) for x in arr.split()]
        amps = []
        for i in range(0, len(vals) - 1, 2):
            im = vals[i]; re = vals[i + 1]
            amps.append(math.sqrt(re * re + im * im))
        return (mac, rssi, amps) if amps else None
    except (ValueError, IndexError):
        return None


class SerialReader(threading.Thread):
    def __init__(self, port, q):
        super().__init__(daemon=True)
        self.port = port; self.q = q; self.stop = threading.Event()
    def run(self):
        try:
            ser = serial.Serial(self.port, BAUD, timeout=1)
            ser.dtr = False; ser.rts = False; ser.reset_input_buffer()
        except Exception as e:
            self.q.put(("ERR", str(e))); return
        while not self.stop.is_set():
            try:
                raw = ser.readline().decode("utf-8", errors="ignore").strip()
            except Exception:
                continue
            rec = parse_csi(raw)
            if rec: self.q.put(("CSI", rec))
        try: ser.close()
        except Exception: pass


class CSIMonitor(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("CSI MONITOR  ·  ESP32 WiFi Channel-State Sensing")
        self.configure(bg=BG)
        self.geometry("1240x760"); self.minsize(1000, 640)

        self.q = queue.Queue()
        self.reader = None
        self.last_amps = None
        self.last_rssi = 0
        self.sel_mac = None
        self.macs = {}
        self.baselines = {}
        self.waterfall = collections.deque(maxlen=320)
        self.motion_hist = collections.deque(maxlen=320)
        self.pkt_times = collections.deque(maxlen=300)
        self.peak_amp = 1.0
        self.frame_count = 0
        self.motion_level = 0.0
        self.recording = False
        self.rec_rows = []

        self._build_ui()
        self.after(33, self._tick)

    def _build_ui(self):
        top = tk.Frame(self, bg=PANEL, height=48); top.pack(fill="x"); top.pack_propagate(False)
        tk.Label(top, text="〜 CSI MONITOR", bg=PANEL, fg=GREEN,
                 font=("Consolas", 15, "bold")).pack(side="left", padx=14)
        tk.Label(top, text="PORT", bg=PANEL, fg=TXT_DIM, font=("Consolas", 9)).pack(side="left", padx=(8, 2))
        self.port_var = tk.StringVar(value="COM27")
        self.port_cb = ttk.Combobox(top, textvariable=self.port_var, width=9,
                                    values=self._ports(), font=("Consolas", 10)); self.port_cb.pack(side="left")
        self.conn_btn = tk.Button(top, text="CONNECT", command=self._toggle, bg=GREEN_D, fg="#eafff2",
                                  relief="flat", font=("Consolas", 10, "bold"), padx=14, pady=4,
                                  activebackground=GREEN); self.conn_btn.pack(side="left", padx=10)
        tk.Label(top, text="SOURCE", bg=PANEL, fg=TXT_DIM, font=("Consolas", 9)).pack(side="left", padx=(8, 2))
        self.mac_var = tk.StringVar(value="(auto)")
        self.mac_cb = ttk.Combobox(top, textvariable=self.mac_var, width=20,
                                   values=["(auto)"], font=("Consolas", 9)); self.mac_cb.pack(side="left")
        self.mac_cb.bind("<<ComboboxSelected>>", self._pick_mac)
        tk.Label(top, text="SENS", bg=PANEL, fg=TXT_DIM, font=("Consolas", 9)).pack(side="left", padx=(12, 2))
        self.sens = tk.Scale(top, from_=1, to=100, orient="horizontal", length=110, bg=PANEL, fg=TXT,
                             troughcolor=GRID, highlightthickness=0, showvalue=False); self.sens.set(35); self.sens.pack(side="left")
        tk.Label(top, text="TRIG", bg=PANEL, fg=AMBER, font=("Consolas", 9)).pack(side="left", padx=(12, 2))
        self.trig = tk.Scale(top, from_=1, to=100, orient="horizontal", length=110, bg=PANEL, fg=AMBER,
                             troughcolor=GRID, highlightthickness=0, showvalue=True,
                             font=("Consolas", 7)); self.trig.set(45); self.trig.pack(side="left")
        self.rec_btn = tk.Button(top, text="● REC", command=self._toggle_rec, bg=PANEL, fg=RED, relief="flat",
                                 font=("Consolas", 10, "bold"), padx=10, pady=4, activebackground="#2a1114")
        self.rec_btn.pack(side="right", padx=12)

        main = tk.Frame(self, bg=BG); main.pack(fill="both", expand=True)
        left = tk.Frame(main, bg=BG); left.pack(side="left", fill="both", expand=True, padx=(8, 4), pady=8)
        self.wf = tk.Canvas(left, bg=PANEL, highlightthickness=1, highlightbackground=GRID)
        self.wf.pack(fill="both", expand=True, pady=(0, 6))
        self.mt = tk.Canvas(left, bg=PANEL, highlightthickness=1, highlightbackground=GRID, height=150)
        self.mt.pack(fill="x")

        right = tk.Frame(main, bg=BG, width=360); right.pack(side="right", fill="y", padx=(4, 8), pady=8)
        right.pack_propagate(False)
        self.presence = tk.Canvas(right, bg=PANEL, highlightthickness=1, highlightbackground=GRID, height=230)
        self.presence.pack(fill="x", pady=(0, 6))
        self.spec = tk.Canvas(right, bg=PANEL, highlightthickness=1, highlightbackground=GRID, height=200)
        self.spec.pack(fill="x", pady=(0, 6))
        self.stat = tk.Canvas(right, bg=PANEL, highlightthickness=1, highlightbackground=GRID)
        self.stat.pack(fill="both", expand=True)

        self.status = tk.Label(self, text="idle — not connected", bg=PANEL, fg=TXT_DIM,
                               anchor="w", font=("Consolas", 9)); self.status.pack(fill="x", side="bottom")

    def _ports(self):
        if not list_ports: return ["COM27"]
        p = [x.device for x in list_ports.comports()]
        return p if p else ["COM27"]

    def _toggle(self):
        if self.reader:
            self.reader.stop.set(); self.reader = None
            self.conn_btn.config(text="CONNECT", bg=GREEN_D); self.status.config(text="disconnected"); return
        if not serial:
            self.status.config(text="pyserial missing"); return
        port = self.port_var.get().strip()
        self.reader = SerialReader(port, self.q); self.reader.start()
        self.conn_btn.config(text="DISCONNECT", bg="#7a1f1f")
        self.status.config(text=f"listening on {port} @ {BAUD} ...")

    def _toggle_rec(self):
        self.recording = not self.recording
        if self.recording:
            self.rec_rows = []; self.rec_btn.config(bg="#5a1114", fg="#fff", text="● REC ●")
        else:
            self.rec_btn.config(bg=PANEL, fg=RED, text="● REC")
            if self.rec_rows:
                fn = time.strftime("csi_rec_%Y%m%d_%H%M%S.csv")
                with open(fn, "w", newline="") as fh:
                    w = csvmod.writer(fh)
                    w.writerow(["t", "mac", "rssi", "motion"] + [f"sc{i}" for i in range(len(self.rec_rows[0]) - 4)])
                    w.writerows(self.rec_rows)
                self.status.config(text=f"saved {len(self.rec_rows)} frames -> {fn}")

    def _pick_mac(self, _):
        v = self.mac_var.get(); self.sel_mac = None if v == "(auto)" else v

    def _tick(self):
        now = time.time()
        while True:
            try: kind, payload = self.q.get_nowait()
            except queue.Empty: break
            if kind == "ERR":
                self.status.config(text=f"serial error: {payload}")
                self.conn_btn.config(text="CONNECT", bg=GREEN_D); self.reader = None; continue
            mac, rssi, amps = payload
            self.macs[mac] = self.macs.get(mac, 0) + 1
            self.sel_mac_auto = max(self.macs, key=self.macs.get)
            target = self.sel_mac or self.sel_mac_auto
            if mac != target: continue
            self.frame_count += 1; self.pkt_times.append(now)
            self.last_amps = amps; self.last_rssi = rssi
            self.peak_amp = max(self.peak_amp * 0.997, max(amps), 1.0)
            bl = self.baselines.setdefault(mac, collections.deque(maxlen=24))
            m = sum(amps) / len(amps)
            dev = (abs(m - sum(bl) / len(bl)) + self._spread(amps)) if bl else 0.0
            bl.append(m); self.waterfall.append(amps)
            sens = self.sens.get() / 100.0
            self.motion_level = min(1.0, dev / (self.peak_amp * (1.05 - sens) + 1e-6))
            self.motion_hist.append(self.motion_level)
            if self.recording:
                self.rec_rows.append([round(now, 3), mac, rssi, round(self.motion_level, 4)] +
                                     [round(a, 1) for a in amps])
        if self.macs:
            want = ["(auto)"] + list(self.macs.keys())
            if len(self.mac_cb["values"]) != len(want): self.mac_cb["values"] = want
        self._draw_waterfall(); self._draw_motion(); self._draw_presence()
        self._draw_spectrum(); self._draw_stats()
        self.after(33, self._tick)

    def _spread(self, amps):
        m = sum(amps) / len(amps)
        return (sum((a - m) ** 2 for a in amps) / len(amps)) ** 0.5

    def _draw_waterfall(self):
        c = self.wf; c.delete("all"); w = c.winfo_width(); h = c.winfo_height()
        if w < 10 or h < 10: return
        if self.waterfall:
            cols = len(self.waterfall); colw = max(1.0, w / 320.0)
            n_sc = len(self.waterfall[-1]); rowh = h / n_sc; peak = self.peak_amp
            x0 = w - cols * colw
            for ci, amps in enumerate(self.waterfall):
                xx = x0 + ci * colw
                for si, a in enumerate(amps):
                    c.create_rectangle(xx, si * rowh, xx + colw + 1, (si + 1) * rowh + 1,
                                       fill=amp_color(min(1.0, a / peak)), outline="")
        c.create_text(10, 12, text="WATERFALL   subcarrier × time   (newest →)", anchor="w",
                      fill=CYAN, font=("Consolas", 10, "bold"))
        if not self.waterfall:
            c.create_text(w/2, h/2, text="waiting for CSI frames…", fill=TXT_DIM, font=("Consolas", 12))

    def _draw_motion(self):
        c = self.mt; c.delete("all"); w = c.winfo_width(); h = c.winfo_height()
        if w < 10 or h < 10: return
        for gy in range(1, 4):
            yy = h * gy / 4; c.create_line(0, yy, w, yy, fill=GRID)
        # adjustable trigger threshold line
        thr = self.trig.get() / 100.0
        ty = h - (h - 20) * thr
        c.create_line(0, ty, w, ty, fill=AMBER, dash=(4, 3))
        c.create_text(w - 6, ty - 8, text=f"TRIG {self.trig.get()}%", anchor="e", fill=AMBER, font=("Consolas", 7, "bold"))
        hist = list(self.motion_hist)
        if len(hist) > 1:
            n = len(hist); step = w / 320.0; x0 = w - n * step
            pts = []
            for i, v in enumerate(hist):
                pts += [x0 + i * step, h - (h - 20) * v]
            c.create_line(pts, fill=GREEN, width=2)
            # fill under
            fill_pts = [x0, h] + pts + [x0 + (n - 1) * step, h]
            c.create_polygon(fill_pts, fill="#0f3a24", outline="")
        c.create_text(10, 12, text="MOTION TIMELINE   channel-change intensity", anchor="w",
                      fill=GREEN, font=("Consolas", 10, "bold"))

    def _draw_presence(self):
        c = self.presence; c.delete("all"); w = c.winfo_width(); h = c.winfo_height()
        if w < 10: return
        ml = self.motion_level
        thr = self.trig.get() / 100.0
        move_thr = thr * 0.5
        state = "PRESENCE" if ml > thr else ("MOVEMENT" if ml > move_thr else "STILL")
        scol = RED if ml > thr else (AMBER if ml > move_thr else GREEN)
        c.create_text(w/2, 34, text=state, fill=scol, font=("Consolas", 26, "bold"))
        # big arc meter
        cx, cy, R = w/2, 150, 80
        c.create_arc(cx-R, cy-R, cx+R, cy+R, start=180, extent=-180, style="arc",
                     outline=GRID, width=14)
        c.create_arc(cx-R, cy-R, cx+R, cy+R, start=180, extent=-180 * ml, style="arc",
                     outline=scol, width=14)
        c.create_text(cx, cy - 6, text=f"{ml*100:.0f}%", fill=scol, font=("Consolas", 22, "bold"))
        c.create_text(cx, cy + 18, text="motion intensity", fill=TXT_DIM, font=("Consolas", 9))
        c.create_text(10, 12, text="PRESENCE DETECTOR", anchor="w", fill=scol, font=("Consolas", 10, "bold"))

    def _draw_spectrum(self):
        c = self.spec; c.delete("all"); w = c.winfo_width(); h = c.winfo_height()
        if w < 10 or h < 10: return
        for gy in range(1, 4):
            yy = h * gy / 4; c.create_line(0, yy, w, yy, fill=GRID)
        amps = self.last_amps
        if amps and len(amps) > 1:
            n = len(amps); peak = self.peak_amp; pts = []
            for i, a in enumerate(amps):
                pts += [w * i / (n - 1), h - (h - 18) * min(1.0, a / peak)]
            c.create_line(pts, fill=GREEN, width=2, smooth=True)
        c.create_text(10, 12, text="LIVE SPECTRUM   amplitude / subcarrier", anchor="w",
                      fill=GREEN, font=("Consolas", 10, "bold"))

    def _draw_stats(self):
        c = self.stat; c.delete("all"); w = c.winfo_width(); h = c.winfo_height()
        if w < 10: return
        now = time.time()
        while self.pkt_times and now - self.pkt_times[0] > 2: self.pkt_times.popleft()
        rate = len(self.pkt_times) / 2.0
        rows = [("PKT / SEC", f"{rate:.0f}", GREEN),
                ("RSSI", f"{self.last_rssi} dBm", CYAN),
                ("FRAMES", f"{self.frame_count}", TXT),
                ("SOURCE", self.sel_mac or getattr(self, "sel_mac_auto", "--"), AMBER),
                ("DEVICES", str(len(self.macs)), TXT)]
        y = 22
        for label, val, col in rows:
            c.create_text(14, y, text=label, anchor="w", fill=TXT_DIM, font=("Consolas", 8))
            c.create_text(w - 14, y, text=val, anchor="e", fill=col, font=("Consolas", 13, "bold"))
            y += 30
        if self.recording:
            c.create_text(w/2, h - 14, text=f"● RECORDING  {len(self.rec_rows)} frames",
                          fill=RED, font=("Consolas", 9, "bold"))


if __name__ == "__main__":
    CSIMonitor().mainloop()
