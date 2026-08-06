"""Overlay tail_sum with the closed-loop pulse markers for a saved session.

Works with both the real stytra logs and the offline-simulated ones:
  * stytra saves `<time>_behavior_log.csv` (tail_sum + tail angles) and
    `<time>_stimulus_log.csv` (right_bend_trigger_* columns) with a leading
    index column and `t` as the last column;
  * simulate_tracking.py writes `simulated_behavior_log.csv` /
    `simulated_stimulus_log.csv` with bare column names and `t` first.

Columns are matched by name (not position), so either layout loads.

The threshold line is read from the session's ``*_metadata.json`` (the value
stytra actually used), so it tracks whatever ``BEND_THRESHOLD`` you ran with;
pass ``--threshold`` only to override it (e.g. for the simulated logs, which
have no metadata).

Usage:
    python plot_session.py                        # newest session under ../recordings
    python plot_session.py ../recordings/simulated
    python plot_session.py path/to/session_dir    # threshold taken from metadata
    python plot_session.py path/to/session_dir/<prefix>_behavior_log.csv
    python plot_session.py <dir> --threshold 1.0 --direction 1   # override
    python plot_session.py <dir> --save out.png --no-show

The CSV loader is pure-stdlib; matplotlib is only needed for the actual plot.
"""

import argparse
import csv
import json
import os
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RECORDINGS = REPO_ROOT / "recordings"


# ----------------------------------------------------------------------
# loading + column matching (stdlib only)
# ----------------------------------------------------------------------
def load_csv(path):
    """Return {column_name: [float, ...]} for a CSV, NaN for blanks/non-numbers."""
    cols = {}
    with open(str(path), newline="") as f:
        reader = csv.reader(f)
        header = next(reader)
        header = [h.strip() for h in header]
        cols = {h: [] for h in header}
        for row in reader:
            for h, v in zip(header, row):
                try:
                    cols[h].append(float(v))
                except (ValueError, TypeError):
                    cols[h].append(float("nan"))
    return cols


def _find(cols, predicate):
    for name in cols:
        if predicate(name.lower().strip()):
            return name
    return None


def time_col(cols):
    return (
        _find(cols, lambda n: n == "t")
        or _find(cols, lambda n: n == "time")
        or _find(cols, lambda n: n.endswith("time"))
    )


def col_by_suffix(cols, suffix):
    """Match a bare name (`pulse`) or a stimulus-prefixed one (`..._pulse`)."""
    return _find(
        cols, lambda n: n == suffix or n.endswith("_" + suffix) or n.endswith(suffix)
    )


def pulse_onsets(stim):
    """Times at which a pulse fired (pulse column == 1)."""
    tc = time_col(stim)
    pc = col_by_suffix(stim, "pulse")
    if not tc or not pc:
        return []
    return [t for t, p in zip(stim[tc], stim[pc]) if p >= 0.5]


def baseline_series(stim):
    """(times, baseline_values) from the stimulus log's ``baseline`` column.

    Returns ``(None, None)`` if there is no baseline column (older logs, or
    baseline_mode="off" won't be drawn since it is all-zero)."""
    if stim is None:
        return None, None
    tc = time_col(stim)
    bc = col_by_suffix(stim, "baseline")
    if not tc or not bc:
        return None, None
    return stim[tc], stim[bc]


def active_intervals(stim):
    """(start, end) spans during which the output was actually high.

    Read from ``output_v`` (volts; falls back to the ``triggered`` flag), so it
    shows the REAL stimulation duration - a 30 ms vs a 150 ms pulse now differ -
    and, with a stimulation delay, the span sits at the delayed pulse time rather
    than at the detection instant. Uses the step-post convention: a span runs
    from the sample the output goes high up to the sample it drops back to 0."""
    tc = time_col(stim)
    vc = col_by_suffix(stim, "output_v") or col_by_suffix(stim, "triggered")
    if not tc or not vc:
        return []
    ts, vs = stim[tc], stim[vc]
    intervals = []
    start = None
    for t, v in zip(ts, vs):
        on = (v == v) and (v > 1e-9)  # NaN-safe; output_v is 0 or ~volts
        if on and start is None:
            start = t
        elif (not on) and start is not None:
            intervals.append((start, t))
            start = None
    if start is not None and ts:
        intervals.append((start, ts[-1]))
    return intervals


