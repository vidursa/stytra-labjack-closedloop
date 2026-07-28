"""Pure decision logic for the closed-loop tail-bend trigger.

This module deliberately imports nothing from stytra / PyQt5 / numpy so that the
core decision can be unit-tested in isolation (see test_offline_logic.py) on any
plain Python interpreter, and reused from the stytra integration layer.

The tracking pipeline outputs a scalar ``tail_sum`` for every processed frame.
In stytra ``tail_sum`` is a cumulative curvature measure (in radians):

    tail_sum = theta[-1] + theta[-2] - theta[0] - theta[1]

i.e. how much the tip half of the tail is rotated relative to the base half.
Its sign encodes the direction of the bend (left vs. right). We treat this value
(converted to degrees) as the "bend angle" that the threshold is applied to.
"""

import math


def tail_sum_to_degrees(tail_sum_rad):
    """Convert a raw ``tail_sum`` (radians) into a signed bend angle in degrees."""
    return math.degrees(tail_sum_rad)


def _is_nan(x):
    return x is None or x != x


class RightBendDetector:
    """Decide whether the tail is bent past a threshold *to the right*.

    Operates directly on the raw ``tail_sum`` value (radians) that the tracking
    pipeline outputs and that you see in the stytra plot.

    Parameters
    ----------
    threshold : float
        Minimum ``tail_sum`` required to trigger. Default 1.0 (radians).
    direction : int
        Sign convention that maps ``tail_sum`` to a "rightward" bend.
        With ``direction = +1`` a positive ``tail_sum`` (> +threshold) is a bend
        to the right; use ``direction = -1`` if, when you watch the recording,
        the circle lights up on *left* bends instead (the physical sign depends
        on camera orientation / rotation, so calibrate it visually).
    """

    def __init__(self, threshold=1.0, direction=1):
        self.threshold = float(threshold)
        self.direction = 1 if direction >= 0 else -1

    def signed(self, value):
        """tail_sum re-oriented so that positive == 'to the right'."""
        return self.direction * value

    def is_triggered(self, value):
        """True when the (oriented) tail_sum exceeds the threshold to the right."""
        if _is_nan(value):
            return False
        return self.signed(value) > self.threshold


class FixedWidthPulseController:
    """Turn a stream of over-threshold booleans into fixed-width pulse onsets.

    A pulse is emitted on the *rising edge* (the frame the bend first crosses the
    threshold), provided at least ``refractory_s`` has elapsed since the previous
    pulse onset. This gives one clean pulse per bend event instead of a pulse for
    every frame the tail stays bent, and rate-limits fast re-crossings.

    The controller is time-driven (you pass it the current time each step), so it
    works identically whether ``t`` comes from the stimulus clock or a simulated
    frame clock.

    Parameters
    ----------
    pulse_width_s : float
        Electrical pulse width in seconds (default 0.005 = 5 ms).
    refractory_s : float
        Minimum time between successive pulse onsets (default 0.1 = 100 ms).
    """

    def __init__(self, pulse_width_s=0.005, refractory_s=0.1):
        self.pulse_width_s = float(pulse_width_s)
        self.refractory_s = float(refractory_s)
        self.last_onset = None
        self._was_over = False

    def reset(self):
        self.last_onset = None
        self._was_over = False

    def step(self, t, over):
        """Advance one frame. Return True if a new pulse should be emitted now.

        Parameters
        ----------
        t : float
            Current time (seconds). Must be non-decreasing across calls.
        over : bool
            Whether the bend is currently past threshold to the right.
        """
        rising = over and not self._was_over
        self._was_over = over
        if rising and (
            self.last_onset is None or (t - self.last_onset) >= self.refractory_s
        ):
            self.last_onset = t
            return True
        return False

    def is_pulse_active(self, t, window_s=None):
        """Whether ``t`` falls within ``window_s`` of the last pulse onset.

        Used to decide how long to keep the feedback circle visible; pass a
        ``window_s`` larger than ``pulse_width_s`` so a sub-frame electrical
        pulse is still shown on screen for a perceptible time.
        """
        if self.last_onset is None:
            return False
        window = self.pulse_width_s if window_s is None else window_s
        return (t - self.last_onset) < window


