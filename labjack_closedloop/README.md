# Closed-loop tail-bend → LabJack U3 pulse

A stytra experiment that, during a recording session, tracks the fish tail and
**emits a pulse on a LabJack U3-HV whenever the tail bends more than 10° to the
right**. A white circle is shown on the stimulus display when the loop fires, so
you can see it working. All frame times, stimulus/output times and the video are
saved when the session ends.

Because no camera or LabJack is connected during development, the camera is
simulated from the recorded video and the LabJack is simulated in software.

## Files

| File | Runs where | Purpose |
|------|-----------|---------|
| `bend_trigger.py` | anywhere (stdlib only) | Pure decision logic: right-bend detector + fixed-width pulse controller |
| `labjack_device.py` | anywhere (stdlib only) | LabJack U3 output wrapper, `backend="simulate"` or `"u3"` |
| `labjack_stream.py` | needs a real U3 | Background TTL stream recorder (camera + stimulation TTLs → CSV) |
| `stytra_integration.py` | stytra env | `RightBendEstimator` + `RightBendTriggerStimulus` |
| `run_experiment.py` | stytra env | The runnable experiment (video-as-camera, loads tracking params, saves data) |
| `simulate_tracking.py` | anywhere (stdlib only) | Generate a simulated tracking + pulse CSV dataset from the params JSON |
| `plot_session.py` | needs matplotlib | Overlay `tail_sum` with pulse markers from a saved (or simulated) session |
| `test_offline_logic.py` | anywhere (stdlib only) | Unit tests for the decision + pulse logic |

Nothing in the `stytra/` package is modified — this is purely additive.

## How it works

```
video frame ─▶ tracking process ─▶ acc_tracking (tail_sum per frame)
                                        │
      RightBendEstimator.get_bend() ────┘  → bend angle in degrees
                                        │
      RightBendTriggerStimulus.update() ┤  once per display frame:
                                        ├─ bend > 10° right?  → LabJack pulse + white circle
                                        └─ else               → output low + black screen
```

The bend metric is stytra's `tail_sum` (a cumulative tail-curvature value, in
radians) converted to degrees. Its **sign** encodes left vs. right; which sign is
"right" depends on camera orientation, so if the circle lights up on *left* bends
just set `BEND_DIRECTION = -1` in `run_experiment.py`.

## Pulse modes

Set in `run_experiment.py`:

- **`PULSE_MODE = "fixed"`** (default) — a single **5 ms** pulse (`PULSE_WIDTH_S`)
  on the *rising edge* of each right bend, rate-limited by a **100 ms** refractory
  (`REFRACTORY_S`). One clean pulse per bend event. The circle is held on for
  `CIRCLE_DISPLAY_S` (default 0.1 s) so a sub-frame pulse is still visible.
- **`PULSE_MODE = "level"`** — output stays high for as long as the tail is bent
  past threshold; the circle follows the level.
- **`PULSE_MODE = "predictive"`** — **anticipatory**, to beat the delay/jitter of
  a fast (~12 Hz) tail. Instead of waiting for the tail to reach the trigger-side
  threshold (by which point it is already well into the bend), it waits for the
  tail to bend to the **opposite** side past `OPPOSITE_THRESHOLD` (a real beat),
  then fires as the tail swings back **up through `FIRE_LEVEL`** — i.e. *before*
  it has bent to the trigger side. `FIRE_LEVEL` is where on the return stroke to
  fire: `0.0` = the neutral crossing, **negative** = earlier (still on the
  opposite side), positive = a little later. One pulse per beat. This is the
  "stimulate a set amount after the other-side bend / at the zero crossing" mode.

