"""Thin wrapper around a LabJack U3 used as a *digital/analog output* device.

Stytra ships a ``U3LabJackPulseTrigger`` that *reads* a TTL line to start a
protocol. Here we need the opposite: stytra decides, in real time, to emit a 5 V
pulse to drive external hardware (e.g. an excitation LED / opto stimulation).

Two backends are provided:

* ``"u3"``      - talks to a real LabJack U3 via the LabJackPython ``u3`` module.
* ``"simulate"`` - no hardware; every state change is recorded and printed. Use
                   this to develop/test against the recorded video.

About the 5 V:
    The U3's *digital* FIO lines are 3.3 V logic. To get a genuine 5 V level we
    drive an analog output (**DAC0** by default, register 5000; DAC1 is 5002),
    which spans ~0-4.95 V. If instead you point ``channel`` at a digital line
    (an int, or "FIO4" etc.) the output will be the 3.3 V logic high.

About pulse width:
    ``pulse_async()`` is what the closed loop uses. On a real U3 driving a
    *digital* line it sends the entire pulse as ONE low-level Feedback command
    (BitStateWrite high -> firmware Wait -> BitStateWrite low), so the width is
    timed by the U3 itself at 128 us resolution rather than by the PC. That
    matters for a level-sensitive load (e.g. an LED driver, where width == light
    dose): PC-timed widths jitter by several ms because the high and low edges
    are two separate USB transactions with independent latency.

Every high/low transition is timestamped and can be written to a CSV via
``save_log()`` so the pulse times line up with the frame/stimulus logs stytra
saves for the same session.
"""

import csv
import datetime
import queue
import threading
import time

_WAIT_UNIT_S = 128e-6  # U3 firmware WaitShort resolution
_MAX_WAIT_CMDS = 20    # keeps the Feedback packet well inside its size limit


def _precise_wait(seconds):
    """Wait ``seconds`` with sub-millisecond accuracy.

    ``time.sleep`` on Windows is only accurate to the system tick (up to
    ~15 ms), which is useless for a 5 ms pulse. Sleep all but the last 1.5 ms,
    then spin. Only ever called on the pulse worker thread, so the spin never
    touches the Qt event loop.
    """
    if seconds <= 0:
        return
    end = time.perf_counter() + seconds
    coarse = seconds - 0.0015
    if coarse > 0:
        time.sleep(coarse)
    while time.perf_counter() < end:
        pass


