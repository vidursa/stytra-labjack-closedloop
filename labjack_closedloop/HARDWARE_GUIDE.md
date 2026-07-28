# Going live: real camera in, real LabJack U3 out

This closed loop was built so the two hardware ends are **independent toggles** in
`run_experiment.py`. Nothing else in the code needs to change:

| End | Simulation | Real hardware |
|-----|-----------|---------------|
| Input (camera) | `USE_REAL_CAMERA = False` (plays the recorded video) | `USE_REAL_CAMERA = True` + `REAL_CAMERA = {...}` |
| Output (LabJack) | `LABJACK_BACKEND = "simulate"` (prints/records transitions) | `LABJACK_BACKEND = "u3"` |

**Flip them one at a time.** Recommended order:

1. Sim camera + sim LabJack — the current default (you have this working).
2. Sim camera + **real LabJack** — confirm pulses reach your device with a known input.
3. **Real camera** + real LabJack — the full experiment.

That way, if something breaks you know which end caused it.

---

## Part A — Real LabJack U3 output

### A1. Install the driver + Python library
The `u3` Python module (from **LabJackPython**) talks to the board through
LabJack's native driver, which must be installed first.

- **Windows:** install the LabJack **UD driver** (the "LabJack-Windows-Installer" /
  "LJUD" package from labjack.com). Then, in your stytra environment:
  ```
  pip install LabJackPython
  ```
- **Linux/macOS:** install the **Exodriver** (libusb-based) from labjack.com, then
  `pip install LabJackPython`.

Verify the board is seen (with the U3 plugged into USB, nothing else running):
```python
import u3
d = u3.U3()
print(d.configU3())     # prints serial number, hardware/firmware versions
d.close()
```
If that prints without error, the driver + library + board are all good.

### A2. Pick the output line
Set in `run_experiment.py`:
```python
LABJACK_BACKEND = "u3"      # <- the only required change to go live
LABJACK_CHANNEL = "FIO4"    # 3.3 V logic pulse
LABJACK_VOLTAGE = 3.3       # recorded in the log (informational for digital lines)
```

- **`"FIO4"` … `"FIO7"` / `"EIO0"` …** → 3.3 V digital logic pulse.
  On the **U3-HV**, `FIO0–FIO3` are permanently analog inputs, so you **must** use
  `FIO4` or higher for a digital output. (The device wrapper drives it with
  `setDOState`.)
- **`"DAC0"` / `"DAC1"`** → a true analog level, ~0–4.95 V. Use this for a real
  **5 V** pulse: set `LABJACK_CHANNEL = "DAC0"` and `LABJACK_VOLTAGE = 5.0`.
  (The wrapper writes the voltage to register 5000/5002.)

### A3. Wire it
- Digital (`FIO4`): connect **FIO4 → your device's trigger/signal input**, and a
  **U3 GND → device ground** (common ground is essential).
- Analog (`DAC0`): connect **DAC0 → signal**, **GND → ground**.
- Driving an LED/opto directly? Don't — a FIO/DAC line sources only a few mA. Use
  the pulse to switch a driver/transistor/opto-isolator that powers the load.

### A4. Bench-test the output before the fish
With the device (or a multimeter / scope / LED+resistor) on FIO4:
```python
import time, sys
sys.path.insert(0, ".")          # run from the labjack_closedloop/ folder
from labjack_device import LabJackU3Output

lj = LabJackU3Output(backend="u3", channel="FIO4", voltage=3.3)
for _ in range(5):
    lj.set_high(); time.sleep(0.2)
    lj.set_low();  time.sleep(0.2)
lj.close()
```
You should measure 5 clean 3.3 V blips. If yes, the output path is proven.

> **Note on timing:** software-timed USB commands have ~1–4 ms of jitter per call.
> For an excitation LED / opto pulse that's fine. In `PULSE_MODE = "level"` the line
> simply follows the bend (on while `tail_sum` is over threshold); in `"fixed"` mode
> each bend onset emits one `PULSE_WIDTH_S` pulse, rate-limited by `REFRACTORY_S`.
> For sub-ms hardware-timed pulses you'd use the U3's timer/PWM — not needed here.

---

## Part B — Real camera input

### B1. Install the camera SDK + Python bindings
The recorded clip is from a **Basler acA640-750um**, so `type="basler"`, which uses
**pypylon**:
```
pip install pypylon
```
(Install Basler's **pylon runtime/Suite** first if pypylon can't find the transport
layer.) Other backends stytra supports via the `type` key: `ximea`, `avt`,
`spinnaker`, `mikrotron`, `opencv` — each needs its own vendor SDK + Python package.

