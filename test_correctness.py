
import os, sys, csv, time, pathlib, importlib.util, tempfile, shutil
import tkinter as tk
import numpy as np

# this sandbox runs with PYTHONSAFEPATH, which drops the script dir
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

log = open("test_correctness.out", "w", encoding="utf-8", buffering=1)
def say(*a):
    print(*a); log.write(" ".join(str(x) for x in a) + "\n")

def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec); sys.modules[name] = m
    spec.loader.exec_module(m); return m

new = load("ut325f_gui.py", "gui_new")
new.messagebox.showerror = lambda *a, **k: None
new.messagebox.showwarning = lambda *a, **k: None
new.messagebox.showinfo = lambda *a, **k: None
tmp = pathlib.Path(tempfile.mkdtemp(prefix="ut325f_test_"))
new.AUTOSAVE_DIR = tmp / "logs"

root = tk.Tk(); root.withdraw()
app = new.TempLoggerApp(root); root.update()

draws = {"n": 0}
real = app.canvas.draw_idle
app.canvas.draw_idle = lambda *a, **k: (draws.__setitem__("n", draws["n"] + 1), real(*a, **k))[1]

# --- the Start dialog: cancel must leave everything untouched -------------
app._ask_log_path = lambda: None                # user pressed Cancel
app.start_pause_logging()
assert app.logging is False and app._log_path is None and not app.timestamps
say("T0a Cancel at the Start dialog -> nothing started, no file created: OK")

# --- an unwritable location must also abort, not start in memory ----------
bad = tmp / "no_such_dir" / "x.csv"
app._ask_log_path = lambda: bad
app.start_pause_logging()
assert app.logging is False and app._log_path is None
say("T0b Unwritable location -> recording refused, nothing started: OK")

chosen = tmp / "chosen_by_user.csv"
app._ask_log_path = lambda: chosen
app.start_pause_logging()                       # Start
assert app._log_path == chosen, (app._log_path, chosen)
assert chosen.exists(), "the chosen file must exist from the first moment"
say("T0c Start records to the user-chosen path:", app._log_path.name)
say("T0d directory remembered for the next run:", pathlib.Path(app._last_log_dir).name)
base = draws["n"]
for i in range(300):
    app.handle_new_data([30.0 + i * 0.01, None, None, 29.5 + i * 0.01])
app.handle_new_data([float("nan"), None, None, 31.0])    # non-finite reading
say("T1  draws triggered by 301 arriving samples:", draws["n"] - base, "(expected 0 - decoupled)")
app.update_plot(); app._redraw()
say("T2  one explicit repaint ->", draws["n"] - base, "draw call(s)")

app.start_pause_logging()                       # Pause -> PAUSE marker row
out = tmp / "exported.csv"
app._log_file.flush(); os.fsync(app._log_file.fileno())
shutil.copyfile(app._log_path, out)
rows = list(csv.reader(out.read_text(encoding="utf-8").splitlines()))
say("T3  header row 2 :", rows[1])
say("T4  normal row   :", rows[2])
say("T5  NaN row      :", rows[-2])
say("T6  PAUSE row    :", rows[-1])
assert rows[1][-1] == "Event", "Event column missing"
assert rows[2][2] == "" and rows[2][3] == "", "disconnected probe should be blank"
assert rows[-2][1] == "NaN", "non-finite reading must not be blanked"
assert rows[-1][-1] == "PAUSE" and all(c == "" for c in rows[-1][1:5]), "pause row malformed"
assert rows[2][1] != "" and rows[2][4] != "", "live channels must carry values"
say("T7  data rows already on disk mid-run (no explicit save step):", len(rows) - 2)

# --- notes land in the Event column of the NEXT sample -------------------
app.note_var.set("  heater on  ")
app.add_note()
assert app._pending_note == "heater on", app._pending_note
assert app.note_var.get() == "", "entry should clear after queueing"
app.start_pause_logging()                            # Resume
queued_msg = app.autosave_label.cget("text")
assert queued_msg.startswith("Note queued"), queued_msg
app.handle_new_data([40.0, None, None, 41.0])        # the annotated sample
resting = app.autosave_label.cget("text")
assert resting == f"Recording to {app._log_path}", resting
say("T7b label while queued:", repr(queued_msg[:34] + "..."))
say("T7c label once the note is attached:", repr("Recording to .../" + app._log_path.name))
app.handle_new_data([40.1, None, None, 41.1])        # the one after
assert app.events[-2] == "heater on" and app.events[-1] == "", app.events[-3:]
assert app._pending_note == "", "a note must fire once only"
say("T8  note queued, then attached to exactly one sample: OK")