# ----------------------------------------------------------------------
# session discovery
# ----------------------------------------------------------------------
def _latest(globbed):
    files = list(globbed)
    if not files:
        return None
    return max(files, key=os.path.getmtime)


def _session_prefix(p):
    """The filename prefix shared by one session's logs.

    e.g. ``'161011_'`` from ``161011_behavior_log.csv``. Returns ``''`` for a
    directory or a file with no recognisable prefix, which means "no specific
    session - take the newest in the folder"."""
    if not p.is_file():
        return ""
    name = p.name
    for tag in ("behavior_log", "stimulus_log", "estimator_log", "metadata"):
        i = name.find(tag)
        if i > 0:
            return name[:i]
    return ""


def find_logs(path):
    """Return (behavior_csv, stimulus_csv, session_dir, prefix) for a dir/file.

    Passing a directory (or an unprefixed file) selects the NEWEST logs in that
    folder. Passing a specific ``<prefix>_behavior_log.csv`` pins the plot to
    THAT session: the stimulus log (and, via the prefix, the metadata) are
    matched by the same ``<prefix>`` rather than by newest-modified."""
    p = Path(path)
    d = p.parent if p.is_file() else p
    prefix = _session_prefix(p)
    beh = _latest(d.glob(prefix + "*behavior_log*.csv"))
    stim = _latest(d.glob(prefix + "*stimulus_log*.csv"))
    return beh, stim, d, prefix


def default_session():
    """Newest session directory that contains a behavior log, else None."""
    if not RECORDINGS.exists():
        return None
    newest = _latest(RECORDINGS.rglob("*behavior_log*.csv"))
    return newest.parent if newest else None


def _search_stim_state(obj):
    """Depth-first search a parsed metadata tree for the stimulus state dict
    carrying our trigger parameters (anchored on ``threshold``). Returns the
    dict, or None."""
    if isinstance(obj, dict):
        if "threshold" in obj:
            return obj
        for v in obj.values():
            r = _search_stim_state(v)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = _search_stim_state(v)
            if r is not None:
                return r
    return None


def stim_params_from_metadata(session_dir, prefix=""):
    """Read the trigger parameters actually used from ``*_metadata.json``.

    stytra records each stimulus's static params (``threshold``, ``direction``,
    ``pulse_mode``, ``opposite_threshold``, ``fire_level``, ...) under
    ``stimulus/log``, so the plot can show the values that were really in force
    instead of hard-coded guesses. Returns ``{}`` when there is no metadata
    (e.g. the offline-simulated logs)."""
    meta = _latest(Path(session_dir).glob(prefix + "*metadata*.json"))
    if meta is None:
        return {}
    try:
        with open(str(meta)) as f:
            return _search_stim_state(json.load(f)) or {}
    except Exception:
        return {}


def _meta_float(meta, key):
    try:
        return float(meta[key])
    except (KeyError, TypeError, ValueError):
        return None


