#!/usr/bin/env python3
"""
log_viewer.py  v5.1
====================
Interactive ArduPilot telemetry viewer

Supported formats:  .tlog (MAVLink binary)  |  .csv (QGroundControl export)

Graphs (top to bottom):
  Odometry (m) | Voltage (V) | Current (A) | Temperature (°C)
  Pitch (°)    | Roll (°)    | Compass (°)

Controls:
  - Toolbar buttons  : Open File, Export CSV, Exit, Zoom, Pan, Home, Save
  - Vertical cursor  : always visible; drag with left-click, or use arrow keys
  - ← / →            : move cursor left / right (5 s steps)
  - Home / End       : jump to start / end of log
  - Ctrl+C           : close the application
  - X-axis           : elapsed time 0 → end, ticks every 5 min on every graph

Dependencies:
    pip install matplotlib pandas pymavlink
"""

import math
import os
import sys
import zoneinfo
from datetime import datetime

TZ_CHILE = zoneinfo.ZoneInfo("America/Santiago")

# ── Dependency check ──────────────────────────────────────────────────────────
def _need(pkg, import_as=None):
    try:
        __import__(import_as or pkg)
    except ImportError:
        print(f"\n[ERROR] Missing '{pkg}':\n        pip install {pkg}\n")
        sys.exit(1)

_need("pandas")
_need("matplotlib")
_need("pymavlink", "pymavlink")

import tkinter as tk
from tkinter import filedialog, messagebox

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("TkAgg")           # explicit backend so toolbar subclass works
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.gridspec as gridspec
import matplotlib.ticker as ticker
from matplotlib.backend_bases import NavigationToolbar2
from matplotlib.backends.backend_tkagg import (
    FigureCanvasTkAgg, NavigationToolbar2Tk,
)

matplotlib.rcParams.update({
    "figure.facecolor":  "#1a1a1a",
    "axes.facecolor":    "#111111",
    "axes.edgecolor":    "#333333",
    "axes.labelcolor":   "#aaaaaa",
    "xtick.color":       "#888888",
    "ytick.color":       "#888888",
    "text.color":        "#cccccc",
    "grid.color":        "#252525",
    "grid.linestyle":    "--",
    "grid.linewidth":    0.6,
    "lines.linewidth":   1.5,
    "font.size":         8,
})

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))

# ── Graph definitions (order = display order) ─────────────────────────────────
GRAPHS = [
    {"label": "Odometry",    "unit": "m",  "col": "odometry",    "color": "#aaaaaa"},
    {"label": "Voltage",     "unit": "V",  "col": "voltage",     "color": "#81c784"},
    {"label": "Current",     "unit": "A",  "col": "current",     "color": "#ffb74d"},
    {"label": "Temperature", "unit": "°C", "col": "temperature", "color": "#f48fb1"},
    {"label": "Pitch",       "unit": "°",  "col": "pitch",       "color": "#FF5100"},
    {"label": "Roll",        "unit": "°",  "col": "roll",        "color": "#4fc3f7"},
    {"label": "Compass",     "unit": "°",  "col": "heading",     "color": "#4dd0e1"},
]
N = len(GRAPHS)

ARROW_STEP_MIN = 5 / 60      # 5 seconds per arrow key press (in minutes)


# ── Data parsing ──────────────────────────────────────────────────────────────

def _build_df(records: dict) -> pd.DataFrame:
    """Merge per-column time-series lists into a single DataFrame."""
    frames = {}
    for col, rows in records.items():
        if not rows:
            continue
        s = pd.DataFrame(rows, columns=["ts", col])
        s["ts"] = pd.to_datetime(s["ts"], unit="s", utc=True).dt.tz_localize(None)
        s = s.sort_values("ts").groupby("ts")[col].mean()
        frames[col] = s

    if not frames:
        return pd.DataFrame()

    df = pd.concat(frames.values(), axis=1, keys=frames.keys())
    df = df.sort_index().interpolate(method="time", limit_direction="both")
    return df


_JOY_DEADZONE   = 80    # x/y/r neutral=0, range -1000..1000
_JOY_MIN_SECS   = 5.0   # minimum continuous movement duration to count as real
_PRE_MOVE_SECS  = 60    # seconds of data to keep before first joystick movement