Confirm the camera is found:
```python
from pypylon import pylon
print(pylon.TlFactory.GetInstance().EnumerateDevices())   # should list your camera
```

### B2. Switch the config
In `run_experiment.py`:
```python
USE_REAL_CAMERA = True
REAL_CAMERA = dict(type="basler", max_buffer_length=12000)
```
`max_buffer_length` sizes the shared-memory frame ring — keep it ≥ the number of
frames in a session (200 fps × 60 s ≈ 12000). Optional camera params you can add to
`REAL_CAMERA` (handled by stytra's camera base): `downsampling=1`,
`roi=(x, y, w, h)`, `rotation=…`. Exposure/gain are set inside the Basler backend
(`open_camera`), tweak there if the image is too dark/bright.

That single toggle is all the closed loop needs — the estimator, threshold, pulse
logic, white circle, and saving are all camera-agnostic.

### B3. Re-check the tail tracking
`run_experiment.py` still loads the tracking parameters from the
`*_trackingparams.json` file, which were tuned on the recorded video. If the live
camera has the **same mount, orientation and framing**, they carry over directly.
If anything moved:

1. Launch it (`AUTO_START` won't matter for setup — you can set it False while
   tuning, or just let the 3 s delay pass and stop early).
2. In the camera panel, re-place the **tail start point** and **tail tip**, and
   check `n_segments`, so `tail_sum` responds cleanly as the fish bends.
3. Watch the live `tail_sum` trace and the `[trigger] session start/end` prints to
   confirm the threshold sits sensibly inside the real bend range, then set
   `BEND_THRESHOLD` accordingly.
4. **Direction:** if the white circle / pulse fires on **left** bends, flip
   `BEND_DIRECTION = -1`.

> stytra restores GUI params from `~/stytra_last_config.json` between runs, so once
> you've re-tuned tracking on the live camera it will persist. (If old values ever
> get in the way, delete that file to reset to code defaults.)

---

## Part C — Exact edits, at a glance

```diff
  # run_experiment.py
- LABJACK_BACKEND = "simulate"
+ LABJACK_BACKEND = "u3"

- USE_REAL_CAMERA = False
+ USE_REAL_CAMERA = True
  REAL_CAMERA = dict(type="basler", max_buffer_length=12000)
```
Optional, for a true 5 V pulse instead of 3.3 V logic:
```diff
- LABJACK_CHANNEL = "FIO4"
- LABJACK_VOLTAGE = 3.3
+ LABJACK_CHANNEL = "DAC0"
+ LABJACK_VOLTAGE = 5.0
```
Nothing in `stytra_integration.py`, `bend_trigger.py`, or `labjack_device.py`
changes — the device wrapper already lazily imports `u3` only when
`backend="u3"`, and keeps a single shared handle across stytra's stimulus
`deepcopy` so the board that fires is the same one that gets closed and logged.

---

## Part D — Run it & confirm

```
python run_experiment.py
```
Expected on a real run:
- Console: `Loaded tracking parameters …`, then `[trigger] session start: … threshold=…`.
- On each right bend past threshold: `[trigger] … ON` and `[LabJack FIO4] HIGH (3.300 V)`
  (real backend), plus the white circle on the stimulus window.
- Your external device pulses in sync.
- After `SESSION_DURATION_S`: `=== SESSION SAVED ===`, a `session end` summary,
  and the shell returns cleanly.

Saved under `../recordings/labjack_right_bend_cl/<date>_f0/`:
`*_video.mp4`, `*_behavior_log.*`, `*_estimator_log.*`, `*_stimulus_log.*`,
`*_metadata.json`, and `../recordings/labjack_pulses.csv`.

Inspect afterwards (threshold line is read from the metadata automatically):
```
python plot_session.py
```

---

## Part E — Safety / sanity checklist

- [ ] `u3.U3()` opens and `configU3()` prints — driver + board OK (A1).
- [ ] Common ground between U3 and the driven device (A3).
- [ ] Bench-tested pulses on FIO4/DAC0 with a meter or scope **before** connecting
      anything sensitive (A4).
- [ ] The LabJack line switches a driver, not the load directly (few-mA limit).
- [ ] Voltage confirmed: 3.3 V on FIO lines, ~5 V only on DAC.
- [ ] Camera enumerated by its SDK (B1); `max_buffer_length` ≥ session frames (B2).
- [ ] Tail start/tip and `n_segments` verified on the **live** image (B3).
- [ ] `BEND_DIRECTION` gives a circle on **right** bends; `BEND_THRESHOLD` sits
      inside the real `tail_sum` range (from the `session start/end` prints).
- [ ] One full session records and saves; shell exits without hanging.

If a step fails, drop back one rung on the ladder (real→sim for that end) to
isolate it.