> **Pulse width is timed by the U3, not the PC.** `fixed`/`predictive` modes call
> `LabJackU3Output.pulse_async()`, which sends both edges plus the delay as a
> *single* Feedback command (`BitStateWrite` → firmware `Wait` → `BitStateWrite`),
> so the width is constant to ~128 µs. This matters for a level-sensitive load
> such as an LED driver, where pulse width == light dose: timing the two edges
> separately from Python would jitter the width by several ms, because each edge
> is its own USB transaction with independent latency. The command is issued on a
> daemon worker thread, so it never stalls the Qt event loop.
>
> Caveats: the firmware path needs a **digital** channel (`FIO4`+). A `DAC0`
> channel has no equivalent Feedback wait, so it falls back to two writes with a
> precise wait between them — off the event loop, but the width still carries the
> difference of two USB latencies. Pulse *onset* latency is unaffected either way.

## LabJack U3-HV wiring

The output defaults to a **3.3 V logic pulse on FIO4**:
```python
LABJACK_CHANNEL = "FIO4"   # digital line
LABJACK_VOLTAGE = 3.3      # nominal logic-high, for the log
```
**Important for the -HV variant:** `FIO0–FIO3` are fixed **analog inputs**, so a
digital output must use `FIO4–FIO7` (or `EIO0–EIO7`). Point `LABJACK_CHANNEL` at
an int (`4`) or string (`"FIO4"`, `"EIO0"`). If you later want a true 5 V level
instead of 3.3 V logic, set `LABJACK_CHANNEL = "DAC0"` and `LABJACK_VOLTAGE = 5.0`
(analog out, ~0–4.95 V).

## Setup (in a virtual environment)

> stytra requires **Python 3.7–3.9** (numba / PyQt5). Do **not** use a newer
> interpreter. Create an isolated environment so your base Python stays clean.

**conda (recommended):**
```bash
conda create -n labjack_cl python=3.9
conda activate labjack_cl
pip install -e ../stytra          # the local stytra copy in this repo
pip install -r requirements.txt   # av, opencv-python, LabJackPython
```
**venv (needs a 3.8/3.9 interpreter):**
```bash
python3.9 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -e ../stytra
pip install -r requirements.txt
```
A GUI display is required. On a headless Linux box run under `xvfb-run`.

## Run

```bash
python run_experiment.py                       # 60 s, params from PARAMS_FILE, manual start
python run_experiment.py --duration 300        # 5-minute session
python run_experiment.py --params last         # reuse the previous run's tail points
python run_experiment.py --autostart           # hands-off: starts itself after 3 s
python run_experiment.py --ttl-stream          # also record the TTL analog inputs
python run_experiment.py --help
```
1. The stytra control window opens; the camera streams and tracking runs **live,
   but nothing is being recorded yet**.
2. Drag the tail **start point** and **tip** until the tracked overlay follows
   the tail. Take as long as you need.
3. Press **▶ (play)** to begin recording for `--duration` seconds. Press it
   again to stop early; it saves automatically when the time elapses.
4. Right bends past threshold → pulse (printed to console in simulate mode) +
   white circle.
5. Data is written to `../recordings/` on completion.

Pass `--autostart` to skip steps 1–3 and have it start on its own — useful once
the tracking points are dialled in and saved.

### Tracking parameters: file vs. last run

`--params file` (default) loads the exported `*_trackingparams.json`.
`--params last` loads nothing: stytra writes the entire parameter tree to
`~/stytra_last_config.json` on exit and restores it at startup, so the tail
points you dragged last time are already in force. Use `last` once you have the
tracking set up the way you want it — otherwise the JSON overwrites your
adjustments on every launch.

### Frame rate

`CAMERA_FPS` (default 200) is both the acquisition rate and the framerate
stamped into the saved `.mp4`. stytra's writer defaults to 24 fps, which makes a
200 fps recording play back **8× too slow** — 60 s of data becomes an 8-minute
video. The frames and every timestamp were always correct; only the container's
playback tag was wrong. Set `CAMERA_FPS` to the camera's real rate.

### Output (saved to `../recordings/<protocol>/<fish_id>/`)
- `*_video.mp4` — recorded frames
- `*_behavior_log.*` — per-frame `tail_sum` + tail angles **with frame timestamps**
- `*_estimator_log.*` — bend angle (degrees) over time
- `*_stimulus_log.*` — `bend_angle`, `triggered`, `output_v`, `pulse` per frame **with timestamps**
- `*_metadata.json` — full session metadata
- `*_ttl_stream.csv` — (with `--ttl-stream`) 1 kHz analog record of the camera /
  stimulation TTLs, `t` in seconds since protocol start
