"""Generate a *simulated* stytra tracking + stimulation dataset.

This does NOT run stytra. It synthesises the data stytra would produce for the
recorded video, using the tail-tracking settings from the ``*_trackingparams.json``
file (number of output segments, etc.), so you can confirm the data format and
the closed-loop logic before running on the real rig.

It reproduces, in pure Python:
  * frame generation at a fixed framerate (the "camera"),
  * a tracked tail: per-segment cumulative angles ``theta_00..theta_0{N-1}`` and
    the derived ``tail_sum`` (stytra's tail-bend scalar, radians),
  * the raw tail_sum threshold (trigger when tail_sum > 1.0 rad to the right),
  * the fixed-width right-bend -> pulse logic (same classes the real stimulus uses).

Outputs (written next to this script, in ../recordings/simulated/):
  * simulated_behavior_log.csv  - t, tail_sum, theta_00..theta_0{N-1}   (per frame)
  * simulated_stimulus_log.csv  - t, tail_sum, triggered, output_v, pulse (per frame)
  * simulated_labjack_pulses.csv- onset_t, offset_t, width_s, voltage    (per pulse)

Run:  python simulate_tracking.py
"""

import csv
import json
import math
import random
from pathlib import Path

from bend_trigger import RightBendDetector, FixedWidthPulseController

# --- config (mirrors run_experiment.py) ---------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
PARAMS_FILE = (
    REPO_ROOT
    / "Basler_acA640-750um__25004149__20260513_144734755_trackingparams.json"
)
OUT_DIR = REPO_ROOT / "recordings" / "simulated"

FRAMERATE_HZ = 250.0        # Basler high-speed acquisition
DURATION_S = 30.0
THRESHOLD = 1.0             # trigger when raw tail_sum (radians) > this
DIRECTION = 1
PULSE_WIDTH_S = 0.005
REFRACTORY_S = 0.1
NOMINAL_V = 3.3             # FIO4 logic-high on the U3-HV
SEED = 42


def load_n_output_segments(params_file):
    """Read n_output_segments from the tracking-params JSON (default 5)."""
    if not params_file.exists():
        print("WARNING: params file not found, assuming 5 segments:", params_file)
        return 5
    params = json.load(open(str(params_file)))
    tail = params["pipeline_params"]["/source/filtering/tail_tracking"]
    return int(tail.get("n_output_segments", 5))


def synth_tip_bend(t, rng):
    """Return the simulated tail-*tip* bend (degrees) at time ``t``.

    Baseline rest (small noise) punctuated by scripted events:
      * symmetric beating bouts (oscillation, net ~0 -> occasional right pulses),
      * sustained rightward holds (clear, single pulse each),
      * a sustained leftward hold (must NOT trigger).
    """
    # scripted (start_s, kind, duration_s, amplitude_deg)
    # amplitudes chosen so tail_sum (~1.2*rad(tip)) crosses the 1.0 rad threshold
    # on rightward events (tip > ~48 deg) but not on the left hold.
    events = [
        (3.0, "hold_right", 0.6, 70.0),
        (6.0, "beat", 0.5, 75.0),
        (9.0, "hold_left", 0.6, 70.0),
        (12.0, "hold_right", 0.5, 60.0),
        (15.0, "beat", 0.6, 80.0),
        (18.0, "hold_right", 0.4, 55.0),
        (22.0, "beat", 0.4, 72.0),
        (25.0, "hold_right", 0.7, 65.0),
    ]
    val = rng.gauss(0.0, 1.2)  # resting baseline jitter (degrees)
    for start, kind, dur, amp in events:
        if start <= t < start + dur:
            if kind == "hold_right":
                # rise, hold, fall (raised-cosine envelope) on the +(right) side
                env = math.sin(math.pi * (t - start) / dur)
                val += amp * env
            elif kind == "hold_left":
                env = math.sin(math.pi * (t - start) / dur)
                val += -amp * env
            elif kind == "beat":
                env = math.sin(math.pi * (t - start) / dur)      # bout envelope
                osc = math.sin(2 * math.pi * 22.0 * (t - start))  # 22 Hz tail beat
                val += amp * env * osc
    return val


