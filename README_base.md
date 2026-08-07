# Closed-loop tail-bend → LabJack stimulation

A [stytra](https://github.com/portugueslab/stytra)-based **closed-loop** rig for
head-restrained zebrafish. It tracks the fish tail in real time and, the moment
the tail bends past a threshold, drives a **TTL pulse on a LabJack U3** to fire
external hardware (an LED driver / opto-stimulation) — no visual stimulus in the
loop. A white circle flashes on the display so you can see every trigger, and
the video, per-frame tail trace, trigger times and pulse log are all saved.

During development there is no rig attached, so the **camera is simulated from a
recorded video** and the **LabJack is simulated in software**. Flipping to real
hardware is two independent switches (see
[`labjack_closedloop/HARDWARE_GUIDE.md`](labjack_closedloop/HARDWARE_GUIDE.md)).

> **New to this repo? Start with [`SETUP.md`](SETUP.md)** — a from-scratch
> install for a fresh computer. The deep technical reference lives in
> [`labjack_closedloop/README.md`](labjack_closedloop/README.md).

---

## What it does

```
video frame ─▶ tracking process ─▶ tail_sum (tail curvature per frame)
                                        │
        RightBendEstimator ────────────┘  → bend angle (degrees)
                                        │
        RightBendTriggerStimulus  ──────┤  once per display frame:
                                        ├─ bent past threshold?  → LabJack pulse + white circle
                                        └─ else                  → output low + black screen
```

Three trigger modes (set in `labjack_closedloop/run_experiment.py`):

- **`fixed`** — one firmware-timed pulse per bend onset, with a refractory gap.
- **`level`** — output stays high the whole time the tail is over threshold.
- **`predictive`** — *anticipatory*: waits for the opposite-side beat, then fires
  as the tail swings back through a set level — firing **before** the tail
  reaches the trigger side, to cancel a fast beat's latency.

The pulse width is timed by the U3's own firmware (constant to ~128 µs), which
matters for a level-sensitive load like an LED driver, where pulse width = light
dose.

## Repository layout

```
.
├── README.md                     ← you are here (project overview)
├── SETUP.md                      ← fresh-computer install walkthrough
├── requirements.txt              ← pip extras on top of stytra
├── environment.yml               ← conda env (Python 3.8 + PyQt5)
├── recordings/                   ← session output is written here (git-ignored)
├── Basler_*_trackingparams.json  ← exported tail-tracking parameters
├── Basler_*.mp4                  ← the simulation "camera" video (NOT in git — see SETUP.md)
├── labjack_closedloop/           ← all the experiment code
│   ├── run_experiment.py         ← the runnable experiment (config at the top)
│   ├── labjack_device.py         ← LabJack U3 output wrapper (u3 / simulate)
│   ├── labjack_stream.py         ← optional TTL stream recorder
│   ├── stytra_integration.py     ← the estimator + trigger stimulus
│   ├── bend_trigger.py           ← pure decision logic (no deps)
│   ├── plot_session.py           ← inspect a saved session
│   ├── simulate_tracking.py      ← synthesise a dataset with no rig
│   ├── test_offline_logic.py     ← unit tests (stdlib only)
│   ├── README.md                 ← full technical reference
│   └── HARDWARE_GUIDE.md         ← going live: real camera + real LabJack
└── stytra/                       ← vendored, unmodified stytra 0.8.34 (installed editable)
```

## Quick start

```bash
conda env create -f environment.yml
conda activate tailbend-cl
pip install -e ./stytra
pip install -r requirements.txt

cd labjack_closedloop
python run_experiment.py            # camera + tracking go live; press ▶ to record
```

Full details, including how to obtain the simulation video (which is **not**
stored in git), are in **[`SETUP.md`](SETUP.md)**.

## Licensing

Vendored **stytra is GPLv3+** (see `stytra/LICENSE.txt`).