class LabJackU3Output:
    _DAC_REGISTERS = {"DAC0": 5000, "DAC1": 5002}

    def __init__(self, backend="simulate", channel="DAC0", voltage=5.0, verbose=True):
        """
        Parameters
        ----------
        backend : {"simulate", "u3"}
            "simulate" records events only; "u3" drives real hardware.
        channel : str or int
            "DAC0"/"DAC1" for a true analog 5 V pulse, or a digital line
            (int like ``4`` or string like ``"FIO4"``) for a 3.3 V logic pulse.
        voltage : float
            Voltage applied on the high state (only meaningful for DAC channels;
            digital lines are fixed at logic high).
        verbose : bool
            Print each transition to stdout.
        """
        self.backend = backend
        self.channel = channel
        self.voltage = float(voltage)
        self.verbose = verbose
        self.device = None
        self._u3 = None
        self.state_high = False
        self.events = []  # list of dicts: time, t, state, voltage
        self._t0 = time.monotonic()

        # Pulse worker: a single daemon thread so a blocking, firmware-timed
        # USB transaction never stalls the Qt event loop. Daemon so it can
        # never hold up interpreter exit.
        self._lock = threading.RLock()
        self._queue = None
        self._worker = None
        self._dropped_pulses = 0

        if backend == "u3":
            import u3  # imported lazily so the sim backend needs no hardware libs

            self._u3 = u3
            self.device = u3.U3()
            if not self._is_dac():
                # configure the digital line as output
                self.device.getFeedback(u3.BitDirWrite(self._dio_number(), 1))
            self._write(0.0)  # make sure we start low
        elif backend == "simulate":
            pass
        else:
            raise ValueError("backend must be 'u3' or 'simulate', got %r" % backend)

    def __deepcopy__(self, memo):
        # stytra deepcopies the stimulus list (Protocol._get_stimulus_list).
        # A hardware handle must NOT be copied: keep a single shared device so
        # the copy that actually runs drives the same LabJack we close + log.
        return self

    # -- channel helpers -------------------------------------------------
    def _is_dac(self):
        return isinstance(self.channel, str) and self.channel.upper().startswith("DAC")

    def _dio_number(self):
        if isinstance(self.channel, int):
            return self.channel
        digits = "".join(c for c in str(self.channel) if c.isdigit())
        return int(digits)

    def reassert_output_direction(self):
        """Re-configure the digital line as an output.

        ``configIO()`` (used when setting up analog inputs for TTL streaming)
        resets FIO4-FIO7 to digital *inputs*, silently killing our output. Call
        this straight afterwards. No-op for DAC channels and the sim backend.
        """
        if self.backend != "u3" or self._is_dac():
            return
        with self._lock:
            self.device.getFeedback(self._u3.BitDirWrite(self._dio_number(), 1))
            self.state_high = False

    # -- low level write -------------------------------------------------
    def _write(self, volts):
        if self.backend != "u3":
            return
        with self._lock:  # main thread (level mode) vs pulse worker thread
            if self._is_dac():
                reg = self._DAC_REGISTERS[self.channel.upper()]
                self.device.writeRegister(reg, float(volts))
            else:
                self.device.setDOState(self._dio_number(), 1 if volts > 0 else 0)

    def _record(self, state, volts, t=None):
        """Log a transition. ``t`` overrides "now" for firmware-timed edges,
        whose real times are known from the command's start + the exact width."""
        ev = dict(
            time=datetime.datetime.now().isoformat(),
            t=(time.monotonic() - self._t0) if t is None else float(t),
            state=int(state),
            voltage=float(volts),
        )
        self.events.append(ev)
        if self.verbose:
            print("[LabJack {}] {} ({:.3f} V)".format(
                self.channel, "HIGH" if state else "low", volts))

    # -- public API ------------------------------------------------------
    def set_high(self):
        """Drive the output to ``voltage`` (no-op if already high)."""
        if self.state_high:
            return
        self._write(self.voltage)
        self.state_high = True
        self._record(1, self.voltage)

    def set_low(self):
        """Drive the output to 0 V (no-op if already low)."""
        if not self.state_high:
            return
        self._write(0.0)
        self.state_high = False
        self._record(0, 0.0)

    def pulse(self, width_s=0.005):
        """Emit a single fixed-width pulse (blocking). Not used by the closed
        loop - see :meth:`pulse_async` - but handy for manual bench testing."""
        self.set_high()
        time.sleep(width_s)
        self.set_low()

    # -- constant-width pulses (used by the closed loop) ------------------
    def _wait_commands(self, width_s):
        """U3 firmware Wait commands covering ``width_s``.

        Returns None if the width can't be expressed (WaitShort missing from
        this LabJackPython version, or too long for one Feedback packet), in
        which case the caller falls back to PC timing.
        """
        wait_short = getattr(self._u3, "WaitShort", None)
        if wait_short is None:
            return None
        n = max(1, int(round(width_s / _WAIT_UNIT_S)))
        cmds = []
        while n > 0 and len(cmds) < _MAX_WAIT_CMDS:
            chunk = min(n, 255)
            cmds.append(wait_short(chunk))
            n -= chunk
        return cmds if n == 0 else None

    def _pulse_blocking(self, width_s):
        """Emit one pulse. Blocks - only ever runs on the worker thread."""
        t0 = time.monotonic() - self._t0

        if self.backend == "u3" and not self._is_dac():
            waits = self._wait_commands(width_s)
            if waits is not None:
                # Both edges + the delay in a single USB transaction: the U3
                # firmware times the width, so it is constant to ~128 us.
                dio = self._dio_number()
                cmds = [self._u3.BitStateWrite(dio, 1)]
                cmds.extend(waits)
                cmds.append(self._u3.BitStateWrite(dio, 0))
                with self._lock:
                    self.device.getFeedback(*cmds)
                    self.state_high = False
                self._record(1, self.voltage, t=t0)
                self._record(0, 0.0, t=t0 + width_s)
                return

        # DAC channel, or a width too long for one packet: two writes with a
        # precise wait between them. Still off the event loop, but the width
        # now carries the difference of two USB latencies.
        self._write(self.voltage)
        self.state_high = True
        self._record(1, self.voltage, t=t0)
        _precise_wait(width_s)
        self._write(0.0)
        self.state_high = False
        self._record(0, 0.0)

    def _worker_loop(self):
        while True:
            width_s = self._queue.get()
            if width_s is None:
                return
            try:
                self._pulse_blocking(width_s)
            except Exception as exc:
                print("[LabJack {}] pulse failed: {!r}".format(self.channel, exc))

    def pulse_async(self, width_s=0.005):
        """Emit a fixed-width pulse without blocking the caller.

        On a real U3 driving a digital line the width is timed by the U3's
        firmware (see the module docstring), so a level-sensitive load sees a
        constant-width TTL regardless of what the PC's event loop is doing.
        """
        if self.backend != "u3":
            # Simulated: no hardware to wait for, log the ideal edges directly.
            t0 = time.monotonic() - self._t0
            self._record(1, self.voltage, t=t0)
            self._record(0, 0.0, t=t0 + width_s)
            return

        if self._worker is None:
            self._queue = queue.Queue()
            self._worker = threading.Thread(
                target=self._worker_loop, name="labjack-pulse", daemon=True
            )
            self._worker.start()

        # Backlog means the loop is asking for pulses faster than USB can send
        # them. Drop rather than queue a pulse that would land at the wrong time.
        if self._queue.qsize() > 4:
            self._dropped_pulses += 1
            return
        self._queue.put(float(width_s))

    def save_log(self, path):
        """Write the recorded high/low transitions to ``path`` as CSV."""
        with open(str(path), "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=["time", "t", "state", "voltage"])
            writer.writeheader()
            for ev in self.events:
                writer.writerow(ev)
        return path

    @property
    def n_pulses(self):
        """Number of rising edges (times the output went high)."""
        return sum(1 for e in self.events if e["state"] == 1)

    def close(self):
        """Return the line to 0 V and release the device."""
        if self._worker is not None:
            try:
                self._queue.put(None)          # drain any in-flight pulse first
                self._worker.join(timeout=1.0)
            except Exception:
                pass
            self._worker = None
        if self._dropped_pulses:
            print("[LabJack {}] WARNING: {} pulse(s) dropped (USB backlog)".format(
                self.channel, self._dropped_pulses))
        try:
            self.set_low()
        except Exception:
            pass
        if self.backend == "u3" and self.device is not None:
            try:
                self.device.close()
            except Exception:
                pass
