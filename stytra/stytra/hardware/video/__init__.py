"""
Module to interact with video surces as cameras or video files. It also
implement video saving
"""

import numpy as np
from multiprocessing import Queue, Event
from queue import Empty, Full
from queue import Queue as ThreadQueue
import threading
from pathlib import Path
from lightparam import Param
from lightparam.param_qt import ParametrizedQt

from stytra.hardware.video.cameras.interface import CameraError
from stytra.utilities import FrameProcess
from arrayqueues.shared_arrays import IndexedArrayQueue
import flammkuchen as fl

from stytra.hardware.video.cameras import camera_class_dict

from stytra.hardware.video.write import VideoWriter, BufferVideoWriter

from stytra.hardware.video.ring_buffer import RingBuffer
import tempfile
import time
import datetime


class VideoSource(FrameProcess):
    """Abstract class for a process that generates frames, being it a camera
    or a file source. A maximum size of the memory used by the process can be
    set.

    **Input Queues:**

    self.control_queue :
        queue with control parameters for the source, e.g. from a
        :class:`CameraControlParameters <.interfaces.CameraControlParameters>`
        object.


    **Output Queues**

    self.frame_queue :
        TimestampedArrayQueue from the arrayqueues module
        where the frames read from the camera are sent.


    **Events**

    self.kill_signal :
        When set kill the process.


    Parameters
    ----------
    rotation : int
        n of times image should be rotated of 90 degrees
    max_mbytes_queue : int
        maximum size of camera queue (Mbytes)

    Returns
    -------

    """

    def __init__(self, rotation=False, max_mbytes_queue=200, n_consumers=1):
        """ """
        super().__init__(name="camera")
        self.rotation = rotation
        self.control_queue = Queue()
        self.frame_queue = IndexedArrayQueue(max_mbytes=max_mbytes_queue)
        self.kill_event = Event()
        self.n_consumers = 1
        self.state = None

    def put_frame(self, frame, messages):  # , timestamp, logiclevel):
        # If the queue is full, arrayqueues should print a warning!
        try:
            if self.frame_queue.queue.qsize() < self.n_consumers + 2:
                self.frame_queue.put(frame)  # , timestamp)
            else:
                messages.append("W:Dropped frame")
        except NotImplementedError:
            try:
                self.frame_queue.put(frame)  # , timestamp)
            except Full:
                messages.append("W:Dropped frame")
        self.update_framerate()