- `../recordings/labjack_pulses.csv` — every high/low transition of the output line

## Recording the camera / stimulation TTLs

`--ttl-stream` runs the U3 in **stream mode** on AIN1–AIN3 at 1 kHz *while* the
closed loop is running, on the same device, and writes `<session>_ttl_stream.csv`
with a column pair (`<label>_v` raw volts, `<label>` logic 0/1) per channel.
Configure the channels/labels/rate near the top of `run_experiment.py`.

Sample times come from the U3's own scan clock (start + index ÷ rate), not from
`datetime.now()` per packet, so they don't drift with however long the PC took to
drain the queue.

> **One device, two access modes.** The U3 accepts our output pulses while
> streaming, but each command costs a few stream samples. A small non-zero
> "missed" count is normal and is printed at the end of the session. For zero
> missed samples, use a second U3 for the output.
>
> **Tip:** wire the FIO4 output back into one of the streamed analog inputs. You
> then get a ground-truth 1 kHz record of the actual stimulation pulse on exactly
> the same clock as the camera strobes — far better than trusting two separate
> software timestamps to line up.
>
> On a U3-**HV**, FIO0–FIO3 are permanently analog, so no `configIO` is needed
> (`TTL_STREAM_CONFIG_ANALOG = False`). On a standard U3, set it True — and note
> that `configIO` resets FIO4–FIO7 to digital *inputs*, which is why the output
> line's direction is re-asserted immediately afterwards.

## Simulated dataset (no stytra / hardware needed)

To sanity-check the tracking data format and the closed-loop logic without the
rig, generate a synthetic session from the same tracking params:
```bash
python simulate_tracking.py
```
It reads `n_output_segments` etc. from `../Basler_..._trackingparams.json`,
synthesises 30 s of frames at 250 Hz with realistic tail beating and scripted
right/left bends, and writes to `../recordings/simulated/`:
- `simulated_behavior_log.csv` — `t, tail_sum, theta_00..theta_04`
- `simulated_stimulus_log.csv` — `t, bend_deg, triggered, output_v, pulse`
- `simulated_labjack_pulses.csv` — `onset_t, offset_t, width_s, voltage`

`tail_sum` in the output satisfies stytra's identity
`tail_sum = theta_04 + theta_03 − theta_00 − theta_01`, and left bends correctly
produce no pulses.

## Inspecting a session afterwards

Overlay the `tail_sum` trace with the pulse markers (works on real or simulated logs):
```bash
python plot_session.py                       # newest session under ../recordings
python plot_session.py ../recordings/simulated
python plot_session.py <session_dir> --threshold 1.0 --direction 1
python plot_session.py <session_dir> --save out.png --no-show   # headless
```
Top panel: `tail_sum` with the threshold line and a red mark at every pulse onset;
bottom panel: the output-voltage train. Columns are matched by name, so it reads
both stytra's prefixed logs (`right_bend_trigger_pulse`, `t` last) and the bare
simulated ones. A `session_plot.png` is written into the session folder.

## Testing the logic

```bash
python test_offline_logic.py
```
Covers the threshold/direction decision, the simulated LabJack edges + CSV log,
a closed-loop replay (N right bends → N pulses), and the fixed-width pulse
controller (single pulse per sustained bend, refractory rate-limiting, circle
display window).

## Going to real hardware

Two independent toggles in `run_experiment.py`:

```python
LABJACK_BACKEND = "u3"      # real LabJack U3 output (needs LabJackPython + driver)
USE_REAL_CAMERA = True      # real camera instead of the recorded video
REAL_CAMERA = dict(type="basler", max_buffer_length=12000)
```

Flip them **one at a time** (sim→real for one end, verify, then the other).
Full step-by-step — driver install, wiring, bench test, camera setup, tracking
re-check, and a safety checklist — is in **[HARDWARE_GUIDE.md](HARDWARE_GUIDE.md)**.
