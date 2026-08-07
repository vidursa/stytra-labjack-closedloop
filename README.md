# Stytra — Version 2: continuous acquisition + save-clip-on-behaviour

This is a copy of the closed-loop project with a **non-blocking rolling-clip
save** built on top. The goal (from the v2 discussion):

> Keep the real camera acquiring continuously with the closed loop active, and
> instead of pressing a record button, **save the last N seconds whenever a
> behaviour occurs** — without dropping frames, stalling the loop, or losing
> time-sync with the other logs.

Version 1's "save buffer" could not do this: it encoded the whole ring buffer
**inline in the camera acquisition loop** (blocking `cam.read()` for seconds →
dropped frames, and the closed loop went blind during the save) and triggered
itself by **pyautogui screen-scraping** hardcoded `F:/todelete/*.png` buttons.

---

## What changed

### 1. The clip save no longer blocks acquisition
`stytra/stytra/hardware/video/__init__.py` (`CameraSource`):

- A **background writer thread** is started inside the camera process. It owns
  the PyAV encode; PyAV releases the GIL while encoding, so `cam.read()` and the
  ring-buffer fill keep running throughout.
- On a save trigger, the acquisition loop does an **O(1) ring-buffer swap**: it
  hands the full buffer object to the writer thread and installs a spare buffer
  to keep acquiring into. It never waits on the encode *or* on a memory copy
  (the copy — `get_all()`'s `np.concatenate` — happens on the writer thread).
- **Overlap handling:** up to `_max_pending_saves` (2) clips can be queued while
  one encodes; idle buffers are pooled (`_max_spare_buffers`, 2) for reuse. If a
  trigger arrives while the writer is saturated, the save is **skipped with a
  logged warning** (`W:Clip save skipped -- writer busy`) rather than dropping
  frames silently.
- All pyautogui / `F:/todelete/*.png` / `self.saved`-latch code is **removed**.

### 2. Saves are triggered by a counter, not a screen click
A new camera param **`save_request_count`** (int). Each increment triggers
exactly one clip. A monotonic counter is robust to param-sync coalescing in a
way a `True/False` pulse is not. It can be bumped from two places:

- **Manual:** the camera-view **"save clip" button** (see #4).
- **Automatic:** the stimulus, on each behaviour onset (see #3).

### 3. Stimulus can auto-save on behaviour
`labjack_closedloop/stytra_integration.py` — new `RightBendTriggerStimulus`
options `autosave_clip` and `clip_min_interval_s`. On each behaviour **onset**
(the rising edge of `triggered`, so it works in `fixed`, `level`, and
`predictive` modes) it bumps `save_request_count`, rate-limited to one clip per
`clip_min_interval_s`. It's a no-op (with a printed note) unless a real camera
exposes the rolling buffer.

Wired in `run_experiment.py` via new constants:

```python
AUTOSAVE_CLIP_ON_STIM = False   # set True to save a clip on each behaviour
CLIP_LENGTH_S         = 60.0    # rolling buffer length saved per clip (s)
CLIP_MIN_INTERVAL_S   = 30.0    # min gap between clip saves
```

`run_experiment.py` also sets the camera's `ring_buffer_length = CLIP_LENGTH_S`
at startup (real camera only).

### 4. The save-clip button has its own icon
The v1 button reused `rewind.svg`, so it looked identical to the rewind button.
v2 adds `stytra/stytra/icons/save_clip.svg` — a video frame with a download
arrow — and the button is now a momentary click (not a stuck-on toggle).

### 5. The save writes a COMPLETE SET, not just video
A save now writes the same set of files a normal protocol run would — video **+
behaviour log + stimulus log + estimator log + metadata** — for the last N s,
under one shared `HHMMSS_` basename. The log snapshots are taken in the main
process (where the accumulators live) and share the basename with the video
(the main process picks it and passes it to the camera via `save_clip_basename`).

### 6. The trace plot stays live during the protocol, with markers
`keep_plot_live_during_protocol` (set in `run_experiment.py`) stops stytra from
freezing the trace at ▶, so the tail trace, estimator, and stimulus decision
signals scroll in real time. A **red** vertical line marks recording start; an
**orange** line marks the last save and scrolls left over N s, so its distance
from the right edge shows how much not-yet-saved data is still buffered.

### 7. Two record modes (`RECORD_MODE` in `run_experiment.py`)
- **`"session"`** (default): classic stytra — press ▶ to record for
  `SESSION_DURATION_S`; video + all logs written at the end. The save-buffer
  button is hidden (recording and buffer-save are separate modes, so two video
  writers never run at once).
- **`"buffer"`**: an always-on rolling recorder. **No** full-session video and
  **nothing** is written when you stop; the closed loop runs open-ended and the
  save button (or `AUTOSAVE_CLIP_ON_STIM`) writes a complete set for the window
  since the last save. Consecutive saves are **back-to-back and non-overlapping**
  — each grabs `min(time since last save, ring_buffer_length)`; wait longer than
  the buffer and it caps at what the buffer still holds. The in-RAM logs are
  trimmed to the buffer window each tick so an open-ended run stays bounded.

---

## Time-sync: how a saved set lines up with the logs

Each save writes into the session folder, named `<HHMMSS_>…`:

| file | content |
|------|---------|
| `…video.mp4` | the video, at the true camera framerate |
| `…video_times.csv` | **per-frame** camera hardware timestamps (`t`, `logic`) |
| `…clocksync.csv` | newest-frame camera timestamp vs host `time.time()` and `time.monotonic()` at the save instant |
| `…behavior_log.csv` | last-N s of tracking (tail_sum + tail angles), stytra `_elapsed` clock |
| `…stimulus_log.csv` | last-N s of the stimulus log (tail_sum/baseline/triggered/output_v/pulse) |
| `…estimator_log.csv` | last-N s of the estimator log |
| `…metadata.json` | parameter-tree snapshot |

To align a clip to the behaviour log and `labjack_pulses.csv`:

- Every frame in the clip has a camera hardware timestamp (`video_times.csv`).
- `clocksync.csv` anchors that camera clock to the host `monotonic` clock — the
  same clock `labjack_pulses.csv` uses — so pulses map directly onto frames.
- The behaviour/stimulus log is on stytra's `_elapsed` clock; bridge it through
  the host wall/monotonic anchor (offsets are constant within a run).

There are still three clocks (camera ns, stytra `_elapsed`, LabJack monotonic) —
v2 does **not** remove them, it just records enough to reconcile them offline.

---

## Residual considerations (read before relying on it)

- **Memory.** Peak ≈ 2 full buffers (one filling + one encoding), each
  `framerate × CLIP_LENGTH_S` frames. At 200 fps × 60 s × 640×480 mono ≈ 3.6 GB
  each. Pooled spares hold no array (freed after encode), so the pool itself is
  cheap. Keep an eye on RAM if you raise `CLIP_LENGTH_S` or resolution.
- **Buffer cap.** stytra caps the ring buffer at `max_buffer_length` frames
  (12000 = exactly 60 s @ 200 fps). A longer or faster clip is **truncated** to
  the cap, with a `W:Replay buffer too big …` note in the camera log. To go
  beyond 60 s @ 200 fps, raise `max_buffer_length` in `CameraSource.__init__`.
- **Encode time vs trigger rate.** A 60 s clip takes a while to encode. With
  `clip_min_interval_s` ≥ a few × the encode time you'll never hit the "writer
  busy" skip; if you shorten it, expect occasional skips (logged, not silent).
- **Shutdown.** On close, the writer is given 30 s to finish an in-flight clip;
  a clip still encoding past that may be left incomplete (daemon thread).

---

## Rig test checklist (Imaging PC, real camera)

1. **Import/boot:** `python labjack_closedloop/run_experiment.py --help` runs
   (no import errors from the edited stytra).
2. **Button:** with a real camera, the camera view shows the new **save-clip**
   icon (not the rewind twin). Click it → a `…video.mp4` + `…video_times.csv` +
   `…clocksync.csv` appear in the session folder; the console shows
   `I:Clip save queued …` then `I:Clip saved -> …`.
3. **No stall:** while a clip encodes, confirm the tail plot / tracking keeps
   updating and pulses still fire (the whole point — no blind gap).
4. **Playback speed:** the saved `…video.mp4` plays at real time (framerate =
   the "Framerate (Hz)" control), not 8× slow.
5. **Auto-save:** set `AUTOSAVE_CLIP_ON_STIM = True`, run; each behaviour onset
   prints `[clip] save requested …` and drops a clip, at most one per
   `CLIP_MIN_INTERVAL_S`.
6. **Sim is a no-op:** with `USE_REAL_CAMERA = False`, no save-clip button, and
   `autosave_clip` prints the "no rolling buffer" note once — nothing crashes.
7. **Sync spot-check:** cross-check one pulse in `labjack_pulses.csv` against the
   clip via `clocksync.csv` + `video_times.csv` — the pulse should land on the
   expected frame.

---

## Deployment

As before, copy **whole files** to the rig (they replace v1 counterparts):

- closed loop: `labjack_closedloop/{run_experiment,stytra_integration,bend_trigger}.py`
- stytra: `stytra/stytra/hardware/video/__init__.py`,
  `stytra/stytra/gui/camera_display.py`, and the new
  `stytra/stytra/icons/save_clip.svg`

(Version 1's two framerate patches — `tracking_experiments.py` and the buffer
writer's `output_framerate` — are already carried in this copy.)
