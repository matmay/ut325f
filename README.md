# ut325f

A Python datalogger for the [UNI-T UT325F 4-channel thermometer](https://meters.uni-trend.com/product/ut325f-4-channel-thermometer/).

Fork of [kamratia1/ut325f](https://github.com/kamratia1/ut325f) with the serial
protocol decoded from a real capture, checksum validation, crash-safe recording
and a single-file build.

## Quick start

```
pip install -r requirements.txt
python ut325f_gui_standalone.py
```

`ut325f_gui_standalone.py` is self-contained — it is the only file you need to
run the logger. Python 3.9 or newer; tested on 3.11.

## Files

| File | Purpose |
|---|---|
| `ut325f_gui_standalone.py` | **The single file to run.** Generated — do not edit. |
| `ut325f_gui.py` | The GUI, editable source. Imports `ut325f_protocol`. |
| `ut325f_protocol.py` | Frame format: framing, checksum, parsing. Shared. |
| `ut325f_print.py` | Prints temperatures to the console. `python ut325f_print.py COM6` |
| `build_standalone.py` | Regenerates the standalone from the two sources. |
| `test_correctness.py` | 41 assertions. Runs without the meter attached. |

Editing `ut325f_gui.py` or `ut325f_protocol.py`? Re-run `python build_standalone.py`
afterwards, or the standalone will be stale.

## Using it

Pick the serial port and press **Connect**. Temperatures appear immediately;
the status shows `CONNECTING...`, then `CONNECTED` once a valid frame arrives,
or `NO DATA` if the port opened but nothing parses — usually the wrong port.

**Start** asks where to record, then writes every sample to that file as it
arrives. There is no separate save step and nothing is held only in memory, so
an interrupted run is still on disk. Cancelling the dialog starts nothing.

* **Pause / Resume** — breaks the trace and marks the gap in the file.
* **Note** — type text, press Enter or the button, and it is attached to the
  `Event` column of the next sample.
* **New File** — closes the current recording and starts a fresh one without
  dropping the serial connection.
* **Open Folder** — reveals the file currently being recorded.

Channel labels, trace colours, the rolling time window and the Y-axis limits can
all be changed while a run is in progress.

If the USB device is unplugged, the run is **not** lost: the gap is marked
`LINK LOST`, the status shows `RECONNECTING...`, and recording resumes into the
same file when the device comes back. Pressing **Disconnect** stops that — it
will not reconnect against your wishes.

## CSV format

Line 1 is the PC wall-clock start time. Line 2 is the header. `Time (s)` is
relative to the moment **Start** was pressed.

```
Test Start Time (PC): 2026-11-11 14:27:39
Time (s),Channel 1,Channel 2,Channel 3,Channel 4,Event
0.274000,21.86,22.85,21.86,22.17,
0.548000,21.87,,21.68,21.80,
0.822000,21.88,22.84,21.87,22.18,heater on
0.822001,,,,,PAUSE
```

A cell is **blank** when that channel has no probe connected. A non-finite
reading is written as `NaN`, never blanked, so it cannot be mistaken for a
missing probe. The `Event` column holds your notes, plus `PAUSE` and
`LINK LOST` markers on synthetic rows that carry no measurement.

To read it back, drop the marker rows — they are the ones with no channel
values at all:

```python
df = pd.read_csv(path, skiprows=1)
chans = df.columns[1:5]
df = df[~df[chans].isna().all(axis=1)]
```

Filtering on `Event` being empty would be wrong: your notes sit on real samples.

## Protocol

Decoded from 155 captured frames. See `ut325f_protocol.py` for the full layout.

* Frames are **56 bytes**, not 53 as previously assumed.
* The last two bytes are the low 16 bits of the sum of bytes 0..53, big-endian.
  Frames failing this are discarded and the reader resyncs, so a false header
  lock can no longer produce plausible-looking garbage.
* Per-channel status: `0x00` valid, `0x30` no probe. Anything else is treated as
  "no reading" rather than guessed at.
* **The meter always streams Celsius**, whatever its display is set to. With the
  display reading 71.5 °F the wire carried 21.88. Do not convert.
* Bytes 25..44 mirror the channel data, but their status bytes do not flag a
  disconnected probe — use the first group.

## Testing

```
python test_correctness.py
```

No hardware needed. Covers framing and checksum rejection against real captured
frames, the CSV encoding, the recording lifecycle, the timebase, and
unplug/replug against a simulated port.

## Limitations

* Reconnect only reopens the *same* port name. If a replug renames the device
  node, reselect the port manually.
* The extra float at bytes 45..48 is not identified and is not exposed.
* Y-axis manual limits are capped at −100…200 °C, below the instrument's range.

## Screenshots

> Note: these predate the current left-hand panel, which now has
> Start / Open Folder / New File and the note box, and the CSV shown has no
> `Event` column. The plot area is unchanged.

![GUI](img/image.png)

![GUI with two channels](img/image-1.png)

## License

MIT, © 2025 Kishan Amratia — see `LICENSE`. Unchanged from upstream.
