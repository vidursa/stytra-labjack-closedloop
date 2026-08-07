"""Closed-loop tail-bend -> LabJack U3 experiment.

Runs stytra against the recorded video (standing in for a live camera) and, for
every frame in which the tracked tail is bent > 10 deg to the right, drives a
5 V pulse on a LabJack U3 (simulated here) while flashing a white circle on the
stimulus display.

Everything stytra normally saves is written to ``dir_save`` at the end of the
session: the recorded video (.mp4), the per-frame behaviour log (tail_sum + tail
angles, with frame timestamps), the estimator log (bend angle over time), the
stimulus/dynamic log (tail_sum / triggered / output_v / pulse with timestamps) and the
metadata .json. In addition, ``run_experiment`` writes ``labjack_pulses.csv``
with every high/low transition of the output line.

Usage (inside a stytra environment, see README):
    python run_experiment.py
"""

import argparse
import json
import os
import multiprocessing
from pathlib import Path

from stytra import Stytra
from stytra.stimulation import Protocol
from lightparam import Param
from PyQt5.QtCore import QTimer

from labjack_device import LabJackU3Output
from stytra_integration import RightBendEstimator, RightBendTriggerStimulus

# --- paths ---------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent.parent
VIDEO_FILE = REPO_ROOT / "Basler_acA640-750um__25004149__20260513_144734755.mp4"
PARAMS_FILE = (
    REPO_ROOT / "Basler_acA640-750um__25004149__20260513_144734755_trackingparams.json"
)
SAVE_DIR = REPO_ROOT / "recordings"

# --- hardware config -----------------------------------------------------
# Set LABJACK_BACKEND = "u3" when a real LabJack U3 is connected (see
# HARDWARE_GUIDE.md). "simulate" records + prints every transition instead.
LABJACK_BACKEND = "u3" #"simulate"
# U3-HV: FIO0-FIO3 are fixed analog inputs, so a digital output must use
# FIO4-FIO7 (or EIO0-EIO7). FIO4 gives a 3.3 V logic pulse. For a true ~5 V
# level set LABJACK_CHANNEL = "DAC0" and LABJACK_VOLTAGE = 5.0.
LABJACK_CHANNEL = "FIO4"
LABJACK_VOLTAGE = 3.3  # nominal logic-high level (for logging)

# --- camera config -------------------------------------------------------
# Simulation: USE_REAL_CAMERA = False plays the recorded video as if it were a
# live camera. Real rig: set True and describe the camera in REAL_CAMERA (the
# `type` keys into stytra's camera_class_dict: basler/ximea/avt/spinnaker/
# mikrotron/opencv). See HARDWARE_GUIDE.md.
USE_REAL_CAMERA = True
REAL_CAMERA = dict(type="basler", max_buffer_length=12000)

# Acquisition rate. This is ALSO the framerate stamped into the saved .mp4:
# stytra's StreamingVideoWriter defaults to output_framerate=24, so a 200 fps
# recording would play back 8x too slow (60 s of data -> 8+ minutes of video).
# The frame data and all timestamps were always correct; only the container's
# playback rate tag was wrong. Set this to the camera's real frame rate.
CAMERA_FPS = 200

# --- TTL stream recording (real LabJack only) ----------------------------
# Record camera / stimulation TTLs on the U3's analog inputs in stream mode,
# alongside the closed loop, on the same device. Written to
# <session>_ttl_stream.csv with t = seconds since protocol start.
# Note: the U3 loses a few stream samples for each output pulse we send (one
# device, two access modes) - the missed count is printed at the end.
# Tip: wire the FIO4 output back into one of these analog inputs to get a
# ground-truth 1 kHz record of the stimulation on the same clock as the cameras.
RECORD_TTL_STREAM = False
TTL_STREAM_CHANNELS = (1, 2, 3)  # AIN1, AIN2, AIN3
TTL_STREAM_LABELS = ("stim_ttl", "camera_ttl", "behaviourcam_ttl")
TTL_STREAM_HZ = 1000
# Leave False on a U3-HV (FIO0-FIO3 are permanently analog). True runs
# configIO(FIOAnalog=15) for a standard U3.
TTL_STREAM_CONFIG_ANALOG = False