def parse_tlog(path: str) -> pd.DataFrame:
    from pymavlink import mavutil
    mlog = mavutil.mavlink_connection(path, dialect="ardupilotmega")

    rec        = {g["col"]: [] for g in GRAPHS}
    joy_events = []   # (timestamp, moving: bool)

    while True:
        msg = mlog.recv_match(blocking=False)
        if msg is None:
            break
        t  = msg._timestamp
        mt = msg.get_type()

        if mt == "ATTITUDE":
            rec["pitch"].append((t, math.degrees(msg.pitch)))
            rec["roll"].append((t,  math.degrees(msg.roll)))

        elif mt == "VFR_HUD":
            rec["heading"].append((t, float(msg.heading)))

        elif mt == "SYS_STATUS":
            if msg.voltage_battery > 0:
                rec["voltage"].append((t, msg.voltage_battery / 1000.0))
            if msg.current_battery >= 0:
                rec["current"].append((t, msg.current_battery / 100.0))

        elif mt in ("SCALED_IMU", "SCALED_IMU2", "RAW_IMU"):
            temp = getattr(msg, "temperature", 0)
            if temp and temp != 0:
                rec["temperature"].append((t, temp / 100.0))

        elif mt == "MANUAL_CONTROL":
            moving = (abs(msg.x) > _JOY_DEADZONE or
                      abs(msg.y) > _JOY_DEADZONE or
                      abs(msg.r) > _JOY_DEADZONE)
            joy_events.append((t, moving))

    # Find first joystick segment >= 3 s (ignores accidental bumps)
    GAP = 2.0
    first_move_ts = None
    seg_start = seg_end = None
    for ts, moving in joy_events:
        if moving:
            if seg_start is None:
                seg_start = ts
            seg_end = ts
        else:
            if seg_start is not None and (ts - seg_end) > GAP:
                if (seg_end - seg_start) >= _JOY_MIN_SECS:
                    first_move_ts = seg_start
                    break
                seg_start = seg_end = None
    # Handle segment still open at end of log
    if first_move_ts is None and seg_start is not None and (seg_end - seg_start) >= _JOY_MIN_SECS:
        first_move_ts = seg_start

    df = _build_df(rec)

    if df.empty:
        return df

    if first_move_ts is None:
        reason = "sin MANUAL_CONTROL" if not joy_events else "todos los segmentos de movimiento < 5s"
        print(f"  Sin deteccion de movimiento ({reason})")
        print(f"  Mostrando log completo")
        return df

    # Trim: keep data from (first valid move - 1 min) onward
    cut = pd.Timestamp(first_move_ts - _PRE_MOVE_SECS, unit="s", tz="UTC").tz_localize(None)
    df  = df[df.index >= cut]
    import zoneinfo as _zi, datetime as _dt
    _tz  = _zi.ZoneInfo("America/Santiago")
    _fmt = lambda ts: _dt.datetime.fromtimestamp(ts, _dt.timezone.utc).astimezone(_tz).strftime("%a %d-%m-%Y %H:%M:%S")
    print(f"  Primer movimiento valido (>=3s) : {_fmt(first_move_ts)} (hora Chile)")
    print(f"  Grafica desde                   : {_fmt(first_move_ts - _PRE_MOVE_SECS)} (1 min antes)")
    return df


def parse_csv(path: str) -> pd.DataFrame:
    df_raw = pd.read_csv(
        path,
        parse_dates=["Timestamp"],
        na_values=["--.--", "--:--:--", ""],
        low_memory=False,
    )
    mapping = {
        "pitch":                "pitch",
        "roll":                 "roll",
        "battery0.voltage":     "voltage",
        "battery0.current":     "current",
        "imuTemp":              "temperature",
        "heading":              "heading",
    }
    keep = {src: dst for src, dst in mapping.items() if src in df_raw.columns}
    df = df_raw[["Timestamp"] + list(keep)].copy()
    df = df.rename(columns={"Timestamp": "ts", **keep})
    df["ts"] = pd.to_datetime(df["ts"])
    df = df.sort_values("ts").set_index("ts")
    for col in df.columns:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def load_file(path: str) -> pd.DataFrame:
    ext = os.path.splitext(path)[1].lower()
    if ext == ".tlog":
        return parse_tlog(path)
    if ext == ".csv":
        return parse_csv(path)
    raise ValueError(f"Unsupported format: {ext}")


def pick_file(initial_dir: str) -> str:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    path = filedialog.askopenfilename(
        title="Open telemetry file",
        initialdir=initial_dir,
        filetypes=[
            ("ArduPilot telemetry", "*.tlog *.csv"),
            ("MAVLink tlog", "*.tlog"),
            ("CSV QGroundControl", "*.csv"),
            ("All files", "*.*"),
        ],
    )
    root.destroy()
    return path


