"""Offline unit tests for the hardware-independent parts of the closed loop.

Runs on a plain Python interpreter (no stytra / PyQt5 / numpy needed):

    python test_offline_logic.py

It exercises the bend detector, the simulated LabJack output and a synthetic
"replay" of the closed loop over a made-up tail_sum trace, checking that the
number and timing of 5 V pulses match what we expect.
"""

import csv
import math
import os
import tempfile

from bend_trigger import (
    RightBendDetector,
    FixedWidthPulseController,
    PredictiveBendController,
    DelayLine,
    BaselineTracker,
)
from labjack_device import LabJackU3Output


def test_detector_threshold_and_direction():
    det = RightBendDetector(threshold=1.0, direction=1)
    # tail_sum 0.5 right -> below threshold
    assert not det.is_triggered(0.5)
    # tail_sum 1.5 right -> triggers
    assert det.is_triggered(1.5)
    # tail_sum 1.5 to the LEFT (negative) -> must NOT trigger for a "right" detector
    assert not det.is_triggered(-1.5)
    # nan (tracking lost) -> no trigger
    assert not det.is_triggered(float("nan"))

    # Flipped convention: left bends now count as "right"
    det_flip = RightBendDetector(threshold=1.0, direction=-1)
    assert det_flip.is_triggered(-1.5)
    assert not det_flip.is_triggered(1.5)
    print("ok  detector threshold + direction")


def test_labjack_sim_edges_and_log():
    lj = LabJackU3Output(backend="simulate", channel="DAC0", voltage=5.0, verbose=False)

    # idempotent: repeated highs/lows collapse to single edges
    lj.set_low()          # already low -> no event
    lj.set_high()         # edge up
    lj.set_high()         # no-op
    lj.set_low()          # edge down
    lj.set_high()         # edge up
    lj.close()            # forces final low -> edge down

    states = [e["state"] for e in lj.events]
    assert states == [1, 0, 1, 0], states
    assert lj.n_pulses == 2, lj.n_pulses

    # voltages: high events carry 5 V, low events 0 V
    for e in lj.events:
        assert e["voltage"] == (5.0 if e["state"] == 1 else 0.0)

    # CSV round-trips
    path = os.path.join(tempfile.gettempdir(), "labjack_test.csv")
    lj.save_log(path)
    with open(path) as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 4
    assert rows[0]["state"] == "1"
    print("ok  labjack simulate edges + csv log")


def test_pulse_async_width_is_exact():
    # pulse_async() is what fixed/predictive modes use. In simulate mode it
    # logs the ideal edges; the recorded width must equal the requested one
    # (on real hardware the U3 firmware enforces the same width).
    lj = LabJackU3Output(backend="simulate", channel="FIO4", voltage=3.3, verbose=False)
    for _ in range(5):
        lj.pulse_async(0.005)
    lj.close()

    assert [e["state"] for e in lj.events] == [1, 0] * 5
    assert lj.n_pulses == 5, lj.n_pulses
    for hi, lo in zip(lj.events[0::2], lj.events[1::2]):
        assert abs((lo["t"] - hi["t"]) - 0.005) < 1e-9, (hi, lo)
        assert hi["voltage"] == 3.3 and lo["voltage"] == 0.0
    print("ok  pulse_async: constant width, one high/low pair per pulse")


def _replay(tail_sums, threshold=1.0, direction=1):
    """Mimic RightBendTriggerStimulus.update() over a trace of raw tail_sum values.

    Returns (labjack, per_frame_triggered).
    """
    det = RightBendDetector(threshold=threshold, direction=direction)
    lj = LabJackU3Output(backend="simulate", voltage=5.0, verbose=False)
    triggered_trace = []
    for ts in tail_sums:
        triggered = det.is_triggered(ts)
        if triggered:
            lj.set_high()
        else:
            lj.set_low()
        triggered_trace.append(1 if triggered else 0)
    lj.close()
    return lj, triggered_trace


