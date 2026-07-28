# Setting up on a new computer

This walks you from a bare machine to a running closed-loop session in
**simulate mode** (no camera, no LabJack). Going to real hardware is a separate,
later step documented in
[`labjack_closedloop/HARDWARE_GUIDE.md`](labjack_closedloop/HARDWARE_GUIDE.md).

Tested target: **Windows** with **Miniconda**. The steps are the same on Linux/
macOS; only the driver install and the activate command differ.

---

## 0. Prerequisites

| Tool | Why | Get it |
|------|-----|--------|
| **GitHub Desktop** | clone/sync this repo | <https://desktop.github.com> |
| **Miniconda** (or Anaconda) | Python 3.8 + PyQt5 environment | <https://docs.conda.io/en/latest/miniconda.html> |

> **Python version matters.** stytra pins to old `numba` / `PyQt5`, so it needs
> **Python 3.7–3.9**. Use **3.8** — do not use a newer interpreter. The provided
> `environment.yml` already pins this for you.

---

## 1. Get the repository

In **GitHub Desktop**: `File → Clone repository`, pick this repo, choose a local
folder. (Or `git clone <url>` on the command line.)

---

## 2. Put the simulation video in place ⚠️

The "camera" video is **not stored in git** — it is larger than GitHub's 100 MB per-file
limit, so it is deliberately git-ignored.

Copy it into the **repo root** (next to `labjack_closedloop/`).

`run_experiment.py` expects it at exactly that path and will stop with
`FileNotFoundError: Video not found: ...` if it is missing. (On the real rig you
use a live camera instead and the video is not needed — see the hardware guide.)

---

## 3. Create the environment

From the repo root (the folder containing `environment.yml`):

```bash
conda env create -f environment.yml
conda activate tailbend-cl
```

<details>
<summary>Prefer plain <code>venv</code> instead of conda?</summary>

You need a real 3.8/3.9 interpreter on PATH. PyQt5 may be harder to install this
way on Windows — conda is recommended.

```bash
python3.8 -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install PyQt5
```
</details>

---

## 4. Install stytra + the extras

```bash
# vendored, unmodified stytra 0.8.34 — installed editable from this repo
pip install -e ./stytra

# runtime extras (video decode, tracking, LabJack, plotting)
pip install -r requirements.txt
```

`LabJackPython` installs fine even with no LabJack attached; the `u3` module is
only imported when you switch to real hardware.

---

## 5. Run it (simulate mode)

```bash
cd labjack_closedloop
python run_experiment.py
```

What happens:

1. The stytra window opens. The recorded video plays as a live camera and
   tracking runs — **but nothing is recorded yet**.
2. In the **Camera** dock, drag the tail **start point** and **tip** until the
   overlay follows the tail. (You can freeze the view with the Camera dock's
   pause button to line points up on a still frame — then **un-pause before
   recording**.)
3. Press the **▶ play toggle in the top toolbar** — it sits just left of the
   `0/60s` progress bar — to begin recording. It becomes a ■ stop icon while
   running and **saves automatically** when the duration elapses.

> The ▶ in the top toolbar is the *recording* control. The pause button in the
> Camera dock only freezes the live view — it does **not** start a session.

Useful flags:

```bash
python run_experiment.py --duration 300     # 5-minute session
python run_experiment.py --params last       # reuse last run's tail points (skip the JSON)
python run_experiment.py --autostart         # hands-off: starts itself after 3 s
python run_experiment.py --help
```

Each session is written under `../recordings/labjack_right_bend_cl/…`
(video, per-frame tail log, estimator log, stimulus log, metadata), plus
`../recordings/labjack_pulses.csv` with every output transition.

---

## 6. Check things without the rig (optional)

```bash
cd labjack_closedloop
python test_offline_logic.py                 # unit tests, stdlib only
python simulate_tracking.py                  # synthesise a dataset under ../recordings/simulated
python plot_session.py                       # plot the newest session (needs matplotlib)
```

---

## 7. Going to real hardware

When the simulate run looks right, flip the two switches near the top of
`run_experiment.py` **one at a time**:

```python
LABJACK_BACKEND = "u3"      # real LabJack U3 output
USE_REAL_CAMERA = True      # real camera instead of the recorded video
```

On Windows the U3 also needs LabJack's **UD driver** (the `u3` Python module
talks to it); on Linux it is the **Exodriver**. Full wiring, a bench test and a
safety checklist are in
[`labjack_closedloop/HARDWARE_GUIDE.md`](labjack_closedloop/HARDWARE_GUIDE.md).

---

## Troubleshooting

| Symptom | Fix |
|---------|-----|
| `FileNotFoundError: Video not found` | The `.mp4` isn't in the repo root — see **step 2**. |
| `numba` / `PyQt5` build or import errors | Wrong Python version. Recreate the env on **Python 3.8** (step 3). |
| Blank / no GUI on a headless Linux box | Run under `xvfb-run python run_experiment.py`. |
| Recorded video plays ~8× too slow | Already handled — `CAMERA_FPS` (default 200) is stamped into the `.mp4`. Set it to your camera's true frame rate. |
| Circle fires on **left** bends | Set `BEND_DIRECTION = -1` in `run_experiment.py`. |
| The command prompt seems to hang after closing the window | Expected briefly — stytra's background processes are force-terminated on exit; it returns on its own. |