# ── Custom toolbar with Open / Export buttons ─────────────────────────────────

class AppToolbar(NavigationToolbar2Tk):
    """Extends the standard matplotlib toolbar with app-specific buttons."""

    toolitems = list(NavigationToolbar2Tk.toolitems)  # copy

    def __init__(self, canvas, window, viewer):
        self._viewer = viewer
        super().__init__(canvas, window)
        # Hide matplotlib's internal coordinate-message label (shows as black box)
        if hasattr(self, "_message_label"):
            self._message_label.pack_forget()
        # Extra buttons
        self._add_separator()
        self._btn_open   = self._add_app_btn("Open File",  self._on_open,          "#3a3a3a")
        self._btn_export = self._add_app_btn("Export CSV", self._on_export,        "#3a3a3a")
        self._btn_exit   = self._add_app_btn("Exit",       self._viewer._do_exit,  "#5a1a1a")
        # Persistent status label — never cleared by matplotlib
        self._status_lbl = tk.Label(
            self, text="",
            bg="#1a1a1a", fg="#cccccc",
            font=("Segoe UI", 12),
            anchor="e",
        )
        self._status_lbl.pack(side=tk.RIGHT, fill=tk.X, expand=True, padx=12)

    def set_message(self, msg):
        """Override: silence matplotlib's own coordinate messages."""
        pass   # we manage the status label ourselves

    def set_status(self, text: str):
        """Update the persistent dial status label."""
        self._status_lbl.config(text=text)

    def _add_separator(self):
        sep = tk.Frame(self, width=2, bg="#444444")
        sep.pack(side=tk.LEFT, padx=4, fill=tk.Y, pady=4)

    def _add_app_btn(self, label: str, command, bg: str) -> tk.Button:
        btn = tk.Button(
            self, text=label, command=command,
            bg=bg, fg="#cccccc",
            activebackground="#FF5100", activeforeground="white",
            relief=tk.FLAT, padx=8, pady=2,
            font=("Segoe UI", 8),
            cursor="hand2",
        )
        btn.pack(side=tk.LEFT, padx=2)
        return btn

    def _on_open(self):
        path = pick_file(os.path.dirname(self._viewer.path))
        if path:
            self._viewer.load_and_redraw(path)

    def _on_export(self):
        if self._viewer.df is None or self._viewer.df.empty:
            messagebox.showwarning("Export CSV", "No data loaded.")
            return
        base = os.path.splitext(self._viewer.path)[0] + "_export.csv"
        root = tk.Tk(); root.withdraw(); root.attributes("-topmost", True)
        dest = filedialog.asksaveasfilename(
            title="Save CSV",
            initialfile=os.path.basename(base),
            initialdir=os.path.dirname(base),
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
        )
        root.destroy()
        if not dest:
            return
        try:
            df = self._viewer.df.copy()
            df.index.name = "Timestamp"
            df.to_csv(dest)
            messagebox.showinfo("Export CSV", f"Saved:\n{dest}")
        except Exception as exc:
            messagebox.showerror("Export CSV", str(exc))


# ── Main viewer ───────────────────────────────────────────────────────────────