def test_closed_loop_replay():
    # Two separate right-bend "bouts" (tail_sum > 1.0) separated by rest / left bends.
    trace = [
        0.0, 0.5, 1.2, 1.8, 1.4,   # bout 1 (right)
        0.3, -1.6, 0.0,            # rest + a big LEFT bend (ignored)
        1.1, 2.0,                  # bout 2 (right)
        0.2,
    ]
    lj, trig = _replay(trace)
    # Expected per-frame trigger pattern:
    assert trig == [0, 0, 1, 1, 1, 0, 0, 0, 1, 1, 0], trig
    # -> exactly two rising edges (two pulses)
    assert lj.n_pulses == 2, lj.n_pulses
    print("ok  closed-loop replay: 2 right-bend bouts -> 2 pulses")

    # A trace that never crosses threshold -> no pulses
    lj2, _ = _replay([0.5, -0.5, 0.9, -0.9])
    assert lj2.n_pulses == 0
    print("ok  sub-threshold trace -> 0 pulses")


def test_pulse_controller_one_pulse_per_sustained_bend():
    # 50 Hz frames; tail held over threshold for 20 frames (0.4 s).
    pc = FixedWidthPulseController(pulse_width_s=0.005, refractory_s=0.1)
    dt = 0.02
    onsets = [i * dt for i in range(30) if pc.step(i * dt, over=(5 <= i < 25))]
    # A single sustained crossing -> exactly one pulse, at the rising-edge frame.
    assert len(onsets) == 1, onsets
    assert abs(onsets[0] - 5 * dt) < 1e-9
    print("ok  pulse controller: sustained bend -> single pulse")


def test_pulse_controller_refractory():
    # Tail rapidly toggles over/under threshold every frame at 200 Hz (dt=5 ms).
    pc = FixedWidthPulseController(pulse_width_s=0.005, refractory_s=0.1)
    dt = 0.005
    onsets = [i * dt for i in range(200) if pc.step(i * dt, over=(i % 2 == 0))]
    # Rising edges happen every 10 ms, but 100 ms refractory caps the rate:
    # onsets must be >= refractory apart.
    for a, b in zip(onsets, onsets[1:]):
        assert (b - a) >= 0.1 - 1e-9, (a, b)
    # ~1 s of toggling / 100 ms refractory -> about 10 pulses, not 100.
    assert 8 <= len(onsets) <= 11, len(onsets)
    print("ok  pulse controller: refractory rate-limits fast re-crossings")


def test_pulse_controller_display_window():
    pc = FixedWidthPulseController(pulse_width_s=0.005, refractory_s=0.1)
    assert pc.step(1.0, over=True) is True
    # circle window (0.1 s) keeps 'active' true well after the 5 ms pulse
    assert pc.is_pulse_active(1.02, window_s=0.1)
    assert not pc.is_pulse_active(1.2, window_s=0.1)
    print("ok  pulse controller: circle display window")


def test_delay_line_zero_is_immediate():
    # delay 0: a pushed pulse is due on the same frame -> identical to firing
    # on detection (backwards compatible).
    dl = DelayLine(0.0)
    dl.push(1.0)
    assert dl.pop_due(1.0) == 1
    assert dl.pending == 0
    print("ok  delay line: zero delay fires immediately")


def test_delay_line_postpones_by_fixed_time():
    # A detection at t=1.000 with a 50 ms delay must not emit until t>=1.050,
    # and then exactly once.
    dl = DelayLine(0.050)
    dl.push(1.000)
    dt = 0.005
    fired_times = []
    for i in range(int(round(1.0 / dt)) + 1):
        t = 1.000 + i * dt
        if dl.pop_due(t):
            fired_times.append(t)
    assert len(fired_times) == 1, fired_times
    assert abs(fired_times[0] - 1.050) < 1e-9, fired_times
    print("ok  delay line: postpones a pulse by the fixed delay, once")


def test_delay_line_preserves_spacing_no_bunching():
    # Constant delay preserves detection spacing: at most one pulse comes due per
    # frame, so scheduled pulses never bunch up.
    dl = DelayLine(0.030)
    dt = 0.005
    detections = {0.010, 0.150, 0.290}  # well separated
    fired = 0
    max_per_frame = 0
    t = 0.0
    for _ in range(120):
        # push any detection that occurs on this frame
        for d in list(detections):
            if abs(d - t) < 1e-9:
                dl.push(d)
        n = dl.pop_due(t)
        max_per_frame = max(max_per_frame, n)
        fired += n
        t += dt
    assert fired == 3, fired
    assert max_per_frame == 1, max_per_frame
    print("ok  delay line: constant delay preserves spacing (no bunching)")


