"""Stytra glue: a tail-bend estimator and a stimulus that drives the LabJack.

These classes depend on stytra + PyQt5 and therefore only import cleanly inside
the stytra environment (Python 3.7-3.9). The hardware-independent decision logic
lives in ``bend_trigger`` and ``labjack_device`` so it can be tested separately.

Data flow (all in the main GUI process, once per display frame):

    camera frame  -> tracking process -> acc_tracking (tail_sum per frame)
    RightBendEstimator.get_tail_sum()  reads the latest raw tail_sum (radians)
    RightBendTriggerStimulus.update()  applies the threshold, and
        * drives the LabJack output (fixed-width pulse or level)
        * flips the white circle on the stimulus display
        * logs tail_sum / triggered / output_v / pulse every frame (stimulus log)
"""

import math
from collections import namedtuple

from PyQt5.QtCore import Qt, QRect, QPointF, QTimer
from PyQt5.QtGui import QBrush, QColor

from stytra.stimulation.estimators import Estimator
from stytra.stimulation.stimuli import DynamicStimulus
from stytra.stimulation.stimuli.visual import VisualStimulus

from bend_trigger import (
    RightBendDetector,
    FixedWidthPulseController,
    PredictiveBendController,
)


class RightBendEstimator(Estimator):
    """Reports the current raw ``tail_sum`` (radians) from the tracking accumulator.

    This is the same value shown in the stytra plot; it is also written to the
    estimator log so the trace is saved and plotted alongside the tracking.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._output_type = namedtuple("b", "tail_sum")

    def get_tail_sum(self):
        """Return the latest raw tail_sum in radians (nan if no data)."""
        if not self.acc_tracking.stored_data:
            return float("nan")

        last = self.acc_tracking.stored_data[-1]
        tail_sum = getattr(last, "tail_sum", float("nan"))
        if tail_sum != tail_sum:  # nan guard (tracking lost)
            return float("nan")

        t = self.acc_tracking.times[-1]
        if len(self.log.times) == 0 or self.log.times[-1] < t:
            self.log.update_list(t, self._output_type(tail_sum))
        return tail_sum


class RightBendTriggerStimulus(VisualStimulus, DynamicStimulus):
    """Full-session stimulus: emit a pulse + white circle on a right tail bend.

    Three output modes:

    * ``pulse_mode="fixed"`` (default) - emit one ``pulse_width_s`` pulse on the
      rising edge of each right bend, rate-limited by ``refractory_s``. The
      circle stays on for ``circle_display_s`` so a sub-frame pulse is visible.
    * ``pulse_mode="level"`` - drive the output high for as long as the tail is
      bent past threshold (circle follows the level).
    * ``pulse_mode="predictive"`` - anticipatory: detect the OPPOSITE-side bend
      (past ``opposite_threshold``) and fire a ``pulse_width_s`` pulse as the
      tail swings back up through ``fire_level`` (default 0 = neutral), i.e.
      BEFORE it bends to the trigger side. Cancels the delay/jitter of waiting
      for a fast beat to reach the trigger-side threshold. See
      :class:`PredictiveBendController`.

    Parameters
    ----------
    labjack : LabJackU3Output
        Output device (real or simulated).
    threshold : float
        tail_sum threshold in radians (default 1.0). Used by fixed/level modes.
    direction : int
        +1 or -1, sign convention for "right" (see RightBendDetector).
    pulse_mode : {"fixed", "level", "predictive"}
    pulse_width_s : float
        Electrical pulse width for fixed/predictive modes (default 0.005 = 5 ms).
    refractory_s : float
        Minimum time between fixed-mode pulses (default 0.1 = 100 ms).
    opposite_threshold : float
        (predictive) opposite-side excursion (radians) needed to arm a beat.
    fire_level : float
        (predictive) oriented tail_sum level whose upward crossing fires; 0 =
        neutral, negative = fire earlier (still on the opposite side).
    circle_display_s : float
        How long to keep the circle on after a fixed-mode pulse (default 0.1 s).
    circle_radius : float
        Radius of the feedback circle, as a fraction of screen width.
    circle_color, background_color : (int, int, int)
        Circle and background RGB colours.
    """

    def __init__(
        self,
        *args,
        labjack=None,
        threshold=1.0,
        direction=1,
        pulse_mode="fixed",
        pulse_width_s=0.005,
        refractory_s=0.1,
        opposite_threshold=1.0,
        fire_level=0.0,
        circle_display_s=0.1,
        circle_radius=0.15,
        circle_color=(255, 255, 255),
        background_color=(0, 0, 0),
        verbose=True,
        **kwargs
    ):
        super().__init__(
            *args,
            dynamic_parameters=["tail_sum", "triggered", "output_v", "pulse"],
            **kwargs
        )
        # Private (leading underscore) so they are excluded from get_state()
        # static logging / JSON serialisation of the stimulus.
        self._labjack = labjack
        self._detector = RightBendDetector(threshold=threshold, direction=direction)
        self._pulser = FixedWidthPulseController(
            pulse_width_s=pulse_width_s, refractory_s=refractory_s
        )
        # Anticipatory ("predictive") controller: fires as the tail swings back
        # from the opposite side through fire_level, before it bends to the
        # trigger side. Uses a short refractory so it can still keep up per beat.
        self._predictor = PredictiveBendController(
            opposite_threshold=opposite_threshold,
            fire_level=fire_level,
            refractory_s=min(refractory_s, 0.05),
            pulse_width_s=pulse_width_s,
        )

        # Recorded as static metadata of the stimulus:
        self.threshold = float(threshold)
        self.direction = 1 if direction >= 0 else -1
        self.pulse_mode = pulse_mode
        self.pulse_width_s = float(pulse_width_s)
        self.refractory_s = float(refractory_s)
        self.opposite_threshold = float(opposite_threshold)
        self.fire_level = float(fire_level)
        self.circle_display_s = float(circle_display_s)
        self.circle_radius = circle_radius
        self.circle_color = circle_color
        self.background_color = background_color

        self.verbose = verbose
        self._prev_active = False

        # Per-session diagnostics (reset in start()): let you see the threshold
        # that is actually in force and the tail_sum range it is applied to.
        self._peak_signed = float("-inf")
        self._n_over = 0
        self._n_frames = 0

        # Dynamic (per-frame logged) state:
        self.tail_sum = 0.0
        self.triggered = 0   # circle / output-active state
        self.output_v = 0.0
        self.pulse = 0       # 1 only on the exact frame a pulse fires

        self.name = "right_bend_trigger"

    @property
    def _nominal_v(self):
        return self._labjack.voltage if self._labjack is not None else 3.3

    def _emit_pulse(self):
        """Fixed-width pulse that neither blocks the event loop nor lets the PC
        time the width.

        ``pulse_async`` hands the whole pulse to the LabJack as a single
        Feedback command, so the U3's firmware times the width (128 us
        resolution) and a level-sensitive load (LED driver) sees a constant
        pulse. PC-side timing jitters by several ms because the two edges are
        separate USB transactions. The QTimer branch is only a fallback for an
        older ``labjack_device.py`` without ``pulse_async``.
        """
        if self._labjack is None:
            return
        emit = getattr(self._labjack, "pulse_async", None)
        if emit is not None:
            emit(self.pulse_width_s)
            return

        self._labjack.set_high()
        ms = int(round(self.pulse_width_s * 1000))
        try:
            QTimer.singleShot(ms, Qt.PreciseTimer, self._labjack.set_low)
        except TypeError:  # PyQt build without the timer-type overload
            QTimer.singleShot(ms, self._labjack.set_low)

    def start(self):
        super().start()
        self._pulser.reset()
        self._predictor.reset()
        self._prev_active = False
        self._peak_signed = float("-inf")
        self._n_over = 0
        self._n_frames = 0
        if self._labjack is not None:
            self._labjack.set_low()
        if self.verbose:
            # Report the values the CONTROLLER is actually using (not just what
            # was passed in) so a config-restore / copy issue would be visible.
            if self.pulse_mode == "predictive":
                print(
                    "[trigger] session start: mode=predictive direction={:+d}  "
                    "arm when oriented tail_sum <= {:+.3f}, fire on up-crossing "
                    "of {:+.3f} (0 = neutral; negative = earlier)".format(
                        self._detector.direction,
                        -self._predictor.opposite_threshold,
                        self._predictor.fire_level,
                    )
                )
            else:
                print(
                    "[trigger] session start: mode={} threshold={:+.3f} rad "
                    "direction={:+d}  (fires when direction*tail_sum > threshold)".format(
                        self.pulse_mode,
                        self._detector.threshold,
                        self._detector.direction,
                    )
                )

    def update(self):
        super().update()
        val = self._experiment.estimator.get_tail_sum()  # radians, may be nan
        self.tail_sum = 0.0 if val != val else float(val)
        over = self._detector.is_triggered(val)
        self.pulse = 0

        # Track the tail_sum range this threshold is applied to (oriented so
        # positive == the trigger direction) for the end-of-session summary.
        signed = self._detector.signed(self.tail_sum)
        self._n_frames += 1
        if over:
            self._n_over += 1
        if signed > self._peak_signed:
            self._peak_signed = signed

        if self.pulse_mode == "level":
            # Output follows the bend directly: high while over threshold, low
            # otherwise -> turns on/off as fast as this update loop runs, so it
            # can track a fast (e.g. 12 Hz) tail beat with no refractory delay.
            active = over
            if self._labjack is not None:
                self._labjack.set_high() if over else self._labjack.set_low()
            self.output_v = self._nominal_v if over else 0.0
        elif self.pulse_mode == "predictive":
            # Anticipatory: fire as the tail swings back from the opposite side
            # up through fire_level (default neutral), i.e. BEFORE it bends to
            # the trigger side - cancels the delay/jitter of a fast beat. Pass
            # the oriented value (nan when tracking is lost, so the state holds).
            signed_val = self._detector.signed(val)
            fired = self._predictor.step(self._elapsed, signed_val)
            if fired:
                self.pulse = 1
                self._emit_pulse()  # non-blocking
            active = self._predictor.is_pulse_active(
                self._elapsed, window_s=self.circle_display_s
            )
            self.output_v = self._nominal_v if fired else 0.0
        else:
            # "fixed": one pulse per rising edge of the right-bend, rate-limited.
            fired = self._pulser.step(self._elapsed, over)
            if fired:
                self.pulse = 1
                self._emit_pulse()  # non-blocking
            active = self._pulser.is_pulse_active(
                self._elapsed, window_s=self.circle_display_s
            )
            self.output_v = self._nominal_v if fired else 0.0

        self.triggered = 1 if active else 0

        # Live terminal feedback on every on/off transition (independent of the
        # GUI plot, which stytra freezes while a protocol is running).
        if self.verbose and active != self._prev_active:
            print("[trigger] t={:7.3f}s  tail_sum={:+.3f}  ->  {}".format(
                self._elapsed, self.tail_sum, "ON " if active else "off"))
        self._prev_active = active

    def stop(self):
        super().stop()
        if self._labjack is not None:
            self._labjack.set_low()
        if self.verbose and self._n_frames:
            peak = self._peak_signed if self._n_frames else float("nan")
            pct = 100.0 * self._n_over / self._n_frames
            print(
                "[trigger] session end: peak oriented tail_sum={:+.3f} rad | "
                "{}/{} frames over threshold {:+.3f} ({:.1f}%)\n"
                "          -> raise threshold above the peak to see fewer/no "
                "triggers; lower it to see more.".format(
                    peak, self._n_over, self._n_frames, self._detector.threshold, pct
                )
            )

    def paint(self, p, w, h):
        # Background
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(*self.background_color)))
        self.clip(p, w, h)
        p.drawRect(QRect(-1, -1, w + 2, h + 2))

        # White feedback circle only while the output is high
        if self.triggered:
            p.setBrush(QBrush(QColor(*self.circle_color)))
            p.drawEllipse(
                QPointF(0.5 * w, 0.5 * h),
                self.circle_radius * w,
                self.circle_radius * w,
            )