# ----------------------------------------------------------------------
# plotting (matplotlib only imported here)
# ----------------------------------------------------------------------
def plot(behavior, stimulus, threshold, direction, out_path, show,
         pulse_mode=None, opposite_threshold=None, fire_level=None):
    import matplotlib

    if not show:
        matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    # tail_sum trace: prefer the behavior log (full tracking rate)
    ts_src = behavior if behavior is not None else stimulus
    tcol = time_col(ts_src)
    scol = col_by_suffix(ts_src, "tail_sum")
    if tcol is None or scol is None:
        raise SystemExit("Could not find 't' and 'tail_sum' columns in the logs.")

    onsets = pulse_onsets(stimulus) if stimulus is not None else []
    spans = active_intervals(stimulus) if stimulus is not None else []
    active_thr = direction * threshold
    predictive = (pulse_mode == "predictive")

    fig, (ax1, ax2) = plt.subplots(
        2, 1, sharex=True, figsize=(12, 6), gridspec_kw={"height_ratios": [3, 1]}
    )

    ax1.plot(ts_src[tcol], ts_src[scol], lw=0.8, color="#1f77b4", label="tail_sum")

    # Decision levels are applied to the RE-ZEROED signal (tail_sum - baseline).
    # If a baseline was used they must be drawn relative to that resting level so
    # they line up with the raw trace: each level L becomes baseline(t) + L. When
    # the baseline is off (all-zero) we draw plain horizontal lines as before.
    bt, bvals = baseline_series(stimulus)
    baseline_on = bvals is not None and any(
        (b == b) and abs(b) > 1e-9 for b in bvals
    )

    def _level_line(offset, **kw):
        """Draw a decision level: a curve baseline(t)+offset if re-zeroed, else
        a flat axhline at offset."""
        if baseline_on:
            ax1.plot(bt, [(b if b == b else 0.0) + offset for b in bvals], **kw)
        else:
            kw.pop("drawstyle", None)
            ax1.axhline(offset, **kw)

    if baseline_on:
        # The resting level itself (replaces the neutral 0 line): the trace now
        # beats around THIS, and the thresholds are symmetric about it.
        ax1.plot(bt, bvals, ls="-", color="0.5", lw=1.0, label="baseline (rest)")
        # Symmetric opposite-side reference, so "equally distant" is visible.
        _level_line(-active_thr, ls="--", color="0.7", lw=0.8,
                    label="- threshold (symmetric)")
    else:
        ax1.axhline(0.0, ls="-", color="0.7", lw=0.6)

    # The trigger-side threshold. In predictive mode it is NOT what fires the
    # pulse, so draw it faintly to avoid implying otherwise.
    _level_line(
        active_thr,
        ls=":" if predictive else "--",
        color="0.55" if predictive else "crimson",
        lw=1.0 if predictive else 1.2,
        label="threshold = {:g}{}{}".format(
            active_thr,
            " + baseline" if baseline_on else "",
            " (unused in predictive)" if predictive else ""),
    )

    # Predictive mode fires off two OTHER levels, both defined on the oriented
    # RE-ZEROED signal (direction * (tail_sum - baseline)). Convert them back
    # into raw tail_sum coordinates (add baseline via _level_line):
    #   arm  : oriented <= -opposite_threshold  ->  offset = -direction*opp
    #   fire : oriented crosses up through fire_level -> offset = direction*fire
    if predictive and opposite_threshold is not None:
        _level_line(
            -direction * opposite_threshold,
            ls="--", color="seagreen", lw=1.3,
            label="opposite bend (arm) = {:g}".format(-direction * opposite_threshold),
        )
    if predictive and fire_level is not None:
        _level_line(
            direction * fire_level,
            ls="-.", color="crimson", lw=1.3,
            label="fire level = {:g}".format(direction * fire_level),
        )
    # Shade the real stimulation spans (width = pulse duration, position = the
    # delayed pulse time). Fall back to thin onset lines if the output column is
    # missing or a span collapses to a single sample (sub-frame pulse).
    if spans:
        for i, (a, b) in enumerate(spans):
            ax1.axvspan(a, b, color="crimson", alpha=0.25,
                        label="stimulation" if i == 0 else None)
    for i, t in enumerate(onsets):
        ax1.axvline(t, color="crimson", alpha=0.30, lw=1.0,
                    label=("pulse" if (i == 0 and not spans) else None))
    ax1.set_ylabel("tail_sum (rad)")
    if predictive:
        title = "Closed-loop session (predictive) — {} pulses: arm past {:g}, fire crossing {:g}".format(
            len(onsets),
            -direction * (opposite_threshold if opposite_threshold is not None else float("nan")),
            direction * (fire_level if fire_level is not None else float("nan")),
        )
    else:
        title = "Closed-loop session ({}) — {} pulses (tail_sum {} {:g})".format(
            pulse_mode or "fixed/level", len(onsets),
            ">" if direction >= 0 else "<", active_thr)
    ax1.set_title(title)
    ax1.legend(loc="upper right", fontsize=8)

    # bottom panel: output train (volts) or the triggered state
    if stimulus is not None:
        st = time_col(stimulus)
        ov = col_by_suffix(stimulus, "output_v")
        tr = col_by_suffix(stimulus, "triggered")
        if ov:
            ax2.plot(stimulus[st], stimulus[ov], drawstyle="steps-post", color="darkorange")
            ax2.set_ylabel("output (V)")
        elif tr:
            ax2.plot(stimulus[st], stimulus[tr], drawstyle="steps-post", color="darkorange")
            ax2.set_ylabel("triggered")
        for a, b in spans:
            ax2.axvspan(a, b, color="crimson", alpha=0.15)
    ax2.set_xlabel("time (s)")

    fig.tight_layout()
    fig.savefig(str(out_path), dpi=120)
    print("Saved plot -> {}".format(out_path))
    if show:
        plt.show()
    return len(onsets)