class LogViewer:
    def __init__(self, path: str):
        self.path    = path
        self.df      = None
        self._vlines = []      # vertical cursor lines (one per subplot)

        # Build Tkinter root + canvas
        self.root = tk.Tk()
        self.root.title("ArduPilot Log Viewer")
        self.root.configure(bg="#1a1a1a")
        self.root.geometry("1400x820")

        # Ctrl+C to exit
        self.root.bind("<Control-c>", lambda _e: self._do_exit())
        self.root.protocol("WM_DELETE_WINDOW", self._do_exit)

        self.fig = plt.figure(figsize=(14, 9))
        self.fig.patch.set_facecolor("#1a1a1a")

        self.canvas = FigureCanvasTkAgg(self.fig, master=self.root)
        self.canvas.get_tk_widget().pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        self.toolbar = AppToolbar(self.canvas, self.root, self)
        self.toolbar.update()
        self.toolbar.pack(side=tk.TOP, fill=tk.X)

        self._cursor_pos = 0.0   # current dial position in elapsed minutes
        self._dragging   = False

        self._build_axes()
        self.load_and_redraw(path)

        self.canvas.mpl_connect("motion_notify_event",  self._on_mouse_move)
        self.canvas.mpl_connect("button_press_event",   self._on_button_press)
        self.canvas.mpl_connect("button_release_event", self._on_button_release)
        self.canvas.mpl_connect("key_press_event",      self._on_key_press)

        # Keep keyboard focus on canvas; reclaim it if a toolbar button steals it
        widget = self.canvas.get_tk_widget()
        widget.focus_set()
        widget.bind("<Enter>", lambda _e: widget.focus_set())
        self.root.bind("<Left>",  lambda _e: self._move_cursor(self._cursor_pos - ARROW_STEP_MIN))
        self.root.bind("<Right>", lambda _e: self._move_cursor(self._cursor_pos + ARROW_STEP_MIN))
        self.root.bind("<Home>",  lambda _e: self._move_cursor(0.0))
        self.root.bind("<End>",   lambda _e: self._move_cursor(self._total_min()))
        self.root.mainloop()

    def _do_exit(self):
        plt.close("all")
        self.root.quit()
        self.root.destroy()

    # ── Axes layout ───────────────────────────────────────────────────────────
    def _build_axes(self):
        gs = gridspec.GridSpec(
            N, 1, figure=self.fig,
            left=0.07, right=0.98,
            top=0.96,  bottom=0.055,
            hspace=0.06,
        )
        self.axes = []
        for i in range(N):
            kwargs = {"sharex": self.axes[0]} if i > 0 else {}
            ax = self.fig.add_subplot(gs[i], **kwargs)
            ax.grid(True, alpha=0.35)
            ax.set_facecolor("#111111")
            for sp in ax.spines.values():
                sp.set_color("#333333")
            # Hide intermediate x-tick labels
            if i < N - 1:
                plt.setp(ax.get_xticklabels(), visible=False)
            self.axes.append(ax)

        # Vertical cursor lines — start visible at x=0
        for ax in self.axes:
            vl = ax.axvline(x=0, color="#ffffff", linewidth=0.9,
                            linestyle="--", alpha=0.65, visible=True)
            self._vlines.append(vl)

    # ── Load & draw ───────────────────────────────────────────────────────────
    def load_and_redraw(self, path: str):
        print(f"Loading: {os.path.basename(path)} ...")
        try:
            df = load_file(path)
        except Exception as exc:
            messagebox.showerror("Load error", str(exc))
            return

        self.path = path
        self.df   = df
        self.fig.suptitle(
            os.path.basename(path),
            color="#FF5100", fontsize=9, fontweight="bold", y=0.99,
        )

        # Elapsed-time origin (seconds)
        if not df.empty:
            self._t0 = df.index[0]
        else:
            self._t0 = pd.Timestamp("now")

        self._cursor_pos = 0.0   # reset dial to T=0 on every new file
        self._draw_all()
        self._move_cursor(0.0)   # populate status bar immediately at T=0

    # ── Draw all subplots ─────────────────────────────────────────────────────
    def _draw_all(self):
        for i, g in enumerate(GRAPHS):
            ax  = self.axes[i]
            col = g["col"]

            ax.cla()
            ax.set_facecolor("#111111")
            ax.grid(True, alpha=0.35)
            for sp in ax.spines.values():
                sp.set_color("#333333")
            ax.spines["left"].set_color(g["color"])

            # Re-create vertical cursor line after cla() — always visible
            vl = ax.axvline(x=self._cursor_pos, color="#ffffff", linewidth=0.9,
                            linestyle="--", alpha=0.65, visible=True)
            self._vlines[i] = vl

            # ── Plot data ──────────────────────────────────────────────────
            has_data = (
                self.df is not None
                and not self.df.empty
                and col in self.df.columns
                and self.df[col].notna().any()
            )
            if has_data:
                s = self.df[col].dropna()
                elapsed_min = (s.index - self._t0).total_seconds() / 60.0
                ax.plot(elapsed_min, s.values, color=g["color"],
                        linewidth=1.5, solid_capstyle="round")
                ax.set_ylim(
                    s.min() - 0.05 * max(abs(s.max() - s.min()), 1e-6),
                    s.max() + 0.05 * max(abs(s.max() - s.min()), 1e-6),
                )
                stats = f"min {s.min():.2f}  max {s.max():.2f}  mean {s.mean():.2f}"
            else:
                stats = "no data"

            ax.set_ylabel(g["unit"], fontsize=7.5, color=g["color"],
                          labelpad=16, rotation=0, va="center")
            ax.tick_params(axis="y", labelsize=6.5, colors=g["color"])
            ax.tick_params(axis="x", labelsize=7)

            # ── Graph title — top-right corner, non-interactive ────────────
            ax.text(
                0.995, 0.93,
                f"{g['label']} ({g['unit']})   {stats}",
                transform=ax.transAxes,
                ha="right", va="top",
                fontsize=7.5, color=g["color"], fontweight="bold",
            )

        # ── X-axis: elapsed minutes, 5-min grid ───────────────────────────
        self._setup_xaxis()
        self.canvas.draw_idle()

    def _setup_xaxis(self):
        """X-axis in elapsed minutes, major grid every 5 min + final value tick."""
        if self.df is None or self.df.empty:
            return

        total_min = (self.df.index[-1] - self._t0).total_seconds() / 60.0

        # Build tick positions: 0, 5, 10 … last-multiple-of-5, + final if different
        import numpy as _np
        last_5 = int(total_min // 5) * 5
        major_ticks = list(range(0, last_5 + 1, 5))
        if abs(total_min - last_5) > 0.1:          # final tick only if distinct
            major_ticks.append(round(total_min, 2))

        for i, ax in enumerate(self.axes):
            ax.set_xlim(0, max(total_min, 0.1))
            ax.xaxis.set_major_locator(ticker.FixedLocator(major_ticks))
            ax.xaxis.set_minor_locator(ticker.MultipleLocator(1))
            plt.setp(ax.get_xticklabels(), visible=True)
            if i == N - 1:
                ax.set_xlabel("Elapsed time (min)", fontsize=7.5,
                              color="#888888", labelpad=2)
            else:
                ax.set_xlabel("")

    # ── Vertical cursor ───────────────────────────────────────────────────────
    def _total_min(self) -> float:
        if self.df is None or self.df.empty:
            return 0.0
        return (self.df.index[-1] - self._t0).total_seconds() / 60.0

    def _move_cursor(self, x_min: float):
        """Clamp, update all vlines, refresh status."""
        x_min = max(0.0, min(x_min, self._total_min()))
        self._cursor_pos = x_min
        for vl in self._vlines:
            vl.set_xdata([x_min, x_min])
            vl.set_visible(True)
        self._update_status(x_min)
        self.canvas.draw_idle()

    def _on_mouse_move(self, event):
        if not self._dragging:
            return
        if event.inaxes is None or event.inaxes not in self.axes:
            return
        self._move_cursor(event.xdata)

    def _on_button_press(self, event):
        if event.button == 1 and event.inaxes in self.axes:
            self._dragging = True
            self._move_cursor(event.xdata)

    def _on_button_release(self, event):
        if event.button == 1:
            self._dragging = False

    def _on_key_press(self, event):
        if event.key == "left":
            self._move_cursor(self._cursor_pos - ARROW_STEP_MIN)
        elif event.key == "right":
            self._move_cursor(self._cursor_pos + ARROW_STEP_MIN)
        elif event.key == "home":
            self._move_cursor(0.0)
        elif event.key == "end":
            self._move_cursor(self._total_min())

    def _update_status(self, elapsed_min: float):
        if self.df is None or self.df.empty:
            return
        t_cursor = self._t0 + pd.Timedelta(minutes=elapsed_min)
        idx = self.df.index.get_indexer([t_cursor], method="nearest")[0]
        if idx < 0:
            return
        row = self.df.iloc[idx]

        # Chile local time (America/Santiago, auto DST)
        t_chile  = t_cursor.tz_localize("UTC").astimezone(TZ_CHILE)
        date_str = t_chile.strftime("%a  %d-%m-%Y  %H:%M:%S")

        # Elapsed as T: HH:MM:SS
        total_secs  = int(elapsed_min * 60)
        h           = total_secs // 3600
        m           = (total_secs % 3600) // 60
        s           = total_secs % 60
        elapsed_str = f"T: {h:02d}:{m:02d}:{s:02d}"

        parts = [f"{date_str}   {elapsed_str}"]
        for g in GRAPHS:
            col = g["col"]
            if col in row.index and pd.notna(row[col]):
                parts.append(f"{g['label']}: {row[col]:.2f} {g['unit']}")
        self.toolbar.set_status("   |   ".join(parts))


# ── Entry point ───────────────────────────────────────────────────────────────
def main():
    path = pick_file(SCRIPT_DIR)
    if not path:
        print("No file selected. Exiting.")
        sys.exit(0)
    LogViewer(path)


if __name__ == "__main__":
    main()