def test_fixed_mode_with_delay_shifts_pulse():
    # Full fixed-mode replay: detection on the rising edge, but the emitted
    # pulse lands stim_delay_s later. Emulates the stimulus update() loop.
    pc = FixedWidthPulseController(pulse_width_s=0.005, refractory_s=0.1)
    dl = DelayLine(0.040)
    dt = 0.005
    detect_t, emit_t = None, None
    for i in range(60):
        t = i * dt
        over = 5 <= i < 25  # sustained bend, rising edge at i=5 (t=0.025)
        if pc.step(t, over):
            detect_t = t
            dl.push(t)
        if dl.pop_due(t) and emit_t is None:
            emit_t = t
    assert abs(detect_t - 0.025) < 1e-9, detect_t
    assert abs(emit_t - 0.065) < 1e-9, emit_t     # 0.025 + 0.040
    assert abs((emit_t - detect_t) - 0.040) < 1e-9
    print("ok  fixed mode + delay: pulse emitted stim_delay_s after detection")


def test_baseline_off_and_fixed():
    off = BaselineTracker(mode="off")
    assert off.update(0.0, 0.17) == 0.0  # no correction
    fixed = BaselineTracker(mode="fixed", offset=0.17)
    assert fixed.update(0.0, 5.0) == 0.17  # constant, ignores input
    assert fixed.update(1.0, -3.0) == 0.17
    print("ok  baseline: off = 0, fixed = constant offset")


def test_baseline_makes_threshold_symmetric():
    # A trace resting at 0.17 that beats +-0.9 about rest. With a right-detector
    # at threshold 0.8 and NO correction, the +0.17 bias makes the right side
    # trigger while the equal-magnitude left side would not: asymmetric.
    det = RightBendDetector(threshold=0.8, direction=1)
    rest = 0.17
    right_peak = rest + 0.9   # 1.07
    left_peak = rest - 0.9    # -0.73
    # raw (no baseline): right triggers, left (mirror) does not -> asymmetric
    assert det.is_triggered(right_peak) and not det.is_triggered(-right_peak)
    # with the baseline removed, the SAME excursion each way is symmetric:
    bl = BaselineTracker(mode="fixed", offset=rest)
    b = bl.update(0.0, rest)
    assert det.is_triggered(right_peak - b)          # +0.9 -> triggers
    assert not det.is_triggered(left_peak - b)       # -0.9 -> does not
    # a mirror-image right bend of the same size past rest also triggers,
    # confirming both sides are now measured from rest, not from 0.
    assert det.is_triggered((rest + 0.9) - b)
    print("ok  baseline: re-zeroing makes both sides equidistant from rest")


def test_baseline_start_freezes_after_window():
    bl = BaselineTracker(mode="start", window_s=0.1)
    dt = 0.01
    # first 0.1 s rests at 0.20 -> baseline converges to ~0.20 and freezes
    for i in range(11):
        bl.update(i * dt, 0.20)
    assert bl.frozen, "start baseline should freeze after the window"
    assert abs(bl.value - 0.20) < 1e-6, bl.value
    # afterwards the fish beats hard; the frozen baseline must NOT move
    for i in range(11, 60):
        bl.update(i * dt, 0.20 + math.sin(i))
    assert abs(bl.value - 0.20) < 1e-6, bl.value
    print("ok  baseline: start mode freezes the resting offset after the window")


def test_baseline_running_tracks_drift_not_beats():
    # Slow drift of the resting point from 0.0 to 0.30 with a fast beat on top.
    # A running mean over ~5 beats should sit near the drifting rest, NOT the beat.
    bl = BaselineTracker(mode="running", window_s=0.5)
    fps, freq = 200.0, 12.0
    n = int(2.0 * fps)
    last = None
    for i in range(n):
        t = i / fps
        drift = 0.30 * (t / 2.0)                     # 0 -> 0.30 over 2 s
        beat = 0.9 * math.sin(2 * math.pi * freq * t)
        last = bl.update(t, drift + beat)
    # near the end the rest is ~0.30; the running baseline should be close to it
    # and far from the instantaneous beat amplitude.
    assert abs(last - 0.30) < 0.08, last
    # nan holds the estimate
    held = bl.update(2.0, float("nan"))
    assert held == last
    print("ok  baseline: running mode follows slow drift, averages out beats")