class CameraSource(VideoSource):
    """Process for controlling a camera.

    Cameras currently implemented:

    ======== ===========================================
    Ximea    Add some info
    Avt      Add some info
    ======== ===========================================

    Parameters
    ----------
    camera_type : str
        specifies type of the camera (currently supported: 'ximea', 'avt')
    downsampling : int
        specifies downsampling factor for the camera.

    Returns
    -------

    """

    """ dictionary listing classes used to instantiate camera object."""

    def __init__(
        self,
        camera_type,
        *args,
        downsampling=1,
        roi=(-1, -1, -1, -1),
        max_buffer_length=12000,
        camera_params=dict(),
        **kwargs
    ):
        """ """
        super().__init__(*args, **kwargs)

        self.cam = None

        self.camera_type = camera_type
        self.downsampling = downsampling
        self.roi = roi
        self.camera_params = camera_params

        self.max_buffer_length = max_buffer_length

        self.state = None
        self.ring_buffer = None

        # --- v2 non-blocking clip saving -------------------------------------
        # In v1 the "save buffer" encoded the whole ring buffer *inline* in this
        # acquisition loop (blocking cam.read() for seconds -> dropped frames and
        # a blind closed loop) and triggered itself via pyautogui screen scraping
        # of hardcoded F:/todelete/*.png buttons. v2 removes all of that:
        #  * saves are triggered by an edge on the `save_request_count` param,
        #    which the GUI button OR the stimulus can bump directly;
        #  * the actual encode runs on a background writer thread, so the
        #    acquisition loop keeps grabbing frames throughout;
        #  * on trigger we swap in a spare ring buffer (O(1)) and hand the full
        #    one to the writer, so acquisition never waits on a memcpy either.
        # These are created lazily inside run() (Windows uses spawn, so threads /
        # thread-queues must be built in the child process, not here).
        self._save_queue = None  # ThreadQueue of pending encode jobs
        self._spare_queue = None  # ThreadQueue of idle RingBuffers to reuse
        self._writer_thread = None
        self._served_saves = 0  # last save_request_count we acted on
        self._max_pending_saves = 2  # bound memory: queued clips awaiting encode
        self._max_spare_buffers = 2  # idle buffers kept for reuse
        self._clip_kbit_rate = 10000  # encode quality for saved clips

    def retrieve_params(self, messages):
        while True:
            try:
                param_dict = self.control_queue.get(timeout=0.0001)
                self.state.params.values = param_dict
                for param, value in param_dict.items():
                    ms = self.cam.set(param, value)
                    try:
                        messages.extend(list(ms))
                    except TypeError:
                        pass
            except Empty:
                break

    def run(self):
        """
        After initializing the camera, the process constantly does the
        following:

            - read control parameters from the control_queue and set them;
            - read frames from the camera and put them in the frame_queue.


        """
        if self.state is None:
            self.state = CameraControlParameters()
        try:
            CameraClass = camera_class_dict[self.camera_type]
            self.cam = CameraClass(
                downsampling=self.downsampling, roi=self.roi, **self.camera_params
            )
        except KeyError:
            raise Exception("{} is not a valid camera type!".format(self.camera_type))
        camera_messages = list(self.cam.open_camera())
        [self.message_queue.put(m) for m in camera_messages]
        self._start_writer_thread()
        prt = None
        while not self.kill_event.is_set():
            # Try to get new parameters from the control queue:
            messages = []
            if self.control_queue is not None:
                self.retrieve_params(messages)
            # Grab the new frame, and put it in the queue if valid:
            try:
                arr, timepoint, logic = self.cam.read()
                timestamp = datetime.datetime.fromtimestamp(timepoint / 1e9)
            except CameraError:
                pass

            if self.rotation:
                arr = np.rot90(arr, self.rotation)

            res_len = int(round(self.state.framerate * self.state.ring_buffer_length))
            if res_len > self.max_buffer_length:
                res_len = self.max_buffer_length
                self.message_queue.put(
                    "W:Replay buffer too big, make the plot"
                    " time range smaller for full replay"
                    " capabilities"
                )

            if self.ring_buffer is None or res_len != self.ring_buffer.length:
                self.ring_buffer = RingBuffer(res_len)

            # --- v2: edge-triggered, non-blocking clip save ------------------
            # A rising `save_request_count` (bumped by the GUI "save clip" button
            # or by the stimulus on a detected behaviour) schedules exactly one
            # clip. All the heavy work (memcpy + encode) happens off this loop:
            # here we only do an O(1) ring-buffer swap and enqueue a job, so
            # cam.read() keeps running and no frames are dropped.
            if self.state.save_request_count > self._served_saves:
                self._served_saves = self.state.save_request_count
                self._schedule_clip_save(res_len)

            if self.state.paused:
                self.message_queue.put(
                    "I:Ring_buffer_size:" + str(self.ring_buffer.length)
                )
                if self.ring_buffer.arr is not None:
                    self.frame_queue.put(self.ring_buffer.get_most_recent())
                else:
                    self.message_queue.put("E:camera paused before any frames acquired")
                prt = None
            elif self.state.replay and self.state.replay_fps > 0:
                messages.append(
                    "I:Replaying between {} and {} of {}".format(
                        *self.state.replay_limits, self.ring_buffer.length
                    )
                )
                old_fps = self.framerate_rec.current_framerate
                if old_fps is not None:
                    self.ring_buffer.replay_limits = (
                        int(round(self.state.replay_limits[0] * old_fps)),
                        int(round(self.state.replay_limits[1] * old_fps)),
                    )
                try:
                    self.frame_queue.put(self.ring_buffer.get())
                except ValueError:
                    pass
                delta_t = 1 / self.state.replay_fps
                if prt is not None:
                    extrat = delta_t - (time.process_time() - prt)
                    if extrat > 0:
                        time.sleep(extrat)
                prt = time.process_time()
            else:
                prt = None
                if arr is not None:
                    try:
                        self.ring_buffer.put(arr, timepoint, logic)
                    except AttributeError:
                        pass
                    self.put_frame(arr, messages)  # , timestamp, logic) #
            for m in messages:
                self.message_queue.put(m)

        # Loop exited: flush/stop the background writer, then release the camera.
        self._stop_writer_thread()
        self.cam.release()

    # ----------------------------------------------------------- v2 helpers --
    def _start_writer_thread(self):
        """Spawn the background clip-encoder thread once, inside this process
        (spawn-safe: queues/threads must be created in the child, not __init__)."""
        if self._writer_thread is not None:
            return
        self._save_queue = ThreadQueue(maxsize=self._max_pending_saves)
        self._spare_queue = ThreadQueue(maxsize=self._max_spare_buffers)
        self._writer_thread = threading.Thread(
            target=self._writer_loop, name="clip_writer", daemon=True
        )
        self._writer_thread.start()

    def _stop_writer_thread(self):
        """Ask the writer to finish queued clips and exit (called at loop end)."""
        if self._writer_thread is None:
            return
        try:
            self._save_queue.put(None, timeout=1.0)
        except Full:
            pass
        self._writer_thread.join(timeout=30.0)
        self._writer_thread = None

    def _get_spare_buffer(self, length):
        """An empty RingBuffer of `length` to keep acquiring into while the full
        one is encoded. Reuse a pooled buffer of the right size, else allocate a
        fresh one (its array is allocated lazily on the first put)."""
        while True:
            try:
                b = self._spare_queue.get_nowait()
            except Empty:
                return RingBuffer(length)
            if b.length == length:
                b.reset()
                return b
            # wrong size (framerate/length changed since it was pooled): drop it

    def _schedule_clip_save(self, length):
        """O(1) hand-off: swap in a spare buffer and queue the full one for the
        writer thread. Never blocks the acquisition loop on encode or memcpy."""
        full = self.ring_buffer
        if full is None or full.arr is None:
            self.message_queue.put("E:clip save requested before any frames acquired")
            return
        spare = self._get_spare_buffer(length)
        # Anchor to bridge the camera hardware clock (per-frame timepoints) to
        # the host wall/monotonic clocks used by the behaviour + pulse logs.
        anchor = dict(host_time=time.time(), monotonic=time.monotonic())
        # Prefer the basename the main process picked (so the video pairs up with
        # its behaviour/stimulus log snapshots); fall back to an HHMMSS_ stamp.
        basename = getattr(self.state, "save_clip_basename", "") or ""
        if basename:
            fname = basename
        else:
            stamp = datetime.datetime.now().strftime("%H%M%S")
            fname = str(Path(self.state.save_direc).joinpath(stamp + "_"))
        # Save only the last `save_clip_seconds` worth of frames (0 -> whole
        # buffer); the writer thread slices the newest n_frames.
        fps = int(round(self.state.framerate))
        clip_s = getattr(self.state, "save_clip_seconds", 0.0) or 0.0
        n_frames = int(round(clip_s * self.state.framerate)) if clip_s > 0 else 0
        job = (full, fname, fps, anchor, n_frames)
        try:
            self._save_queue.put_nowait(job)
        except Full:
            # Writer still busy: keep the history (revert the swap) and warn,
            # rather than dropping frames silently.
            self.message_queue.put(
                "W:Clip save skipped -- writer busy ({} clips already queued)".format(
                    self._max_pending_saves
                )
            )
            try:
                self._spare_queue.put_nowait(spare)
            except Full:
                pass
            return
        self.ring_buffer = spare
        self.message_queue.put("I:Clip save queued ({} frames)".format(full.length))

    def _writer_loop(self):
        """Background thread: encode queued ring-buffer snapshots to disk. PyAV
        releases the GIL while encoding, so the acquisition loop runs unimpeded."""
        while True:
            try:
                job = self._save_queue.get(timeout=0.25)
            except Empty:
                if self.kill_event.is_set():
                    break
                continue
            if job is None:
                break
            full, fname, fps, anchor, n_frames = job
            try:
                frames = full.get_all()
                times, logic = full.get_all_meta()
                # Keep only the newest n_frames (windowed save); 0 = whole buffer.
                if n_frames and 0 < n_frames < len(frames):
                    frames = frames[-n_frames:]
                    times = times[-n_frames:]
                    logic = logic[-n_frames:]
                writer = BufferVideoWriter(
                    input_queue=frames,
                    meta_queue=(times, logic),
                    output_framerate=fps,
                    kbit_rate=self._clip_kbit_rate,
                )
                writer.filename_queue.put(fname)
                writer.run()
                self._write_clocksync(fname, anchor, times)
                self.message_queue.put("I:Clip saved -> " + fname + "video.mp4")
            except Exception as e:
                self.message_queue.put("E:Clip save failed: {!r}".format(e))
            finally:
                full.reset()
                try:
                    self._spare_queue.put_nowait(full)
                except Full:
                    pass  # pool already full: let this buffer be GC'd

    def _write_clocksync(self, fname, anchor, times):
        """One-row CSV bridging the camera hardware clock to the host clocks, so
        a saved clip aligns to the behaviour/LabJack-pulse logs after the fact."""
        try:
            newest = float(times[-1]) if len(times) else float("nan")
            with open(str(fname) + "clocksync.csv", "w") as f:
                f.write("newest_frame_timepoint_ns,host_time_s,monotonic_s\n")
                f.write(
                    "{:.0f},{:.6f},{:.6f}\n".format(
                        newest, anchor["host_time"], anchor["monotonic"]
                    )
                )
        except Exception as e:
            self.message_queue.put("W:clocksync write failed: {!r}".format(e))