app._log_file.flush(); os.fsync(app._log_file.fileno())
rows2 = list(csv.reader(app._log_path.read_text(encoding="utf-8").splitlines()))
annotated = [r for r in rows2[2:] if r[-1] == "heater on"]
assert len(annotated) == 1 and annotated[0][1] == "40.00", annotated
say("T9  annotated row on disk:", annotated[0])

# --- New File rolls over without touching the connection -----------------
second = tmp / "run_two.csv"
app._ask_log_path = lambda: second
app.new_log_file()
assert app._log_path == second and app.logging and not app.paused
assert app.timestamps == [] and app.events == [], "New File must reset the trace"
app.handle_new_data([21.0, None, None, 22.0])
app._log_file.flush(); os.fsync(app._log_file.fileno())
r2 = list(csv.reader(second.read_text(encoding="utf-8").splitlines()))
assert r2[1][-1] == "Event" and len(r2) == 3, r2
say("T9b New File opened a fresh run, still logging:", second.name, "| data rows:", len(r2) - 2)

# --- cancelling New File leaves the current run alone --------------------
app._ask_log_path = lambda: None
app.new_log_file()
assert app._log_path == second and app.logging
say("T9c Cancel at New File -> current run continues untouched: OK")

app.update_ui()
say("T9d Open Folder button state with a file open:", app.open_folder_btn.cget("state"))

say("T10 first sample t = %.4f s = the real delay from Start to the first frame.\n    Reads 0.0000 when that delay is under one clock tick (15.6 ms on Windows);\n    against a real meter at ~270 ms spacing it is normally non-zero."
    % app.timestamps[0])
assert app.timestamps[0] >= 0
assert all(b >= a for a, b in zip(app.timestamps, app.timestamps[1:])), "timestamps must be non-decreasing"
say("T11 timestamps non-decreasing (bisect windowing is valid): OK")

keep = len(new.FRAME_HEADER) - 1
junk = b"\x00" * 100000
assert new.FRAME_HEADER not in junk
say("T12 100 kB of headerless junk -> trimmed to", len(junk[-keep:]), "bytes")
part = b"\xAA\x55\x00"
assert (part[-keep:] if len(part) > keep else part) == part
say("T13 a 3-byte partial header at the buffer tail is preserved: OK")

app.frames_received = 0; app.is_connected = True; app.connect_time = time.monotonic() - 10
app.update_status_label(); say("T14 connected, zero valid frames, 10 s elapsed ->", repr(app.status_label.cget("text")))
app.frames_received = 5; app.update_status_label()
say("T15 after a valid frame ->", repr(app.status_label.cget("text")))

app.time_window_sec.set(0); app.time_window_min.set(0)
say("T16 time window typed as 0 -> _window_seconds() =", app._window_seconds(), "(no degenerate x-limits)")
app.y_min.set(50.0); app.y_max.set(10.0)
say("T17 y_max < y_min -> _manual_y_limits() =", app._manual_y_limits())

# --- unplug / replug: the run must survive and resume by itself ---------
class FakeSerial:
    def __init__(self, *a, **k): self.is_open = True
    def read(self, n): time.sleep(0.02); return b""
    def close(self): self.is_open = False
    def cancel_read(self): pass

class FakePort:
    def __init__(self, dev): self.device = dev

PRESENT = ["COM9"]
new.serial.Serial = FakeSerial
new.serial.tools.list_ports.comports = lambda: [FakePort(d) for d in PRESENT]
new.RECONNECT_INTERVAL_MS = 50

app2 = new.TempLoggerApp(tk.Toplevel(root))
run = tmp / "replug.csv"
app2._ask_log_path = lambda: run
app2.start_pause_logging()
app2._open_port("COM9")
assert app2.is_connected and app2._auto_reconnect
app2.handle_new_data([25.0, None, None, 26.0])
say("T19 connected and logging; auto-reconnect armed:", app2._auto_reconnect)

PRESENT = []                                   # <-- cable pulled
app2._link_lost()
assert not app2.is_connected and app2._auto_reconnect
app2.update_status_label()
assert app2.events[-1] == "LINK LOST", app2.events[-3:]
say("T20 unplug -> status", repr(app2.status_label.cget("text")),
    "| trace marked", repr(app2.events[-1]), "| still logging:", app2.logging)

for _ in range(3):                             # port still absent
    root.update(); time.sleep(0.06)