# --- trigger config ------------------------------------------------------
# Trigger when the tail_sum (radians, the value shown in the stytra plot),
# AFTER baseline re-zeroing (see below), exceeds this threshold to the right.
BEND_THRESHOLD = 0.8  # only for fixed and level mode
BEND_DIRECTION = 1  # flip to -1 if the circle fires on LEFT bends

# --- baseline re-zeroing -------------------------------------------------
# The tracked tail_sum often rests at a non-zero value (e.g. 0.17 rad) depending
# on how the tail start point / tip are placed, so one side sits closer to the
# threshold than the other. This subtracts the resting offset before the
# threshold is applied, so both sides are equally far from rest. Detection uses
# (tail_sum - baseline); the raw tail_sum is still logged, and `baseline` is
# saved as its own column so plot_session can draw the re-zeroed thresholds.
#   "off"     : no correction (default; identical to before).
#   "fixed"   : subtract BASELINE_OFFSET, a value you read off a session plot.
#   "start"   : auto - average the first BASELINE_WINDOW_S s (fish at rest just
#               after you press record), then hold that offset for the session.
#   "running" : auto - moving average over the last BASELINE_WINDOW_S s; tracks
#               slow drift (fast beats average out). Slowly absorbs a sustained
#               one-sided hold, by design.
BASELINE_MODE = "start"
BASELINE_OFFSET = 0.0  # ("fixed") resting tail_sum to subtract, e.g. 0.17
BASELINE_WINDOW_S = 1.0  # ("start"/"running") averaging window (seconds)

# --- pulse config --------------------------------------------------------
# "level"      = output is HIGH the whole time tail_sum is over threshold and
#                LOW otherwise -> turns on/off as fast as the tail crosses the
#                threshold. Follows a ~12 Hz beat; no refractory delay.
# "fixed"      = a single PULSE_WIDTH_S pulse per bend onset, rate-limited by
#                REFRACTORY_S (one discrete pulse per bend event).
# "predictive" = ANTICIPATORY. Waits for the tail to bend to the OPPOSITE side
#                (past OPPOSITE_THRESHOLD), then fires as it swings back up
#                through FIRE_LEVEL (0 = neutral) - i.e. BEFORE the tail has
#                bent to the trigger side. Cancels the stimulation delay/jitter
#                caused by waiting for a fast beat to reach the trigger side.
PULSE_MODE = "predictive"
PULSE_WIDTH_S = 0.01  # (fixed/predictive) electrical pulse width, 10 ms
REFRACTORY_S = 0.01  # (fixed) min gap between pulses (10 ms here)
CIRCLE_DISPLAY_S = 0.1  # keep the circle visible this long after a pulse

# (fixed/predictive only) Delay the pulse a fixed time AFTER the trigger
# condition is detected. This decouples DETECTION from STIMULATION timing: pick
# BEND_THRESHOLD / the predictive levels purely for reliable, low-jitter
# detection, then use this knob to place the stimulation at any phase of a later
# beat cycle (0.0 = fire immediately, as before). The delay is applied on the
# stimulus clock, so the saved logs (and plot_session) show the pulse at its
# real, delayed time. Ignored in "level" mode.
STIM_DELAY_S = 0.0

# --- predictive-mode config (only used when PULSE_MODE = "predictive") ----
# How far to the OPPOSITE side (radians) the tail must bend to count as a real
# beat and arm the trigger. Set near your typical opposite-bend peak.
OPPOSITE_THRESHOLD = 0.45
# Where on the return stroke to fire: the oriented tail_sum level whose upward
# crossing emits the pulse. 0.0 = fire at the neutral crossing; NEGATIVE fires
# earlier (still on the opposite side); positive fires slightly later. This is
# the "how far after the other-side bend to stimulate" knob.
FIRE_LEVEL = -0.4  # only for predictive

# --- latency config ------------------------------------------------------
# stytra drains freshly-tracked frames into the accumulator (which the trigger
# reads) on its `gui_timer`, which it starts at 60 Hz (~16 ms). That polling
# gap is the single largest *reducible* source of stimulation delay after the
# tail crosses threshold. We raise the gui_timer to POLL_INTERVAL_MS so the
# latest tail_sum refreshes at ~camera rate. 4 ms = 250 Hz matches a 200 fps
# camera; there is no benefit going below the camera's frame period (the value
# can't update faster than frames arrive), and very small values just add CPU
# load. Set to 16 to restore stytra's default.
POLL_INTERVAL_MS = 4