def main():
    ap = argparse.ArgumentParser(description="Plot tail_sum + pulses for a session.")
    ap.add_argument("session", nargs="?", help="session dir or a *_behavior_log.csv")
    ap.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="override the threshold line; default reads it from the session metadata",
    )
    ap.add_argument("--direction", type=int, default=None, choices=(1, -1))
    ap.add_argument(
        "--pulse-mode",
        default=None,
        choices=("fixed", "level", "predictive"),
        help="override the mode; default reads it from the session metadata",
    )
    ap.add_argument(
        "--opposite-threshold",
        type=float,
        default=None,
        help="predictive mode: override the opposite-bend (arm) level",
    )
    ap.add_argument(
        "--fire-level",
        type=float,
        default=None,
        help="predictive mode: override the level whose up-crossing fires",
    )
    ap.add_argument("--save", default=None, help="output PNG path")
    ap.add_argument("--no-show", action="store_true")
    args = ap.parse_args()

    session = args.session or default_session()
    if session is None:
        raise SystemExit(
            "No session given and none found under {}. Run an experiment or "
            "simulate_tracking.py first.".format(RECORDINGS)
        )

    beh, stim, sdir, prefix = find_logs(session)
    if beh is None and stim is None:
        raise SystemExit("No *_behavior_log.csv / *_stimulus_log.csv in {}".format(sdir))
    print("Session dir : {}".format(sdir))
    print("Behavior log: {}".format(beh))
    print("Stimulus log: {}".format(stim))

    # Threshold/direction: CLI override wins, else the value saved in the
    # session metadata, else a last-resort default (e.g. simulated logs).
    meta = stim_params_from_metadata(sdir, prefix)
    meta_thr = _meta_float(meta, "threshold")
    _raw_dir = _meta_float(meta, "direction")
    meta_dir = None if _raw_dir is None else (1 if _raw_dir >= 0 else -1)
    if args.threshold is not None:
        threshold, thr_src = args.threshold, "cli override"
    elif meta_thr is not None:
        threshold, thr_src = meta_thr, "session metadata"
    else:
        threshold, thr_src = 1.0, "default (no metadata found)"
    if args.direction is not None:
        direction = args.direction
    elif meta_dir is not None:
        direction = meta_dir
    else:
        direction = 1
    print("Threshold   : {:g} rad, direction {:+d}  [from {}]".format(
        threshold, direction, thr_src))

    # Predictive-mode levels: same precedence (CLI > metadata > unset).
    pulse_mode = args.pulse_mode or meta.get("pulse_mode")
    opposite_threshold = (
        args.opposite_threshold
        if args.opposite_threshold is not None
        else _meta_float(meta, "opposite_threshold")
    )
    fire_level = (
        args.fire_level
        if args.fire_level is not None
        else _meta_float(meta, "fire_level")
    )
    print("Pulse mode  : {}".format(pulse_mode or "unknown (no metadata)"))
    pulse_width_s = _meta_float(meta, "pulse_width_s")
    stim_delay_s = _meta_float(meta, "stim_delay_s")
    if pulse_width_s is not None and pulse_mode != "level":
        print("Pulse width : {:.0f} ms".format(pulse_width_s * 1000))
    if stim_delay_s:  # non-zero, non-None
        print("Stim delay  : {:.0f} ms after detection".format(stim_delay_s * 1000))
    baseline_mode = meta.get("baseline_mode")
    if baseline_mode and baseline_mode != "off":
        note = ""
        if baseline_mode == "fixed":
            bo = _meta_float(meta, "baseline_offset")
            note = " (offset {:+.3f} rad)".format(bo) if bo is not None else ""
        print("Baseline    : {} re-zero{}  [threshold applied to tail_sum - baseline]".format(
            baseline_mode, note))
    if pulse_mode == "predictive":
        print("Predictive  : arm at tail_sum {:g}, fire at tail_sum {:g}".format(
            -direction * (opposite_threshold if opposite_threshold is not None else float("nan")),
            direction * (fire_level if fire_level is not None else float("nan"))))

    behavior = load_csv(beh) if beh else None
    stimulus = load_csv(stim) if stim else None

    out_path = Path(args.save) if args.save else sdir / "session_plot.png"
    n = plot(
        behavior, stimulus, threshold, direction, out_path, not args.no_show,
        pulse_mode=pulse_mode,
        opposite_threshold=opposite_threshold,
        fire_level=fire_level,
    )
    print("{} pulse(s) marked.".format(n))


if __name__ == "__main__":
    main()