class PredictiveBendController:
    """Fire in *anticipation* of a bend to the trigger side.

    On a fast (~12 Hz) beat, waiting for the tail to cross a threshold on the
    trigger ('right') side stimulates late and jittery - the tail is already
    well into the bend. A tail beat is oscillatory, though: a rightward bend is
    preceded by a leftward peak and a swing back through the neutral position.
    This controller detects that left excursion and fires as the tail swings
    back *up through* a chosen level (default 0 = neutral), i.e. BEFORE it has
    bent to the trigger side.

    It operates on the *oriented* signal ``s = direction * tail_sum`` (so
    positive == trigger side, negative == opposite side), exactly like
    :class:`RightBendDetector.signed`.

    State machine (one pulse per beat cycle)::

        WAIT_OPPOSITE  -- wait until s <= -opposite_threshold (a real beat to
                          the other side) --> ARMED
        ARMED          -- fire the instant s rises up through fire_level, then
                          disarm (back to WAIT_OPPOSITE)

    Because each fire needs a fresh opposite excursion to re-arm, this emits at
    most once per beat; ``refractory_s`` is an extra lower bound between fires.

    Parameters
    ----------
    opposite_threshold : float
        How far past neutral, on the OPPOSITE side, the tail must bend to arm
        (radians, > 0). Larger values ignore small wobbles and arm only on real
        beats.
    fire_level : float
        Oriented ``tail_sum`` level (radians) whose upward crossing fires the
        pulse. ``0.0`` fires at the neutral crossing; **negative** fires EARLIER
        (still on the opposite side); positive fires slightly later (already
        onto the trigger side). This is the "how far after the other-side bend"
        knob. Keep it greater than ``-opposite_threshold``.
    refractory_s : float
        Minimum time between successive fires (seconds).
    pulse_width_s : float
        Fallback window for :meth:`is_pulse_active` (circle visibility).
    """

    _WAIT, _ARMED = 0, 1

    def __init__(
        self,
        opposite_threshold=1.0,
        fire_level=0.0,
        refractory_s=0.05,
        pulse_width_s=0.005,
    ):
        self.opposite_threshold = abs(float(opposite_threshold))
        self.fire_level = float(fire_level)
        self.refractory_s = float(refractory_s)
        self.pulse_width_s = float(pulse_width_s)
        self.last_onset = None
        self._state = self._WAIT
        self._prev = None

    def reset(self):
        self.last_onset = None
        self._state = self._WAIT
        self._prev = None

    @property
    def armed(self):
        return self._state == self._ARMED

    def step(self, t, s):
        """Advance one frame with the oriented value ``s``. Return True to fire.

        Parameters
        ----------
        t : float
            Current time (seconds), non-decreasing across calls.
        s : float
            Oriented tail_sum (``direction * tail_sum``); positive == trigger
            side. ``nan`` holds the current state and never fires.
        """
        if s is None or s != s:  # nan: tracking lost, hold state
            return False
        prev = self._prev
        self._prev = s

        # Arm as soon as the tail has swung far enough to the opposite side.
        if s <= -self.opposite_threshold:
            self._state = self._ARMED

        # Fire on the upward crossing of fire_level while armed.
        if self._state == self._ARMED and prev is not None:
            if prev < self.fire_level <= s:
                if self.last_onset is None or (t - self.last_onset) >= self.refractory_s:
                    self.last_onset = t
                    self._state = self._WAIT
                    return True
        return False

    def is_pulse_active(self, t, window_s=None):
        """Whether ``t`` falls within ``window_s`` of the last fire (circle)."""
        if self.last_onset is None:
            return False
        window = self.pulse_width_s if window_s is None else window_s
        return (t - self.last_onset) < window