def segment_angles(tip_bend_deg, n_seg, rng):
    """Distribute a tip bend into ``n_seg`` cumulative angles (radians).

    Angle grows ~linearly from base to tip (theta_{N-1} == tip bend), with a
    little independent per-segment tracking noise.
    """
    tip_rad = math.radians(tip_bend_deg)
    thetas = []
    for i in range(n_seg):
        base = tip_rad * (i + 1) / n_seg
        thetas.append(base + math.radians(rng.gauss(0.0, 0.4)))
    return thetas


def tail_sum_from_thetas(thetas):
    """stytra's output tail_sum = theta[-1] + theta[-2] - theta[0] - theta[1]."""
    if len(thetas) < 2:
        return thetas[-1] if thetas else 0.0
    return thetas[-1] + thetas[-2] - thetas[0] - thetas[1]


def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    rng = random.Random(SEED)

    n_seg = load_n_output_segments(PARAMS_FILE)
    theta_cols = ["theta_{:02d}".format(i) for i in range(n_seg)]
    dt = 1.0 / FRAMERATE_HZ
    n_frames = int(DURATION_S * FRAMERATE_HZ)

    detector = RightBendDetector(threshold=THRESHOLD, direction=DIRECTION)
    pulser = FixedWidthPulseController(
        pulse_width_s=PULSE_WIDTH_S, refractory_s=REFRACTORY_S
    )

    behavior_path = OUT_DIR / "simulated_behavior_log.csv"
    stim_path = OUT_DIR / "simulated_stimulus_log.csv"
    pulse_path = OUT_DIR / "simulated_labjack_pulses.csv"

    pulses = []
    n_over_frames = 0

    with open(behavior_path, "w", newline="") as bf, open(
        stim_path, "w", newline=""
    ) as sf:
        bw = csv.writer(bf)
        sw = csv.writer(sf)
        bw.writerow(["t", "tail_sum"] + theta_cols)
        sw.writerow(["t", "tail_sum", "triggered", "output_v", "pulse"])

        for i in range(n_frames):
            t = i * dt
            tip = synth_tip_bend(t, rng)
            thetas = segment_angles(tip, n_seg, rng)
            tail_sum = tail_sum_from_thetas(thetas)          # radians

            over = detector.is_triggered(tail_sum)
            n_over_frames += 1 if over else 0
            fired = pulser.step(t, over)
            active = pulser.is_pulse_active(t, window_s=0.1)

            if fired:
                pulses.append((t, t + PULSE_WIDTH_S))

            bw.writerow(
                ["{:.6f}".format(t), "{:.6f}".format(tail_sum)]
                + ["{:.6f}".format(a) for a in thetas]
            )
            sw.writerow(
                [
                    "{:.6f}".format(t),
                    "{:.6f}".format(tail_sum),
                    1 if active else 0,
                    "{:.3f}".format(NOMINAL_V if fired else 0.0),
                    1 if fired else 0,
                ]
            )

    with open(pulse_path, "w", newline="") as pf:
        pw = csv.writer(pf)
        pw.writerow(["onset_t", "offset_t", "width_s", "voltage"])
        for onset, offset in pulses:
            pw.writerow(
                [
                    "{:.6f}".format(onset),
                    "{:.6f}".format(offset),
                    "{:.4f}".format(PULSE_WIDTH_S),
                    "{:.3f}".format(NOMINAL_V),
                ]
            )

    # --- summary --------------------------------------------------------
    print("Simulated tracking dataset")
    print("  tracking params : {}".format(PARAMS_FILE.name))
    print("  output segments : {}  ({})".format(n_seg, ", ".join(theta_cols)))
    print("  framerate       : {:.0f} Hz".format(FRAMERATE_HZ))
    print("  duration        : {:.0f} s  ({} frames)".format(DURATION_S, n_frames))
    print("  threshold       : tail_sum > {:.2f} rad to the right".format(THRESHOLD))
    print(
        "  pulse           : {:.0f} ms fixed width, {:.0f} ms refractory".format(
            PULSE_WIDTH_S * 1e3, REFRACTORY_S * 1e3
        )
    )
    print(
        "  frames over thr : {} ({:.1f}%)".format(
            n_over_frames, 100.0 * n_over_frames / n_frames
        )
    )
    print("  pulses emitted  : {}".format(len(pulses)))
    print("  pulse onsets (s): {}".format(", ".join("{:.3f}".format(p[0]) for p in pulses)))
    print("\nWrote:")
    for p in (behavior_path, stim_path, pulse_path):
        print("  {}".format(p))


if __name__ == "__main__":
    main()