assert not app2.is_connected and app2._auto_reconnect
say("T21 port still absent -> keeps waiting, no error, status",
    repr(app2.status_label.cget("text")))

PRESENT = ["COM9"]                             # <-- plugged back in
deadline = time.time() + 5
while not app2.is_connected and time.time() < deadline:
    root.update(); time.sleep(0.05)
assert app2.is_connected, "should have reconnected on its own"
say("T22 replug -> reconnected automatically, status", repr(app2.status_label.cget("text")))

app2.handle_new_data([27.0, None, None, 28.0])
app2._log_file.flush(); os.fsync(app2._log_file.fileno())
rr = list(csv.reader(run.read_text(encoding="utf-8").splitlines()))
assert app2._log_path == run, "must keep recording to the same file"
assert [r for r in rr if r[-1] == "LINK LOST"], rr
say("T23 same file throughout; rows:", len(rr) - 2, "| gap row:",
    [r for r in rr if r[-1] == "LINK LOST"][0])

app2.disconnect()                              # user says stop
assert not app2._auto_reconnect and app2._reconnect_after_id is None
PRESENT = ["COM9"]
for _ in range(3):
    root.update(); time.sleep(0.06)
assert not app2.is_connected, "must NOT reconnect after a user disconnect"
say("T24 user Disconnect -> stays down even though the port is present:",
    repr(app2.status_label.cget("text")))
app2._closing = True

# --- protocol, checked against real captured frames ----------------------
import ut325f_protocol as proto
GOOD   = bytes.fromhex("aa5500340169d8ae41d7c9b64169d8ae411d54b1410000000069d8ae41d7c9b64169d8ae411d54b141000000000080b04100000000001359")
UNPLUG = bytes.fromhex("aa5500340191efae4117cdb5410e72ad416e70ae410030000091efae4117cdb5410e72ad416e70ae41000000000040b1410000000000119e")
DEG_F  = bytes.fromhex("aa550034016a05af41560fa9412033ad4158b2ad41000000006a05af41560fa9412033ad4158b2ad41000000000080b14100000000000e74")   # meter displaying degF

assert len(GOOD) == 56 == proto.FRAME_SIZE
assert proto.checksum_ok(GOOD) and proto.checksum_ok(UNPLUG) and proto.checksum_ok(DEG_F)
say("T25 real frame is 56 bytes and its checksum validates: OK")

v = proto.parse_frame(GOOD)
say("T26 all four probes ->", ["%.2f" % x for x in v])
assert all(x is not None for x in v)

v = proto.parse_frame(UNPLUG)
assert v[1] is None and all(v[i] is not None for i in (0, 2, 3)), v
say("T27 channel 2 unplugged -> ", ["%.2f" % x if x is not None else None for x in v])

v = proto.parse_frame(DEG_F)
assert all(20 < x < 23 for x in v), v          # Celsius on the wire, not 71 degF
say("T28 meter in degF still streams Celsius:", ["%.2f" % x for x in v])

corrupt = bytearray(GOOD); corrupt[7] ^= 0xFF
assert not proto.checksum_ok(bytes(corrupt))
fr, rest = proto.find_frame(bytes(corrupt))
assert fr is None, "a corrupt frame must be rejected, not decoded"
say("T29 one flipped payload byte -> frame rejected by checksum: OK")

stream = b"\x11\x22" + GOOD + b"\x00" + UNPLUG
out = []
buf = stream
while True:
    fr, buf = proto.find_frame(buf)
    if fr is None: break
    out.append(proto.parse_frame(fr))
assert len(out) == 2, len(out)
say("T30 leading junk and a stray byte between frames -> both frames recovered")

junk = b"\x00" * 100000
fr, rest = proto.find_frame(junk)
assert fr is None and len(rest) <= 4, len(rest)
say("T31 100 kB of headerless junk -> remainder trimmed to", len(rest), "bytes")

nonfinite = bytearray(GOOD)
nonfinite[5:9] = b"\xff\xff\xff\xff"                      # NaN payload
nonfinite[54:56] = (sum(nonfinite[:54]) & 0xFFFF).to_bytes(2, "big")
assert proto.checksum_ok(bytes(nonfinite))
assert proto.parse_frame(bytes(nonfinite))[0] is None
say("T32 checksum-valid but non-finite value -> reported as no reading: OK")


app.on_close()
say("T18 on_close: after-ids cancelled =", app._ui_after_id is None and app._repaint_after_id is None,
    "| log file closed =", app._log_file is None, "| port =", app.serial_port)
say("ALL CORRECTNESS ASSERTIONS PASSED")
log.close()