# --- session control -----------------------------------------------------
# How long each session records (seconds). This drives the run length directly
# via the stimulus duration, so it is NOT affected by stytra's restored config
# (see note below). The source video is ~60 s (11997 frames @ 200 fps), played
# back at real time, so 60.0 captures the whole clip once; larger values re-
# record the looped video from the start.
#
# NOTE: stytra saves every GUI/protocol param to ~/stytra_last_config.json and
# restores it on the next launch, so a `session_duration` typed in the GUI (or
# left over from an earlier run) would override a code default. We therefore
# read this module constant directly instead of the restorable Param, so the
# value here always wins. To change the length, edit THIS number or pass
# --duration on the command line (which overwrites this before the protocol is
# built, so it wins over both the code default and the restored config).
SESSION_DURATION_S = 60.0

# AUTO_START runs the protocol automatically a few seconds after launch. That
# leaves no time to position the tail start point / tip, so it is OFF by
# default: the camera streams and tracking runs live as soon as the window
# opens, you drag the tracking points until the tail overlay looks right, and
# THEN press the ▶ toggle to begin recording. Pass --autostart for hands-off
# runs (or set this True).
AUTO_START = False
AUTO_START_DELAY_S = 3.0

# --- tracking parameters -------------------------------------------------
# "file" : load PARAMS_FILE (the exported *_trackingparams.json).
# "last" : don't load anything - stytra restores the tracking parameters from
#          the previous run automatically (it saves the whole parameter tree to
#          ~/stytra_last_config.json on exit and applies it at startup). Use
#          this once you have adjusted the tail points in the GUI and want to
#          keep them run to run.
PARAMS_SOURCE = "last"


class RightBendClosedLoop(Protocol):
    name = "labjack_right_bend_cl"

    stytra_config = dict(
        display=dict(full_screen=False),
        tracking=dict(embedded=True, method="tail", estimator=RightBendEstimator),
        # Live camera when USE_REAL_CAMERA, otherwise the recorded video.
        camera=(
            dict(REAL_CAMERA) if USE_REAL_CAMERA else dict(video_file=str(VIDEO_FILE))
        ),
        # output_framerate must match the acquisition rate or the saved .mp4
        # plays back at the wrong speed (see CAMERA_FPS).
        recording=dict(extension="mp4", kbit_rate=10000, output_framerate=CAMERA_FPS),
    )

    def __init__(self):
        super().__init__()
        # Shown in the GUI for reference, but the actual run length is taken
        # from the SESSION_DURATION_S constant in get_stim_sequence(), so it is
        # not overridden by stytra's restored config.
        self.session_duration = Param(SESSION_DURATION_S, limits=(1.0, 3600.0))
        # One output device, reused across protocol rebuilds.
        self.labjack = LabJackU3Output(
            backend=LABJACK_BACKEND,
            channel=LABJACK_CHANNEL,
            voltage=LABJACK_VOLTAGE,
        )

    def get_stim_sequence(self):
        return [
            RightBendTriggerStimulus(
                labjack=self.labjack,
                threshold=BEND_THRESHOLD,
                direction=BEND_DIRECTION,
                pulse_mode=PULSE_MODE,
                pulse_width_s=PULSE_WIDTH_S,
                refractory_s=REFRACTORY_S,
                stim_delay_s=STIM_DELAY_S,
                baseline_mode=BASELINE_MODE,
                baseline_offset=BASELINE_OFFSET,
                baseline_window_s=BASELINE_WINDOW_S,
                opposite_threshold=OPPOSITE_THRESHOLD,
                fire_level=FIRE_LEVEL,
                circle_display_s=CIRCLE_DISPLAY_S,
                # Use the module constant, NOT self.session_duration, so the
                # length can't be silently reset to an old value by stytra's
                # config restore.
                duration=SESSION_DURATION_S,
            )
        ]


