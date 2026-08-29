"""Multi-camera capture.

Each camera runs on its own thread and keeps only its newest frame. Dropping
stale frames rather than queueing them is deliberate: a pointer built on
two-second-old video is worse than useless, so latency is protected at the cost
of throughput.

Adding a camera is a config edit -- ``devices = [0, 1]`` -- and nothing
downstream changes, because everything consumes the same ``Frame`` shape.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from ..config import CameraConfig


@dataclass(frozen=True)
class Frame:
    """One image from one camera, stamped on arrival."""

    camera_id: int
    image: np.ndarray
    timestamp_ms: int
    sequence: int


class CameraWorker:
    """Grabs frames from a single device into a latest-only slot."""

    def __init__(self, camera_id: int, cfg: CameraConfig) -> None:
        self.camera_id = camera_id
        self._cfg = cfg
        self._capture: cv2.VideoCapture | None = None
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._frame: Frame | None = None
        self._stop = threading.Event()
        self._sequence = 0
        self.error: str | None = None

    def open(self) -> bool:
        capture = cv2.VideoCapture(self.camera_id, cv2.CAP_AVFOUNDATION)
        if not capture.isOpened():
            capture.release()
            self.error = f"camera {self.camera_id} could not be opened"
            return False
        capture.set(cv2.CAP_PROP_FRAME_WIDTH, self._cfg.width)
        capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self._cfg.height)
        capture.set(cv2.CAP_PROP_FPS, self._cfg.fps)
        # A one-frame device buffer keeps the newest image close to real time.
        capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        self._capture = capture
        self.error = None
        return True

    def start(self) -> bool:
        if self._capture is None and not self.open():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"camera-{self.camera_id}", daemon=True
        )
        self._thread.start()
        return True

    def _run(self) -> None:
        assert self._capture is not None
        failures = 0
        while not self._stop.is_set():
            ok, image = self._capture.read()
            if not ok or image is None:
                failures += 1
                if failures > 60:
                    self.error = f"camera {self.camera_id} stopped delivering frames"
                    return
                time.sleep(0.01)
                continue
            failures = 0
            if self._cfg.mirror:
                # Mirror so the debug view reads like a mirror and your hand and
                # the cursor travel the same direction.
                image = cv2.flip(image, 1)
            self._sequence += 1
            frame = Frame(
                camera_id=self.camera_id,
                image=image,
                timestamp_ms=int(time.monotonic() * 1000),
                sequence=self._sequence,
            )
            with self._lock:
                self._frame = frame

    def latest(self) -> Frame | None:
        with self._lock:
            return self._frame

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.5)
            self._thread = None
        if self._capture is not None:
            self._capture.release()
            self._capture = None
        with self._lock:
            self._frame = None


class CameraBank:
    """A set of cameras addressed as one source."""

    def __init__(self, cfg: CameraConfig) -> None:
        self._cfg = cfg
        self.workers: dict[int, CameraWorker] = {}

    @property
    def primary_id(self) -> int:
        """Device trusted for gaze; falls back to any live camera."""
        if self._cfg.primary_gaze in self.workers:
            return self._cfg.primary_gaze
        return next(iter(self.workers), self._cfg.primary_gaze)

    def start(self) -> list[str]:
        """Start every configured camera, returning messages for the ones that failed."""
        problems: list[str] = []
        for camera_id in self._cfg.devices:
            worker = CameraWorker(camera_id, self._cfg)
            if worker.start():
                self.workers[camera_id] = worker
            else:
                problems.append(worker.error or f"camera {camera_id} unavailable")
        return problems

    def latest(self) -> dict[int, Frame]:
        """Newest frame per camera, skipping cameras that have not delivered yet."""
        frames: dict[int, Frame] = {}
        for camera_id, worker in self.workers.items():
            frame = worker.latest()
            if frame is not None:
                frames[camera_id] = frame
        return frames

    def failures(self) -> list[str]:
        return [w.error for w in self.workers.values() if w.error]

    def stop(self) -> None:
        for worker in self.workers.values():
            worker.stop()
        self.workers.clear()

    def __len__(self) -> int:
        return len(self.workers)