def _sine_beats(n_beats=5, amp=1.2, fps=200.0, freq=12.0, direction=1):
    """A clean oscillatory tail_sum trace: direction*amp*sin(2*pi*freq*t)."""
    n = int(round(n_beats / freq * fps))
    out = []
    for i in range(n):
        t = i / fps
        out.append((t, direction * amp * math.sin(2 * math.pi * freq * t)))
    return out


def test_predictive_fires_before_trigger_side():
    # Oscillatory beat; predictive should fire near the upward zero-crossing,
    # BEFORE the tail reaches the trigger (positive) side.
    ctrl = PredictiveBendController(
        opposite_threshold=1.0, fire_level=0.0, refractory_s=0.02, pulse_width_s=0.005
    )
    trace = _sine_beats(n_beats=5, amp=1.2, freq=12.0)
    fire_vals = [s for (t, s) in trace if ctrl.step(t, s)]
    # Fires once per beat (5 beats -> ~5 fires; boundaries may drop one).
    assert 4 <= len(fire_vals) <= 5, len(fire_vals)
    # Each fire happens at/just above neutral (>=0), and well before the trigger-
    # side peak (1.2). At 200 fps a 12 Hz beat steps ~0.45/sample near the zero
    # crossing, so the first post-crossing sample can sit up to one step above 0.
    for v in fire_vals:
        assert -1e-9 <= v < 0.6, v
    print("ok  predictive: fires ~once per beat, near the neutral up-crossing")


def test_predictive_fire_level_shifts_timing():
    # A negative fire_level should fire EARLIER (at a more-negative value) than
    # firing at neutral -> anticipates even more.
    trace = _sine_beats(n_beats=3, amp=1.2, freq=12.0)

    ctrl0 = PredictiveBendController(opposite_threshold=1.0, fire_level=0.0, refractory_s=0.02)
    neutral_fires = [(t, s) for (t, s) in trace if ctrl0.step(t, s)]

    ctrlneg = PredictiveBendController(opposite_threshold=1.0, fire_level=-0.5, refractory_s=0.02)
    early_fires = [(t, s) for (t, s) in trace if ctrlneg.step(t, s)]

    assert neutral_fires and early_fires
    # Compare the first beat's fire time: earlier fire_level -> earlier (smaller) t.
    assert early_fires[0][0] < neutral_fires[0][0], (early_fires[0], neutral_fires[0])
    # and it fires while still on the opposite side (value < 0).
    assert early_fires[0][1] < 0.0
    print("ok  predictive: negative fire_level fires earlier on the return stroke")


def test_predictive_needs_opposite_excursion():
    # If the tail never bends far enough to the opposite side, it never arms.
    ctrl = PredictiveBendController(opposite_threshold=1.0, fire_level=0.0)
    # small wobble around neutral, never <= -1.0
    fires = 0
    for i in range(400):
        t = i * 0.005
        s = 0.4 * math.sin(2 * math.pi * 12.0 * t)  # amplitude 0.4 only
        if ctrl.step(t, s):
            fires += 1
    assert fires == 0, fires
    # nan frames hold state without firing
    ctrl.reset()
    assert ctrl.step(0.0, float("nan")) is False
    print("ok  predictive: no arm without a real opposite bend; nan-safe")


if __name__ == "__main__":
    test_detector_threshold_and_direction()
    test_labjack_sim_edges_and_log()
    test_pulse_async_width_is_exact()
    test_closed_loop_replay()
    test_pulse_controller_one_pulse_per_sustained_bend()
    test_pulse_controller_refractory()
    test_pulse_controller_display_window()
    test_predictive_fires_before_trigger_side()
    test_predictive_fire_level_shifts_timing()
    test_predictive_needs_opposite_excursion()
    test_delay_line_zero_is_immediate()
    test_delay_line_postpones_by_fixed_time()
    test_delay_line_preserves_spacing_no_bunching()
    test_fixed_mode_with_delay_shifts_pulse()
    test_baseline_off_and_fixed()
    test_baseline_makes_threshold_symmetric()
    test_baseline_start_freezes_after_window()
    test_baseline_running_tracks_drift_not_beats()
    print("\nAll offline logic tests passed.")