def _shutdown_background_processes(timeout=3.0):
    """Force-terminate any stytra child processes left alive after the window
    closes.

    stytra's camera / frame-dispatcher / video-writer are ``multiprocessing.
    Process`` objects that are NOT daemonic, and the window's close handler does
    not join all of them. Their (and their queues') surviving threads would
    otherwise block interpreter exit, so the cmd prompt appears to hang. By the
    time the Qt loop has returned, every session has already been saved, so it
    is safe to terminate them outright.
    """
    kids = multiprocessing.active_children()
    if not kids:
        return
    print("Shutting down {} background process(es)...".format(len(kids)))
    for p in kids:
        p.terminate()
    for p in kids:
        p.join(timeout=timeout)
    for p in multiprocessing.active_children():  # anything stubborn: kill hard
        try:
            p.kill()
        except Exception:
            pass


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Closed-loop tail-bend -> LabJack U3 experiment.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    ap.add_argument(
        "-d",
        "--duration",
        type=float,
        default=SESSION_DURATION_S,
        metavar="SECONDS",
        help="total recording time per session",
    )
    ap.add_argument(
        "--params",
        choices=("file", "last"),
        default=PARAMS_SOURCE,
        help="tracking parameters: load the exported JSON, or keep the ones "
        "stytra restored from your previous run",
    )
    start = ap.add_mutually_exclusive_group()
    start.add_argument(
        "--autostart",
        dest="autostart",
        action="store_true",
        default=AUTO_START,
        help="start recording automatically a few seconds after launch",
    )
    start.add_argument(
        "--no-autostart",
        dest="autostart",
        action="store_false",
        help="wait for the ▶ toggle so you can position the tail points first",
    )
    ap.add_argument(
        "--ttl-stream",
        action="store_true",
        default=RECORD_TTL_STREAM,
        help="also stream the TTL analog inputs to CSV (needs a real U3)",
    )
    return ap.parse_args(argv)


