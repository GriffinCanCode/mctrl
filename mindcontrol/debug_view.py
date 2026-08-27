"""Live overlay for tuning.

Most of the thresholds in `config.toml` are only meaningful against real numbers
from your own hands in your own lighting, so this window shows them: the
skeleton, the classified pose, the live pinch distance, and where gaze thinks you
are looking.

On macOS an OpenCV window must own the main thread, which the menu bar already
does, so the viewer runs in a separate process and is fed finished images. When
running headless (`--debug`) the main thread is free and drawing happens inline.
"""

from __future__ import annotations

import contextlib
import multiprocessing as mp
import queue

import cv2
import numpy as np

from .capture import Frame
from .geometry import SKELETON, Pose
from .pipeline import PipelineStatus

VIEW_WIDTH = 720
POSE_COLOURS = {
    Pose.READY: (120, 220, 255),
    Pose.FIST: (255, 170, 90),
    Pose.OPEN_PALM: (140, 240, 140),
    Pose.TELEPHONE: (220, 160, 255),
}
DEFAULT_COLOUR = (170, 170, 170)


def render(
    frame: Frame, hands: list, status: PipelineStatus, gaze: tuple[float, float] | None
) -> np.ndarray:
    """Draw one annotated frame, scaled down for cheap display."""
    image = frame.image
    scale = VIEW_WIDTH / max(image.shape[1], 1)
    canvas = cv2.resize(image, (VIEW_WIDTH, int(image.shape[0] * scale)))
    height, width = canvas.shape[:2]

    for fused in hands:
        features = fused.features
        colour = POSE_COLOURS.get(features.pose, DEFAULT_COLOUR)
        points = [(int(p[0] * width), int(p[1] * height)) for p in features.landmarks[:, :2]]
        for start, end in SKELETON:
            cv2.line(canvas, points[start], points[end], colour, 1, cv2.LINE_AA)
        for point in points:
            cv2.circle(canvas, point, 2, colour, -1, cv2.LINE_AA)

        anchor = (int(features.anchor[0] * width), int(features.anchor[1] * height))
        cv2.circle(canvas, anchor, 7, colour, 2, cv2.LINE_AA)
        label = f"{features.handedness} {features.pose.value} pinch {features.pinch:.2f}"
        if fused.merged:
            label += f" x{len(fused.cameras)}"
        cv2.putText(
            canvas,
            label,
            (anchor[0] - 60, max(anchor[1] - 16, 14)),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            colour,
            1,
            cv2.LINE_AA,
        )

    _draw_status(canvas, status)
    if gaze is not None:
        _draw_gaze_inset(canvas, gaze)
    return canvas


def _draw_status(canvas: np.ndarray, status: PipelineStatus) -> None:
    lines = [
        f"{status.fps:5.1f} fps   mode {status.mode}",
        f"gesture {status.gesture}",
        f"cameras {','.join(str(c) for c in status.cameras) or '-'}"
        f"{'  merged' if status.merged else ''}"
        f"   gaze {'ready' if status.gaze_ready else 'uncalibrated'}",
    ]
    cv2.rectangle(canvas, (0, 0), (canvas.shape[1], 14 + 20 * len(lines)), (20, 20, 20), -1)
    for index, text in enumerate(lines):
        cv2.putText(
            canvas,
            text,
            (10, 20 + index * 20),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.48,
            (230, 230, 230),
            1,
            cv2.LINE_AA,
        )
    for index, problem in enumerate(status.problems[:2]):
        cv2.putText(
            canvas,
            problem[:70],
            (10, canvas.shape[0] - 12 - index * 18),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.45,
            (120, 140, 255),
            1,
            cv2.LINE_AA,
        )


def _draw_gaze_inset(canvas: np.ndarray, gaze: tuple[float, float]) -> None:
    """Show gaze on a small proxy of the screen.

    Gaze is a screen coordinate, not a camera one, so plotting it on the video
    would put it somewhere meaningless. A miniature screen keeps it honest.
    """
    box_w, box_h = 150, 94
    x0, y0 = canvas.shape[1] - box_w - 12, 12
    cv2.rectangle(canvas, (x0, y0), (x0 + box_w, y0 + box_h), (70, 70, 70), 1)
    cv2.circle(
        canvas,
        (int(x0 + gaze[0] * box_w), int(y0 + gaze[1] * box_h)),
        5,
        (110, 230, 255),
        -1,
        cv2.LINE_AA,
    )
    cv2.putText(
        canvas, "gaze", (x0 + 4, y0 + box_h - 6), cv2.FONT_HERSHEY_SIMPLEX, 0.4, (90, 90, 90), 1
    )


def _viewer(frames: mp.Queue, title: str) -> None:
    """Child-process loop: show whatever arrives until told to stop."""
    while True:
        try:
            image = frames.get(timeout=0.5)
        except queue.Empty:
            if cv2.waitKey(1) & 0xFF == 27:
                break
            continue
        if image is None:
            break
        cv2.imshow(title, image)
        if cv2.waitKey(1) & 0xFF == 27:
            break
    cv2.destroyAllWindows()
    for _ in range(4):
        cv2.waitKey(1)


class DebugView:
    """An overlay window hosted in its own process."""

    def __init__(self, title: str = "mindcontrol") -> None:
        self._title = title
        self._context = mp.get_context("spawn")
        self._queue: mp.Queue | None = None
        self._process = None

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.is_alive()

    def open(self) -> None:
        if self.running:
            return
        self._queue = self._context.Queue(maxsize=1)
        self._process = self._context.Process(
            target=_viewer, args=(self._queue, self._title), daemon=True
        )
        self._process.start()

    def push(self, image: np.ndarray) -> None:
        """Offer a frame, dropping it if the viewer is still busy with the last one."""
        if not self.running or self._queue is None:
            return
        with contextlib.suppress(queue.Full):
            self._queue.put_nowait(image)

    def close(self) -> None:
        if self._queue is not None:
            with contextlib.suppress(queue.Full):
                self._queue.put_nowait(None)
        if self._process is not None:
            self._process.join(timeout=1.5)
            if self._process.is_alive():
                self._process.terminate()
            self._process = None
        self._queue = None