class VideoFileSource(VideoSource):
    """A class to stream videos from a file to test parts of
    stytra without a camera available, or do offline analysis

    Parameters
    ----------
        source_file
            path of the video file
        loop : bool
            continue video from the beginning if the end is reached

    Returns
    -------

    """

    def __init__(self, source_file=None, loop=True, **kwargs):
        super().__init__(**kwargs)
        self.source_file = source_file
        self.loop = loop
        self.state = None
        self.offset = 0
        self.paused = False
        self.old_frame = None
        self.offset = 0

    def inner_loop(self):
        pass

    def update_params(self):
        while True:
            try:
                param_dict = self.control_queue.get(timeout=0.0001)
                self.state.params.values = param_dict
            except Empty:
                break

    def run(self):
        if self.state is None:
            self.state = VideoControlParameters()
        if self.source_file.endswith("h5") or self.source_file.endswith("hdf5"):
            framedata = fl.load(self.source_file)

            if isinstance(framedata, np.ndarray):
                frames = framedata
            else:
                frames = framedata["video"]

            i_frame = self.offset
            prt = None
            while not self.kill_event.is_set():
                messages = []
                # Try to get new parameters from the control queue:
                message = ""
                if self.control_queue is not None:
                    self.update_params()

                # we adjust the framerate
                delta_t = 1 / self.state.framerate
                if prt is not None:
                    extrat = delta_t - (time.process_time() - prt)
                    if extrat > 0:
                        time.sleep(extrat)

                self.put_frame(frames[i_frame, :, :], messages)

                if not self.state.paused:
                    i_frame += 1

                if i_frame == frames.shape[0]:
                    if self.loop:
                        i_frame = self.offset
                    else:
                        break

                for m in messages:
                    self.message_queue.put(m)
                prt = time.process_time()

        else:
            import av

            container = av.open(self.source_file)
            container.streams.video[0].thread_type = "AUTO"
            container.streams.video[0].thread_count = 1

            prt = None
            # Honor kill_event so the process exits on close. Otherwise this
            # infinite decode loop ignores it and camera.join() in wrap_up hangs
            # forever -- which is why only SIMULATION hangs on close (a real
            # CameraSource already checks kill_event and exits cleanly).
            while self.loop and not self.kill_event.is_set():
                for framedata in container.decode(video=0):
                    if self.kill_event.is_set():
                        break
                    messages = []
                    if self.paused:
                        frame = self.old_frame
                    else:
                        frame = framedata.to_ndarray(format="rgb24")

                    # adjust the frame rate by adding extra time if the processing
                    # is quicker than the specified framerate

                    if self.control_queue is not None:
                        self.update_params()

                    delta_t = 1 / self.state.framerate
                    if prt is not None:
                        extrat = delta_t - (time.process_time() - prt)
                        if extrat > 0:
                            time.sleep(extrat)

                    self.put_frame(frame[:, :, 0], messages)

                    prt = time.process_time()
                    self.old_frame = frame

                    for m in messages:
                        self.message_queue.put(m)

                container.seek(0)  # loop back to start (PyAV >=9 dropped 'whence')

            container.close()
            return