def main(argv=None):
    global SESSION_DURATION_S

    args = parse_args(argv)
    # Applied before the protocol is constructed, so get_stim_sequence() picks
    # it up and stytra's restored config cannot override it.
    SESSION_DURATION_S = args.duration

    SAVE_DIR.mkdir(exist_ok=True)

    if not USE_REAL_CAMERA and not VIDEO_FILE.exists():
        raise FileNotFoundError("Video not found: {}".format(VIDEO_FILE))

    protocol = RightBendClosedLoop()

    # exec=False so we can inject the tracking parameters before the Qt event
    # loop starts, then run the loop ourselves.
    st = Stytra(protocol=protocol, dir_save=str(SAVE_DIR), exec=False)

    # Keep the tail trace plot live DURING the protocol (stytra normally freezes
    # it at ▶ "so plotting does not interfere with stimulus display"). This shows
    # the raw tracking tail, the estimator tail_sum, and the stimulus decision
    # signals (tail_sum/triggered/output_v/pulse) scrolling in real time, plus a
    # red vertical marker at the moment recording started. If you ever see the
    # stimulus display stutter on a slow PC, set this back to False.
    st.exp.keep_plot_live_during_protocol = True

    # Apply the saved tail-tracking parameters. Pushing them straight onto the
    # tracking process' parameter queue guarantees the worker adopts them; we
    # also set them on the main-process pipeline so the GUI reflects them.
    #
    # With --params last we skip this entirely: stytra has already restored the
    # whole parameter tree (tail start point, tip, n_segments, ...) from
    # ~/stytra_last_config.json during start_experiment(), so the previous run's
    # values are in force. We only re-push them to the tracking worker, which
    # starts from class defaults and would otherwise wait for the next
    # changed-parameter tick.
    if args.params == "last":
        try:
            st.exp.processing_params_queue.put(st.exp.pipeline.serialize_params())
            print(
                "Using tracking parameters restored from the previous run "
                "(~/stytra_last_config.json); PARAMS_FILE not loaded."
            )
        except Exception as e:
            print("Could not re-push restored params to the tracking worker:", e)
    elif PARAMS_FILE.exists():
        params = json.load(open(str(PARAMS_FILE)))
        pipeline_params = params.get("pipeline_params", params)
        st.exp.processing_params_queue.put(pipeline_params)
        try:
            st.exp.pipeline.deserialize_params(pipeline_params)
        except Exception as e:  # non-fatal: worker already has the params
            print("Could not apply params to GUI pipeline:", e)
        print("Loaded tracking parameters from", PARAMS_FILE.name)
    else:
        print("WARNING: tracking params file not found, using defaults:", PARAMS_FILE)

    # Optional TTL stream recording on the same U3 (camera + stimulation TTLs).
    # Started/stopped with the protocol so its t=0 matches every stytra log.
    ttl_stream = None
    if args.ttl_stream:
        from labjack_stream import TTLStreamRecorder

        ttl_stream = TTLStreamRecorder(
            protocol.labjack,
            channels=TTL_STREAM_CHANNELS,
            labels=TTL_STREAM_LABELS,
            scan_frequency=TTL_STREAM_HZ,
            configure_analog_inputs=TTL_STREAM_CONFIG_ANALOG,
        )
        st.exp.protocol_runner.sig_protocol_started.connect(ttl_stream.start)
        st.exp.protocol_runner.sig_protocol_finished.connect(ttl_stream.stop)
        st.exp.protocol_runner.sig_protocol_interrupted.connect(ttl_stream.stop)

    # Print a clear confirmation whenever a session is saved.
    def _on_saved():
        if ttl_stream is not None:
            ttl_stream.stop()  # no-op if the finished signal already stopped it
            if ttl_stream.samples:
                path = ttl_stream.save_csv(st.exp.filename_base() + "ttl_stream.csv")
                print(
                    "    TTL stream -> {} ({} scans)".format(
                        path, len(ttl_stream.samples)
                    )
                )
        print("\n=== SESSION SAVED ===")
        print("    logs + video under: {}".format(SAVE_DIR / RightBendClosedLoop.name))
        print("    (press the ▶ toggle again for another run, or close the window)\n")

    st.exp.sig_data_saved.connect(_on_saved)

    # Cut the post-threshold stimulation delay: raise stytra's tracking-drain
    # timer from its 60 Hz default to POLL_INTERVAL_MS. start_protocol() resets
    # gui_timer to 60 Hz itself, so we re-apply *after* it returns by deferring
    # via singleShot(0, ...) on the protocol-started signal (covers both the
    # AUTO_START path and the manual toolbar button).
    def _boost_poll_rate():
        st.exp.gui_timer.setInterval(POLL_INTERVAL_MS)
        if getattr(st.exp, "acc_tracking", None) is not None:
            print(
                "[latency] tracking poll raised to {} ms (~{:.0f} Hz)".format(
                    POLL_INTERVAL_MS, 1000.0 / POLL_INTERVAL_MS
                )
            )

    if POLL_INTERVAL_MS and POLL_INTERVAL_MS < 16:
        st.exp.protocol_runner.sig_protocol_started.connect(
            lambda: QTimer.singleShot(0, _boost_poll_rate)
        )

    # Single teardown path, fired either when the event loop returns OR the
    # instant Qt starts quitting (window closed) - whichever comes first. This
    # guarantees the shell returns instead of hanging on stytra's non-daemon
    # camera / dispatcher / writer processes, regardless of the installed
    # stytra version's own (sometimes incomplete) shutdown.
    _done = {"torn_down": False}

    def _teardown_and_exit():
        if _done["torn_down"]:
            return
        _done["torn_down"] = True
        protocol.labjack.close()
        pulse_log = SAVE_DIR / "labjack_pulses.csv"
        protocol.labjack.save_log(pulse_log)
        print(
            "\nSession finished. {} pulses emitted. Pulse log -> {}".format(
                protocol.labjack.n_pulses, pulse_log
            )
        )
        _shutdown_background_processes()
        # Hard-exit: skip any atexit joins / queue feeder threads that would
        # otherwise keep the interpreter (and the cmd window) alive.
        os._exit(0)

    st.exp.app.aboutToQuit.connect(_teardown_and_exit)

    if args.autostart:
        print(
            "\nAuto-starting the protocol in {:.0f}s; it records for {:.0f}s then "
            "SAVES automatically.\nWatch for [trigger]/[LabJack] lines below and the "
            "white circle on the stimulus window.\n".format(
                AUTO_START_DELAY_S, SESSION_DURATION_S
            )
        )
        QTimer.singleShot(int(AUTO_START_DELAY_S * 1000), st.exp.start_protocol)
    else:
        print(
            "\nCamera + tracking are live NOW; recording has not started.\n"
            "  1. Drag the tail start point and the tip in the camera window until\n"
            "     the tracked overlay follows the tail.\n"
            "  2. Press the ▶ toggle (top toolbar) to begin recording for {:.0f}s.\n"
            "     It saves automatically when the duration elapses; press ▶ again\n"
            "     to stop early.\n".format(SESSION_DURATION_S)
        )

    # Run the Qt event loop. Teardown normally fires via aboutToQuit above; the
    # finally is a fallback if exec_() ever returns without emitting it.
    try:
        st.exp.app.exec_()
    finally:
        _teardown_and_exit()


if __name__ == "__main__":
    main()
