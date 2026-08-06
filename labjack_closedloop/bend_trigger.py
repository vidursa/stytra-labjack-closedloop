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
from collections import deque


def tail_sum_to_degrees(tail_sum_rad):
    """Convert a raw ``tail_sum`` (radians) into a signed bend angle in degrees."""
    return math.degrees(tail_sum_rad)


def _is_nan(x):
    return x is None or x != x


class BaselineTracker:
    """Estimate the tail's *resting* ``tail_sum`` so the threshold sits symmetrically.

    Tail tracking often rests at a non-zero ``tail_sum`` (e.g. 0.17 rad) depending
    on how the start point / tip are placed, which makes one side closer to the
    threshold than the other. This estimates that resting offset ``b`` so the
    detector can test the *re-zeroed* value ``tail_sum - b``: both sides are then
    equally far from rest before the threshold is applied.

    Modes
    -----
    ``"off"``
        No correction; the baseline is always 0 (identical to before).
    ``"fixed"``
        Constant, user-supplied ``offset`` (read it straight off a session plot).
    ``"start"``
        Average the first ``window_s`` seconds of tracked values (the fish is
        assumed to be at rest just after you press record), then hold that offset
        for the rest of the session. While still collecting it returns the
        running mean so far, so early frames are already roughly corrected.
    ``"running"``
        Moving average over the last ``window_s`` seconds, updated every frame:
        slow drift is tracked while the fast (~12 Hz) beats average out. Note it
        will slowly absorb a *sustained* one-sided hold, by design ("re-zero the
        default position").

    ``update(t, raw)`` returns the current baseline. ``nan`` inputs (tracking
    lost) hold the current estimate without disturbing it.

    Parameters
    ----------
    mode : {"off", "fixed", "start", "running"}
    offset : float
        The constant offset for ``"fixed"`` mode (radians).
    window_s : float
        Averaging window for ``"start"`` / ``"running"`` modes (seconds).
    """

    def __init__(self, mode="off", offset=0.0, window_s=2.0):
        self.mode = mode
        self.offset = float(offset)
        self.window_s = float(window_s)
        self.reset()

    def reset(self):
        self._samples = deque()  # (t, value) for the windowed modes
        self._sum = 0.0
        self._value = self.offset if self.mode == "fixed" else 0.0
        self._frozen = False  # "start": True once the window has been averaged

    @property
    def value(self):
        """The current baseline estimate (radians)."""
        return self._value

    @property
    def frozen(self):
        """True once a ``"start"`` baseline has been locked in."""
        return self._frozen

    def update(self, t, raw):
        """Advance one frame with the raw ``tail_sum`` and return the baseline."""
        if self.mode == "off":
            return 0.0
        if self.mode == "fixed":
            return self.offset
        if raw is None or raw != raw:  # nan: hold the current estimate
            return self._value

        if self.mode == "start":
            if not self._frozen:
                self._samples.append((t, raw))
                self._sum += raw
                self._value = self._sum / len(self._samples)  # provisional mean
                if (t - self._samples[0][0]) >= self.window_s:
                    self._frozen = True  # lock the offset for the rest of the run
            return self._value

        # "running": moving average over the last window_s (O(1) amortised).
        self._samples.append((t, raw))
        self._sum += raw
        while self._samples and (t - self._samples[0][0]) > self.window_s:
            _, old = self._samples.popleft()
            self._sum -= old
        self._value = self._sum / len(self._samples) if self._samples else 0.0
        return self._value


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


class DelayLine:
    """Postpone fire events by a fixed time, on the controller's own clock.

    A detector/controller decides *when the trigger condition is met*; this
    postpones the actual pulse by ``delay_s`` so the stimulation lands a chosen
    time after detection (e.g. a fixed offset into a later phase of the tail-beat
    cycle). Detection and emission share the same monotonic ``t`` (the stimulus
    ``_elapsed`` clock), so the delayed pulse is logged on the same time axis as
    the tail trace and everything else.

    Usage each frame::

        if controller_fired:
            delay.push(t)          # schedule a pulse for t + delay_s
        if delay.pop_due(t):       # has any scheduled pulse's time arrived?
            emit_pulse()

    With ``delay_s == 0`` a pushed pulse is immediately due on the same frame, so
    the behaviour is identical to firing on detection (backwards compatible).

    A constant delay preserves the spacing of detections, so scheduled pulses
    never bunch up: at most one comes due per frame.

    Parameters
    ----------
    delay_s : float
        Seconds to wait between detection and the pulse (>= 0).
    """

    def __init__(self, delay_s=0.0):
        self.delay_s = max(0.0, float(delay_s))
        self._pending = []  # emit times, ascending

    def reset(self):
        self._pending = []

    @property
    def pending(self):
        """How many scheduled pulses have not yet come due."""
        return len(self._pending)

    def push(self, t):
        """Schedule a pulse for ``t + delay_s``."""
        self._pending.append(float(t) + self.delay_s)

    def pop_due(self, t):
        """Remove and count the scheduled pulses whose time has arrived by ``t``."""
        n = 0
        while self._pending and self._pending[0] <= t:
            self._pending.pop(0)
            n += 1
        return n


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