class VideoControlParameters(ParametrizedQt):
    def __init__(self, **kwargs):
        super().__init__(name="video_params", **kwargs)
        self.framerate = Param(
            200.0, limits=(10, 700), unit="Hz", desc="Framerate (Hz)"
        )
        self.offset = Param(50)
        self.paused = Param(False)


class CameraControlParameters(ParametrizedQt):
    """HasPyQtGraphParams class for controlling the camera params.
    Ideally, methods to automatically set dynamic boundaries on frame rate and
    exposure time can be implemented. Currently not implemented.

    Parameters
    ----------

    Returns
    -------

    """

    def __init__(self, **kwargs):
        super().__init__(name="camera_params", **kwargs)
        self.exposure = Param(4.0, limits=(0.1, 1000), unit="ms", desc="Exposure (ms)")
        self.framerate = Param(
            200.0, limits=(1, 700), unit=" Hz", desc="Framerate (Hz)"
        )
        self.gain = Param(10.0, limits=(0.1, 12), desc="Camera amplification gain")
        self.ring_buffer_length = Param(
            1200, (1, 2000), desc="Rolling buffer that saves the last items"
        )
        self.paused = Param(False)
        self.replay = Param(False, desc="Replaying")
        self.replay_fps = Param(
            200,
            (0, 500),
            desc="If bigger than 0, the rolling buffer will be replayed at the given framerate",
        )
        self.replay_limits = Param((0, 1200), gui=False)
        # v2: monotonic counter -- each increment (from the GUI "save clip"
        # button or the stimulus) triggers exactly one non-blocking clip save.
        # Robust to param-sync coalescing in a way a True/False pulse is not.
        self.save_request_count = Param(0, gui=False)
        self.save_direc = Param(tempfile.gettempdir(), gui="text")
        # v2: shared basename for a clip, set by the main process so the video
        # (…video.mp4 / …video_times.csv / …clocksync.csv) pairs up by filename
        # with the behaviour/stimulus log snapshots it writes. Empty -> the
        # camera process falls back to its own HHMMSS_ stamp.
        self.save_clip_basename = Param("", gui=False)
        # v2: how many seconds of the rolling buffer to save (set by the main
        # process = min(time since last save, buffer length)). <= 0 -> whole
        # buffer. Keeps consecutive saves back-to-back and non-overlapping.
        self.save_clip_seconds = Param(0.0, gui=False)
