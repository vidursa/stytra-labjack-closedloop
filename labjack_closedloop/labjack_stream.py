"""Background TTL stream recorder for the LabJack U3.

Records analog inputs in the U3's *stream* mode while the closed loop runs, so
the camera exposure strobes and the stimulation TTL land on one common,
crystal-accurate clock. This is the streaming half of the previous standalone
`streamTest-threading` script, restructured to live inside the stytra session:

  * it shares the SAME ``u3.U3()`` handle as the closed-loop output (opening a
    second handle to one device is what breaks on Windows);
  * it starts on protocol start and stops on protocol end, so t=0 is the same
    zero as every stytra log;
  * sample times come from the U3's own scan clock (start time + index/rate)
    rather than ``datetime.now()`` per packet, which drifts by however long the
    PC took to drain the queue.

Only meaningful with ``LABJACK_BACKEND = "u3"``; with the simulated backend the
recorder is inert and ``start()`` returns False.

IMPORTANT - stream vs. command/response
    The U3 can accept command/response packets (our output pulses) while
    streaming, but each one costs a few stream samples. Expect a small non-zero
    "missed" count at the end of a session; it is reported so you can see it.
    If you need zero missed samples, use a second U3 for the output.
"""

import csv
import threading
import time

DEFAULT_CHANNELS = (1, 2, 3)
DEFAULT_LABELS = ("stim_ttl", "camera_ttl", "behaviourcam_ttl")
DEFAULT_SCAN_FREQUENCY = 1000
LOGIC_THRESHOLD_V = 1.0


class TTLStreamRecorder:
    """Stream N analog inputs to memory on a worker thread, then dump to CSV."""

    def __init__(
        self,
        labjack,
        channels=DEFAULT_CHANNELS,
        labels=DEFAULT_LABELS,
        scan_frequency=DEFAULT_SCAN_FREQUENCY,
        resolution=1,
        threshold_v=LOGIC_THRESHOLD_V,
        configure_analog_inputs=False,
        verbose=True,
    ):
        """
        Parameters
        ----------
        labjack : LabJackU3Output
            The already-open output device; its handle and lock are reused.
        channels : sequence of int
            Analog input numbers to stream (AIN1, AIN2, AIN3 by default).
        labels : sequence of str
            Column names, one per channel.
        scan_frequency : int
            Scans per second per channel (1000 Hz = 1 ms resolution).
        resolution : int
            U3 stream resolution index; 1 is fast and plenty for logic levels.
        threshold_v : float
            Voltage above which a channel is recorded as logic 1.
        configure_analog_inputs : bool
            Leave False on a U3-**HV**, where FIO0-FIO3 are permanently analog.
            Set True on a standard U3 to run ``configIO(FIOAnalog=15)``; that
            call also resets FIO4-FIO7 to digital *inputs*, so the output line's
            direction is re-asserted immediately afterwards.
        """
        self._lj = labjack
        self.channels = list(channels)
        self.labels = list(labels)[: len(self.channels)]
        while len(self.labels) < len(self.channels):
            self.labels.append("AIN{}".format(self.channels[len(self.labels)]))
        self.scan_frequency = int(scan_frequency)
        self.resolution = int(resolution)
        self.threshold_v = float(threshold_v)
        self.configure_analog_inputs = bool(configure_analog_inputs)
        self.verbose = verbose

        self.samples = []       # [t, v0, logic0, v1, logic1, ...]
        self.missed = 0
        self.errors = 0
        self.t_start_wall = None

        self._thread = None
        self._stop_evt = threading.Event()

    @property
    def available(self):
        return getattr(self._lj, "backend", None) == "u3" and self._lj.device is not None

    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    # -- lifecycle -------------------------------------------------------
    def start(self):
        """Configure and begin streaming. Returns True if it actually started."""
        if not self.available:
            if self.verbose:
                print("[ttl-stream] LabJack backend is not 'u3' - not recording.")
            return False
        if self.running:
            return False

        d = self._lj.device
        try:
            with self._lj._lock:
                if self.configure_analog_inputs:
                    d.configIO(FIOAnalog=15)
                    # configIO reset FIO4-7 to inputs; put our output line back.
                    self._lj.reassert_output_direction()
                d.getCalibrationData()
                d.streamConfig(
                    NumChannels=len(self.channels),
                    PChannels=list(self.channels),
                    NChannels=[31] * len(self.channels),  # 31 = single-ended
                    Resolution=self.resolution,
                    ScanFrequency=self.scan_frequency,
                )
        except Exception as exc:
            print("[ttl-stream] streamConfig failed: {!r}".format(exc))
            return False

        self.samples = []
        self.missed = 0
        self.errors = 0
        self._stop_evt.clear()
        self._thread = threading.Thread(
            target=self._run, name="labjack-ttl-stream", daemon=True
        )
        self._thread.start()
        if self.verbose:
            print("[ttl-stream] recording AIN{} at {} Hz".format(
                ", AIN".join(str(c) for c in self.channels), self.scan_frequency))
        return True

    def stop(self, timeout=3.0):
        """Ask the worker to stop and wait for it to drain."""
        if not self.running:
            return
        self._stop_evt.set()
        self._thread.join(timeout=timeout)
        self._thread = None
        if self.verbose:
            print("[ttl-stream] stopped: {} scans, {} missed, {} errors".format(
                len(self.samples), self.missed, self.errors))

    # -- worker ----------------------------------------------------------
    def _run(self):
        d = self._lj.device
        keys = ["AIN{}".format(c) for c in self.channels]
        thr = self.threshold_v
        dt = 1.0 / self.scan_frequency
        scan_index = 0

        try:
            with self._lj._lock:
                d.streamStart()
            self.t_start_wall = time.time()
        except Exception as exc:
            print("[ttl-stream] streamStart failed: {!r}".format(exc))
            return

        try:
            for packet in d.streamData(convert=False):
                if self._stop_evt.is_set():
                    break
                if packet is None:
                    continue
                self.missed += packet.get("missed", 0)
                self.errors += packet.get("errors", 0)

                # Conversion is done here (not in the stream reader) so the USB
                # read loop keeps up, exactly as in the original script.
                r = d.processStreamData(packet["result"])
                series = [r[k] for k in keys]
                n = min(len(s) for s in series)
                for i in range(n):
                    row = [(scan_index + i) * dt]
                    for s in series:
                        v = s[i]
                        row.append(v)
                        row.append(1 if v > thr else 0)
                    self.samples.append(row)
                scan_index += n
        except Exception as exc:
            print("[ttl-stream] read loop stopped: {!r}".format(exc))
        finally:
            try:
                with self._lj._lock:
                    d.streamStop()
            except Exception:
                pass

    # -- output ----------------------------------------------------------
    def save_csv(self, path):
        """Write the recorded scans. ``t`` is seconds since protocol start."""
        header = ["t"]
        for label in self.labels:
            header.append(label + "_v")
            header.append(label)
        with open(str(path), "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(header)
            for row in self.samples:
                writer.writerow(
                    [round(row[0], 6)]
                    + [round(x, 4) if isinstance(x, float) else x for x in row[1:]]
                )
        return path
