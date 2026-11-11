import tkinter as tk
from tkinter import ttk, filedialog, messagebox, simpledialog
import serial
import serial.tools.list_ports
import threading
import time
import datetime
import csv
import os
import sys
import subprocess
import math
import bisect
import pathlib
import numpy as np
import matplotlib
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

matplotlib.use("TkAgg")

from ut325f_protocol import FRAME_HEADER, FRAME_SIZE, find_frame, parse_frame

# --- tuning constants -----------------------------------------------------
REPAINT_INTERVAL_MS = 250      # repaint rate, decoupled from frame arrival.
                               # A full redraw costs 100-170 ms on a slow
                               # machine, so 4 Hz leaves the UI thread headroom.
                               # Lower it for a smoother trace on fast hardware.
MAX_BUFFER_BYTES = 1 << 16     # hard cap on the serial re-sync buffer
NO_DATA_WARN_S = 5.0           # warn if a port opens but yields no valid frame
RECONNECT_INTERVAL_MS = 1500   # how often to look for a vanished port again
# Manual Y-axis bounds. The UT325F spans roughly -250 C to 1767 C depending
# on thermocouple type (J/K/T/E/R/S/N), so the old -100..200 window could not
# be set over most of the instrument's range.
Y_LIMIT_MIN = -250.0
Y_LIMIT_MAX = 1800.0
SYNC_EVERY_ROWS = 20           # fsync the autosave file every N rows
AUTOSAVE_DIR = pathlib.Path.home() / "ut325f_logs"   # default offered in the Start dialog

DEFAULT_LABELS = ["Channel 1", "Channel 2", "Channel 3", "Channel 4"]
COLOR_OPTIONS = [
    ('red', '#ff0000'),
    ('blue', '#0000ff'),
    ('green', '#008000'),
    ('orange', '#ffa500'),
    ('purple', '#800080'),   # Channel 4 default
    ('magenta', '#ff00ff'),
    ('cyan', '#00ffff'),
    ('black', '#000000'),
    ('brown', '#a52a2a'),
    ('navy', '#000080'),
    ('olive', '#808000'),
    ('teal', '#008080')
]

class ColorLineCombo(ttk.Combobox):
    def __init__(self, master, color_list, init_color, **kwargs):
        self._color_list = color_list
        self._color_names = [name for name, code in color_list]
        self._color_codes = [code for name, code in color_list]
        super().__init__(master, values=self._color_names, state="readonly", **kwargs)
        self.set(init_color)
        self.bind('<Expose>', self._customize_dropdown)
        self.bind('<<ComboboxSelected>>', self._customize_dropdown)
        self.bind('<Button-1>', self._customize_dropdown)

    def _customize_dropdown(self, event=None):
        try:
            menu = self.tk.call("ttk::combobox::PopdownWindow", self, "f.l")
            listbox = self.tk.globalgetvar(menu)
            for i, color in enumerate(self._color_codes):
                self.tk.call(listbox, "itemconfigure", i, "-foreground", color)
        except Exception:
            pass

    def get_color(self):
        idx = self.current()
        if idx < 0:
            idx = 0
        return self._color_codes[idx]

    def set_color(self, color_code):
        for idx, (_, code) in enumerate(self._color_list):
            if code == color_code or code.lower() == color_code.lower():
                self.current(idx)
                break

class TempLoggerApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Temperature Logger")
        self.root.geometry("1200x600")
        self.root.resizable(True, True)
        self.root.minsize(1200, 600)

        self.serial_port = None
        self.serial_thread = None
        self.running = False
        self.logging = False
        self.paused = False
        self.stop_event = threading.Event()
        self.is_connected = False
        self._closing = False
        self.frames_received = 0
        self.connect_time = None
        self._auto_reconnect = False     # armed while a link is expected back
        self._reconnect_port = None
        self._reconnect_after_id = None

        # repaint / redraw scheduling (see _repaint_tick)
        self._plot_dirty = False
        self._legend_dirty = True
        self._repaint_after_id = None
        self._ui_after_id = None
        self._last_window = 90
        self._last_y_limits = (0.0, 100.0)

        # streaming autosave
        self._log_file = None
        self._log_writer = None
        self._log_path = None
        self._rows_since_sync = 0
        self._autosave_warned = False
        self._last_log_dir = None
        self._pending_note = ""
        self.note_var = tk.StringVar()
        self.start_time = None

        self.timestamps = []
        self.data = [[] for _ in range(4)]
        self.events = []          # "" for a sample row, "PAUSE" for a break marker
        self.graph_enabled = [tk.BooleanVar(value=True) for _ in range(4)]
        self.marker_enabled = tk.BooleanVar(value=False)  # Markers disabled by default
        self.y_autoscale = tk.BooleanVar(value=True)
        self.y_min = tk.DoubleVar(value=0.0)
        self.y_max = tk.DoubleVar(value=100.0)
        self.time_window_min = tk.IntVar(value=1)
        self.time_window_sec = tk.IntVar(value=30)
        self.test_start_time = None
        self.channel_colors = [COLOR_OPTIONS[0][1], COLOR_OPTIONS[1][1], COLOR_OPTIONS[2][1], COLOR_OPTIONS[4][1]]
        self.channel_labels = [tk.StringVar(value=DEFAULT_LABELS[i]) for i in range(4)]

        self.build_gui()
        self.refresh_ports()
        self.update_ui()
        self._repaint_tick()

    def build_gui(self):
        left_frame = ttk.Frame(self.root)
        left_frame.pack(side=tk.LEFT, fill=tk.Y, padx=5, pady=5)
        
        port_row = ttk.Frame(left_frame)
        port_row.pack(pady=(2,0), fill=tk.X)
        ttk.Label(port_row, text="Serial Port:").pack(side=tk.LEFT)
        self.port_combo = ttk.Combobox(port_row, width=30, state="readonly")
        self.port_combo.pack(side=tk.LEFT, padx=2, fill=tk.X, expand=True)
        self.port_combo.bind("<Button-1>", lambda event: self.refresh_ports())

        # Connect/Disconnect button and status label (centered, directly below dropdown)
        connect_frame = ttk.Frame(left_frame)
        connect_frame.pack(pady=(4, 0), fill=tk.X)
        self.connect_btn = ttk.Button(connect_frame, text="Connect", width=15, command=self.toggle_connection)
        self.connect_btn.pack(pady=(0, 2), padx=2, ipadx=2, ipady=2)
        self.status_label = ttk.Label(connect_frame, text="DISCONNECTED", font=('Arial', 11, 'bold'))
        self.status_label.pack(pady=(0, 2))
        # Add separator after status label
        sep0 = ttk.Separator(left_frame, orient='horizontal')
        sep0.pack(fill='x', pady=(6, 8))

        self.temp_labels = []
        self.color_combos = []
        self.label_entries = []
        temp_frame = ttk.LabelFrame(left_frame)
        temp_frame.pack(fill=tk.X, pady=10)
        temp_title = ttk.Label(temp_frame, text="TEMPERATURES", font=("Arial", 12, "bold"), anchor="center", justify="center")
        temp_title.pack(fill=tk.X, pady=(0, 4))
        for i in range(4):
            row = ttk.Frame(temp_frame)
            row.pack(fill=tk.X, pady=2)
            label_entry = ttk.Entry(row, textvariable=self.channel_labels[i], width=12, font=("Arial", 12))
            label_entry.pack(side=tk.LEFT, padx=(2, 2))
            self.label_entries.append(label_entry)
            label_entry.bind("<FocusOut>", lambda e, idx=i: self.on_label_changed(idx))
            label_entry.bind("<Return>", lambda e, idx=i: self.on_label_changed(idx))
            lbl = ttk.Label(
                row,
                text="--.-- °C",
                font=("Arial", 14, "bold"),
                width=10,
                foreground=self.channel_colors[i],
                anchor="center",
                justify="center"
            )
            lbl.pack(side=tk.LEFT, padx=2, fill=tk.X, expand=False)
            self.temp_labels.append(lbl)
            chk = ttk.Checkbutton(row, text="Enable", variable=self.graph_enabled[i], command=self.update_plot)
            chk.pack(side=tk.LEFT, padx=2)
            color_combo = ColorLineCombo(row, COLOR_OPTIONS, COLOR_OPTIONS[i if i < 3 else 4][0], width=9)
            color_combo.pack(side=tk.LEFT, padx=8)
            color_combo.bind("<<ComboboxSelected>>", lambda e, idx=i: self.on_color_changed(idx))
            self.color_combos.append(color_combo)

        sep = ttk.Separator(left_frame, orient='horizontal')
        sep.pack(fill='x', pady=(8, 4))

        log_label = ttk.Label(left_frame, text="Data Logging/Graphing", font=('Arial', 12, 'bold'))
        log_label.pack(pady=(2, 2))

        # Data logging buttons: Start/Pause, Save, Delete
        log_frame = ttk.Frame(left_frame)
        log_frame.pack(pady=8)
        self.start_btn = ttk.Button(log_frame, text="Start", command=self.start_pause_logging)
        self.start_btn.pack(side=tk.LEFT, padx=4)
        self.open_folder_btn = ttk.Button(log_frame, text="Open Folder",
                                          command=self.open_log_folder, state=tk.DISABLED)
        self.open_folder_btn.pack(side=tk.LEFT, padx=4)
        self.new_file_btn = ttk.Button(log_frame, text="New File",
                                       command=self.new_log_file, state=tk.DISABLED)
        self.new_file_btn.pack(side=tk.LEFT, padx=4)

        # Free-text note attached to the Event column of the next sample
        note_frame = ttk.Frame(left_frame)
        note_frame.pack(fill=tk.X, pady=(2, 2))
        self.note_entry = ttk.Entry(note_frame, textvariable=self.note_var, font=("Arial", 11))
        self.note_entry.pack(side=tk.LEFT, padx=(2, 4), fill=tk.X, expand=True)
        self.note_entry.bind("<Return>", lambda e: self.add_note())
        self.note_btn = ttk.Button(note_frame, text="Note", width=8,
                                   command=self.add_note, state=tk.DISABLED)
        self.note_btn.pack(side=tk.LEFT, padx=(0, 2))

        self.autosave_label = ttk.Label(left_frame, text="", font=('Arial', 9),
                                        wraplength=300, justify="center", foreground="#555555")
        self.autosave_label.pack(fill=tk.X, pady=(0, 4))
        
        right_frame = ttk.Frame(self.root)
        right_frame.pack(side=tk.RIGHT, fill=tk.BOTH, expand=True, padx=5, pady=5)

        self.fig = Figure(figsize=(6, 4), dpi=100)
        self.ax = self.fig.add_subplot(111)
        self.ax.grid(True, which='both', linestyle='--', linewidth=0.7, alpha=0.7)
        self.lines = []
        for i in range(4):
            line, = self.ax.plot([], [], color=self.channel_colors[i], label=self.channel_labels[i].get(),
                                 linewidth=1, marker='o', markersize=4, markerfacecolor=self.channel_colors[i])
            self.lines.append(line)
        self.ax.set_xlabel("Time (s)")
        self.ax.set_ylabel("Temperature (°C)")
        self.ax.set_xlim(0, 90)
        self.legend = self.ax.legend(loc="upper left", fontsize=10)

        self.canvas = FigureCanvasTkAgg(self.fig, right_frame)
        self.canvas.get_tk_widget().pack(fill=tk.BOTH, expand=True)

        controls_row1 = ttk.Frame(right_frame)
        controls_row1.pack(fill=tk.X, pady=6)
        ttk.Checkbutton(
            controls_row1, text="Show Markers",
            variable=self.marker_enabled,
            command=self.update_plot
        ).pack(side=tk.TOP, anchor="center")

        controls_row2 = ttk.Frame(right_frame)
        controls_row2.pack(fill=tk.X, pady=6)
        controls_row2_inner = ttk.Frame(controls_row2)
        controls_row2_inner.pack(anchor="center")
        ttk.Label(controls_row2_inner, text="Time window:").pack(side=tk.LEFT, padx=(10, 2))
        ttk.Spinbox(controls_row2_inner, from_=0, to=60, textvariable=self.time_window_min, width=3, command=self.update_plot).pack(side=tk.LEFT)
        ttk.Label(controls_row2_inner, text="min").pack(side=tk.LEFT)
        ttk.Spinbox(controls_row2_inner, from_=1, to=3600, textvariable=self.time_window_sec, width=4, command=self.update_plot).pack(side=tk.LEFT)
        ttk.Label(controls_row2_inner, text="sec").pack(side=tk.LEFT)

        controls_row3 = ttk.Frame(right_frame)
        controls_row3.pack(fill=tk.X, pady=6)
        controls_row3_inner = ttk.Frame(controls_row3)
        controls_row3_inner.pack(anchor="center")
        ttk.Checkbutton(
            controls_row3_inner, text="Auto Y",
            variable=self.y_autoscale,
            command=self.update_plot
        ).pack(side=tk.LEFT, padx=(10, 8))
        ttk.Label(controls_row3_inner, text="Y min:").pack(side=tk.LEFT)
        y_min_spin = ttk.Spinbox(controls_row3_inner, from_=Y_LIMIT_MIN, to=Y_LIMIT_MAX,
                                 textvariable=self.y_min, width=7, command=self.update_plot)
        y_min_spin.pack(side=tk.LEFT)
        ttk.Label(controls_row3_inner, text="Y max:").pack(side=tk.LEFT)
        y_max_spin = ttk.Spinbox(controls_row3_inner, from_=Y_LIMIT_MIN, to=Y_LIMIT_MAX,
                                 textvariable=self.y_max, width=7, command=self.update_plot)
        y_max_spin.pack(side=tk.LEFT)

        self.y_min_spin = y_min_spin
        self.y_max_spin = y_max_spin

        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def refresh_ports(self):
        ports = [p.device for p in serial.tools.list_ports.comports()]
        self.port_combo['values'] = [port for port in reversed(ports)]
        if ports:
            self.port_combo.current(0)

    def toggle_connection(self):
        if self.is_connected or self._auto_reconnect:
            self.disconnect()
        else:
            self.connect()

    def connect(self):
        port = self.port_combo.get()
        if not port:
            messagebox.showerror("Error", "No port selected.")
            return
        try:
            self._open_port(port)
        except Exception as e:
            messagebox.showerror("Connection Error", str(e))

    def _open_port(self, port):
        """Open the port and start the reader. Raises on failure."""
        self.serial_port = serial.Serial(port, 115200, timeout=0.2)
        self._reconnect_port = port
        self._auto_reconnect = True      # unplug now means "come back", not "give up"
        self.connect_btn.config(text="Disconnect")
        self.is_connected = True
        self.frames_received = 0
        self.connect_time = time.monotonic()
        self.update_status_label()
        self.running = True
        self.stop_event.clear()
        self.serial_thread = threading.Thread(target=self.serial_reader, daemon=True)
        self.serial_thread.start()

    def _teardown_port(self):
        """Release the port without deciding anything about reconnecting."""
        self.running = False
        self.stop_event.set()
        ser = self.serial_port
        self.serial_port = None          # hand the port off before touching it,
        if ser is not None:              # so the reader's guard sees None
            try:
                ser.cancel_read()
            except Exception:
                pass
            try:
                ser.close()
            except Exception:
                pass
        self.is_connected = False
        for lbl in self.temp_labels:
            lbl.config(text="--.-- °C")  # Reset to dashes

    def disconnect(self):
        """The user asked to stop. Do not come back on our own."""
        if self._closing:
            return
        self._auto_reconnect = False
        self._cancel_reconnect()
        self._teardown_port()
        self.connect_btn.config(text="Connect")
        self.update_status_label()

    def _link_lost(self):
        """The reader stopped by itself: the cable or the meter went away.
        Must only run on the Tk main thread."""
        if self._closing:
            return
        self._teardown_port()
        if self.logging and not self.paused:
            self._mark_break("LINK LOST")
        if self._auto_reconnect and self._reconnect_port:
            self._schedule_reconnect()
        else:
            self.connect_btn.config(text="Connect")
        self.update_status_label()

    def _schedule_reconnect(self):
        self._cancel_reconnect()
        self._reconnect_after_id = self.root.after(RECONNECT_INTERVAL_MS, self._try_reconnect)

    def _cancel_reconnect(self):
        if self._reconnect_after_id is not None:
            try:
                self.root.after_cancel(self._reconnect_after_id)
            except Exception:
                pass
            self._reconnect_after_id = None

    def _try_reconnect(self):
        """Re-open the same port once it reappears. Logging, the open CSV file
        and the trace are all left alone, so a run survives a replug."""
        self._reconnect_after_id = None
        if self._closing or not self._auto_reconnect:
            return
        try:
            present = [pt.device for pt in serial.tools.list_ports.comports()]
        except Exception:
            present = []
        if self._reconnect_port in present:
            try:
                self._open_port(self._reconnect_port)   # re-arms and restarts
                self.refresh_ports()
                self.port_combo.set(self._reconnect_port)
                return
            except Exception:
                # enumerated but not yet openable: the OS is still settling
                self.serial_port = None
                self.is_connected = False
        self._schedule_reconnect()
        self.update_status_label()

    def update_status_label(self):
        if self.is_connected:
            if self.frames_received:
                self.status_label.config(text="CONNECTED", foreground="green")
            elif self.connect_time is not None and \
                    (time.monotonic() - self.connect_time) > NO_DATA_WARN_S:
                self.status_label.config(text="NO DATA", foreground="#cc6600")
            else:
                self.status_label.config(text="CONNECTING...", foreground="#cc6600")
        elif self._auto_reconnect:
            self.status_label.config(text="RECONNECTING...", foreground="#cc6600")
        else:
            self.status_label.config(text="DISCONNECTED", foreground="red")

    def serial_reader(self):
        buffer = b''
        keep = len(FRAME_HEADER) - 1     # longest header prefix worth retaining
        while self.running:
            ser = self.serial_port       # single read of the attribute: no
            if ser is None or not ser.is_open:   # check-then-use race
                break
            try:
                chunk = ser.read(128)
            except Exception as e:
                if self.running:
                    print("Serial error:", e)
                break
            if chunk:
                buffer += chunk
            while True:
                # find_frame verifies the checksum and resyncs past a false
                # header lock, so only sound frames reach the GUI.
                frame, buffer = find_frame(buffer)
                if frame is None:
                    break
                # Always update the temperature labels, even if not logging
                temps = parse_frame(frame)
                self.root.after(0, self.handle_new_data, temps)
            # find_frame already drops anything that cannot become a frame;
            # this is only a backstop against pathological input.
            if len(buffer) > MAX_BUFFER_BYTES:
                buffer = buffer[-keep:]
        # Tkinter is not thread-safe: hand teardown back to the main thread.
        # after() is itself a Tk call, so skip it once the root is going away.
        if not self._closing:
            try:
                self.root.after(0, self._link_lost)
            except RuntimeError:
                pass

    def handle_new_data(self, temps):
        if self._closing:
            return
        # time.monotonic() is the stdlib's clock for measuring intervals: it
        # cannot be stepped by NTP, DST or a manual clock change, the way
        # time.time() can. Its 15.6 ms granularity on Windows is far finer
        # than the ~270 ms spacing between frames.
        now = time.monotonic()
        self.frames_received += 1
        # Update the temperature labels regardless of logging state
        for i, val in enumerate(temps):
            color = self.color_combos[i].get_color()
            self.temp_labels[i]['foreground'] = color
            if val is not None:
                self.temp_labels[i]['text'] = f"{val:.2f} °C"
            else:
                self.temp_labels[i]['text'] = "-"

        # Only log data for graph when logging and not paused
        if self.logging and not self.paused and self.start_time is not None:
            # t = 0 is the moment Start was pressed, which is also what
            # "Test Start Time (PC)" on line 1 of the CSV records.
            t = now - self.start_time
            note, self._pending_note = self._pending_note, ""
            if note:
                self._refresh_log_label()   # clear "Note queued ..."
            self.timestamps.append(t)
            self.events.append(note)
            for ch in range(4):
                self.data[ch].append(temps[ch])
            self._write_row(t, temps, event=note)
        self.update_plot()

    def update_plot(self):
        """Request a repaint. The actual draw happens in _repaint_tick at a
        fixed 10 Hz, so frame arrival no longer drives matplotlib."""
        self._plot_dirty = True

    def _repaint_tick(self):
        if self._closing:
            return
        if self._plot_dirty:
            try:
                self._redraw()
            except Exception as e:
                print("Plot error:", e)
        self._repaint_after_id = self.root.after(REPAINT_INTERVAL_MS, self._repaint_tick)

    def _window_seconds(self):
        try:
            w = self.time_window_min.get() * 60 + self.time_window_sec.get()
        except Exception:
            w = self._last_window        # spinbox holds non-numeric text
        if w <= 0:
            w = 1                        # a zero window gives degenerate x-limits
        self._last_window = w
        return w

    def _manual_y_limits(self):
        try:
            lo, hi = self.y_min.get(), self.y_max.get()
        except Exception:
            lo, hi = self._last_y_limits
        if hi <= lo:
            hi = lo + 1.0
        self._last_y_limits = (lo, hi)
        return lo, hi

    def _redraw(self):
        self._plot_dirty = False
        window = self._window_seconds()

        for i in range(4):
            color = self.color_combos[i].get_color()
            if color != self.channel_colors[i]:
                self.channel_colors[i] = color
                self._legend_dirty = True
            self.temp_labels[i]['foreground'] = color

        # timestamps are strictly increasing, so the visible window is a slice
        if self.timestamps:
            t_last = self.timestamps[-1]
            i0 = bisect.bisect_left(self.timestamps, max(0.0, t_last - window))
        else:
            t_last = 0.0
            i0 = 0
        xs = self.timestamps[i0:]

        marker = 'o' if self.marker_enabled.get() else 'None'
        y_lo = y_hi = None
        for ch in range(4):
            line = self.lines[ch]
            enabled = self.graph_enabled[ch].get()
            label = self.channel_labels[ch].get()
            if line.get_label() != label:
                line.set_label(label)
                self._legend_dirty = True
            if line.get_visible() != enabled:
                line.set_visible(enabled)
                self._legend_dirty = True
            line.set_color(self.channel_colors[ch])
            line.set_markerfacecolor(self.channel_colors[ch])
            line.set_marker(marker)
            if enabled and xs:
                ys = np.array([np.nan if v is None else v
                               for v in self.data[ch][i0:]], dtype=float)
                line.set_data(xs, ys)    # no ax.clear(): reuse the Line2D
                finite = ys[np.isfinite(ys)]
                if finite.size:
                    lo, hi = float(finite.min()), float(finite.max())
                    y_lo = lo if y_lo is None else min(y_lo, lo)
                    y_hi = hi if y_hi is None else max(y_hi, hi)
            else:
                line.set_data([], [])

        self.ax.set_xlim(max(0.0, t_last - window), max(t_last, window)
                         if self.timestamps else 90)
        if self.y_autoscale.get():
            if y_lo is not None:
                pad = max(0.05 * (y_hi - y_lo), 0.1)
                self.ax.set_ylim(y_lo - pad, y_hi + pad)
        else:
            self.ax.set_ylim(*self._manual_y_limits())

        if self._legend_dirty:
            visible = [l for l in self.lines if l.get_visible()]
            existing = self.ax.get_legend()
            if visible:
                self.ax.legend(visible, [l.get_label() for l in visible],
                               loc="upper left", fontsize=10)
            elif existing is not None:
                existing.remove()
            self._legend_dirty = False

        self.canvas.draw_idle()

    def start_pause_logging(self):
        # Start or Pause logic with one button
        if not self.logging:
            # Start pressed. Ask where to record BEFORE any state changes, so
            # that cancelling leaves the previous run untouched and t = 0 is
            # the moment recording begins, not the moment the dialog opened.
            path = self._ask_log_path()
            if path is None:
                return                      # cancelled: nothing was started
            self.timestamps = []
            self.data = [[] for _ in range(4)]
            self.events = []
            # One start instant for both the wall-clock header and t = 0.
            self.test_start_time = datetime.datetime.now()
            if not self._open_log_file(path):
                return                      # could not open: nothing started
            self.start_time = time.monotonic()
            self.logging = True
            self.paused = False
            self.start_btn.config(text="Pause")
            self.ax.set_xlim(0, self._window_seconds())
            self.update_plot()
        else:
            # Pause pressed
            self.paused = not self.paused
            if self.paused:
                self.start_btn.config(text="Resume")
            else:
                self.start_btn.config(text="Pause")
            # Insert NaN to break the plot lines on pause
            if self.paused:
                self._mark_break("PAUSE")
        self.update_plot()

    # ---------------- streaming autosave (#2) and CSV encoding (#7) -------
    @staticmethod
    def _format_value(v):
        """Blank = probe not connected. NaN/Inf = a non-finite reading, which is
        NOT the same thing and must not be silently blanked."""
        if v is None:
            return ""
        if isinstance(v, float):
            if math.isnan(v):
                return "NaN"
            if math.isinf(v):
                return "Inf" if v > 0 else "-Inf"
        return f"{v:.2f}"

    def _header_rows(self):
        start_str = self.test_start_time.strftime("%Y-%m-%d %H:%M:%S") if self.test_start_time else ""
        return ([f"Test Start Time (PC): {start_str}"],
                ["Time (s)"] + [self.channel_labels[ch].get() for ch in range(4)] + ["Event"])

    def _mark_break(self, label):
        """Insert a gap in the trace and name it in the Event column, so a
        pause or a dropped link is never drawn as a straight line."""
        if not self.timestamps:
            return
        t = self.timestamps[-1] + 1e-6
        self.timestamps.append(t)
        self.events.append(label)
        for ch in range(4):
            self.data[ch].append(np.nan)
        self._write_row(t, [None] * 4, event=label)
        self.update_plot()

    def _refresh_log_label(self):
        """Restore the label to its resting text: the file being recorded to."""
        self.autosave_label.config(
            text=f"Recording to {self._log_path}" if self._log_path else "")

    def _ask_log_path(self):
        """Ask where this run should be recorded. Returns a Path, or None if
        the user cancelled."""
        suggested = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S") + ".csv"
        initial_dir = self._last_log_dir
        if not initial_dir:
            try:
                AUTOSAVE_DIR.mkdir(parents=True, exist_ok=True)
                initial_dir = str(AUTOSAVE_DIR)
            except Exception:
                initial_dir = str(pathlib.Path.home())
        path = filedialog.asksaveasfilename(
            title="Record this run to...",
            defaultextension=".csv",
            initialdir=initial_dir,
            initialfile=suggested,
            filetypes=[("CSV files", "*.csv")]
        )
        return pathlib.Path(path) if path else None

    def _open_log_file(self, path):
        """Open the run's CSV before the first sample, so an interrupted run is
        still on disk. Save then exports a copy of this file.
        Returns True if recording can begin."""
        self._close_log_file()
        self._rows_since_sync = 0
        self._autosave_warned = False
        try:
            self._log_path = pathlib.Path(path)
            self._log_file = open(self._log_path, "w", newline="", encoding="utf-8")
            self._log_writer = csv.writer(self._log_file)
            for row in self._header_rows():
                self._log_writer.writerow(row)
            self._log_file.flush()
            self._last_log_dir = str(self._log_path.parent)
            self._refresh_log_label()
            return True
        except Exception as e:
            self._log_file = None
            self._log_writer = None
            self._log_path = None
            self.autosave_label.config(text="")
            messagebox.showerror("Cannot record",
                                 f"Could not open this file for writing:\n{path}\n\n{e}\n\n"
                                 "Nothing has been started; pick another location.")
            return False

    def _write_row(self, t, values, event=""):
        if self._log_writer is None:
            return
        try:
            self._log_writer.writerow([f"{t:.6f}"] + [self._format_value(v) for v in values] + [event])
            self._log_file.flush()
            self._rows_since_sync += 1
            if self._rows_since_sync >= SYNC_EVERY_ROWS:
                os.fsync(self._log_file.fileno())
                self._rows_since_sync = 0
        except Exception as e:
            self._log_writer = None
            if not self._autosave_warned:
                self._autosave_warned = True
                self.autosave_label.config(text="Autosave stopped - data held in memory only")
                messagebox.showwarning("Autosave stopped", f"Writing to the autosave file failed:\n{e}")

    def _close_log_file(self):
        if self._log_file is not None:
            try:
                self._log_file.flush()
                os.fsync(self._log_file.fileno())
            except Exception:
                pass
            try:
                self._log_file.close()
            except Exception:
                pass
        self._log_file = None
        self._log_writer = None

    def open_log_folder(self):
        """Reveal the file this run is being recorded to."""
        if self._log_path is None:
            return
        target = self._log_path if self._log_path.exists() else self._log_path.parent
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(target)])
            elif sys.platform == "win32":
                if target.is_file():
                    subprocess.Popen(["explorer", "/select,", str(target)])
                else:
                    os.startfile(str(target))
            else:
                subprocess.Popen(["xdg-open", str(target.parent if target.is_file() else target)])
        except Exception as e:
            messagebox.showerror("Could not open folder", str(e))

    def new_log_file(self):
        """Close the current recording and begin a fresh one, without
        disturbing the serial connection."""
        path = self._ask_log_path()
        if path is None:
            return                          # cancelled: current run continues
        previous = self._log_path
        self.test_start_time = datetime.datetime.now()
        if not self._open_log_file(path):
            self._log_path = previous       # keep pointing at the run still open
            self._refresh_log_label()
            return                          # current run continues untouched
        self.timestamps = []
        self.data = [[] for _ in range(4)]
        self.events = []
        self._pending_note = ""
        self.start_time = time.monotonic()
        self.logging = True
        self.paused = False
        self.start_btn.config(text="Pause")
        self.ax.set_xlim(0, self._window_seconds())
        self.update_plot()

    def add_note(self):
        """Stage a note; it is written into the Event column of the next
        recorded sample."""
        text = self.note_var.get().strip()
        if not text:
            return
        self._pending_note = text
        self.note_var.set("")
        self.autosave_label.config(text=f'Note queued for the next sample: "{text}"')

    def on_color_changed(self, idx):
        color = self.color_combos[idx].get_color()
        self.temp_labels[idx]['foreground'] = color
        self.update_plot()

    def on_label_changed(self, idx):
        self.update_plot()

    def update_ui(self):
        if self._closing:
            return
        state = tk.NORMAL if not self.y_autoscale.get() else tk.DISABLED
        try:
            self.y_min_spin.config(state=state)
            self.y_max_spin.config(state=state)
        except Exception:
            pass

        if not self.logging:
            self.start_btn.config(state=tk.NORMAL, text="Start")
        else:
            self.start_btn.config(state=tk.NORMAL,
                                  text="Resume" if self.paused else "Pause")
        # Open Folder needs a file; New File and Note need an active run.
        self.open_folder_btn.config(
            state=tk.NORMAL if self._log_path is not None else tk.DISABLED)
        run_state = tk.NORMAL if self.logging else tk.DISABLED
        self.new_file_btn.config(state=run_state)
        self.note_btn.config(state=run_state)

        # Update status label color
        self.update_status_label()

        self._ui_after_id = self.root.after(500, self.update_ui)

    def on_close(self):
        self._closing = True             # queued after() callbacks become no-ops
        self.running = False
        self.stop_event.set()
        for after_id in (self._ui_after_id, self._repaint_after_id):
            if after_id is not None:
                try:
                    self.root.after_cancel(after_id)
                except Exception:
                    pass
        self._ui_after_id = None
        self._repaint_after_id = None
        self._auto_reconnect = False
        self._cancel_reconnect()
        ser = self.serial_port
        if ser is not None:
            try:
                ser.cancel_read()        # release a blocking read(128)
            except Exception:
                pass
        if self.serial_thread and self.serial_thread.is_alive():
            try:
                self.serial_thread.join(timeout=1.0)
            except Exception:
                pass
        self._close_log_file()
        if self.serial_port is not None:
            try:
                self.serial_port.close()
            except Exception:
                pass
            self.serial_port = None
        self.root.destroy()

if __name__ == "__main__":
    root = tk.Tk()
    app = TempLoggerApp(root)
    root.mainloop()
    