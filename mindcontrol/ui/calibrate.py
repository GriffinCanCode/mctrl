"""Nine-point gaze calibration.

Runs as its own process, for two reasons: a fullscreen OpenCV window has to own
the main thread, and the camera can only be held by one process at a time, so the
menu-bar app releases its cameras and hands them over for the duration.

You look at each dot; the app records what your eyes and head look like while you
do. Fitting those pairs gives the mapping from appearance to screen position that
`GazeModel` uses from then on. The result is written to disk, and the app reloads
it when it takes the camera back.
"""

from __future__ import annotations

import sys
import time

import cv2
import numpy as np

from ..camera.capture import CameraWorker
from ..config import GAZE_MODEL_PATH, Config, load
from ..control.mouse import main_display_bounds
from ..tracking.gaze import GazeModel, GazeTracker

WINDOW = "mindcontrol calibration"
# Inset from the edges: a dot in the very corner is uncomfortable to fixate and
# tends to be tracked with the head rather than the eyes.
GRID = (0.08, 0.5, 0.92)
TARGETS = [(x, y) for y in GRID for x in GRID]

SETTLE_S = 1.0
SAMPLES_PER_TARGET = 30
SAMPLE_TIMEOUT_S = 4.0
MIN_SAMPLES_PER_TARGET = 8


def _canvas(width: int, height: int) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


def _draw_target(
    frame: np.ndarray, target: tuple[float, float], progress: float, phase: str
) -> None:
    height, width = frame.shape[:2]
    cx, cy = int(target[0] * width), int(target[1] * height)

    # The ring closing in on the dot shows how much longer to hold still.
    radius = int(46 - 26 * progress)
    colour = (90, 210, 120) if phase == "recording" else (120, 120, 120)
    cv2.circle(frame, (cx, cy), max(radius, 12), colour, 2, cv2.LINE_AA)
    cv2.circle(frame, (cx, cy), 6, (255, 255, 255), -1, cv2.LINE_AA)


def _draw_caption(frame: np.ndarray, lines: list[str]) -> None:
    height, width = frame.shape[:2]
    for index, text in enumerate(lines):
        size = cv2.getTextSize(text, cv2.FONT_HERSHEY_SIMPLEX, 0.7, 1)[0]
        cv2.putText(
            frame,
            text,
            ((width - size[0]) // 2, int(height * 0.86) + index * 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.7,
            (170, 170, 170),
            1,
            cv2.LINE_AA,
        )


def run(cfg: Config | None = None) -> int:
    """Drive the calibration. Returns a process exit code."""
    cfg = cfg or load()
    # Sized to the main display, matching the fractions `Mouse.move_to_fraction`
    # will later interpret. Calibrating against the desktop union would record
    # targets for a coordinate space the fullscreen window never covered.
    left, top, right, bottom = main_display_bounds()
    width, height = int(right - left), int(bottom - top)

    camera = CameraWorker(cfg.cameras.primary_gaze, cfg.cameras)
    if not camera.start():
        print(f"[calibrate] {camera.error}", file=sys.stderr)
        return 2

    tracker = GazeTracker(cfg.tracking)
    cv2.namedWindow(WINDOW, cv2.WINDOW_NORMAL)
    cv2.setWindowProperty(WINDOW, cv2.WND_PROP_FULLSCREEN, cv2.WINDOW_FULLSCREEN)

    features: list[np.ndarray] = []
    targets: list[tuple[float, float]] = []
    aborted = False

    try:
        # Let auto-exposure settle before the first dot, or the first point is
        # collected from a half-dark frame.
        _wait_for_camera(camera, tracker, width, height)

        for index, target in enumerate(TARGETS):
            collected = _collect_point(camera, tracker, cfg, target, index, width, height)
            if collected is None:
                aborted = True
                break
            if len(collected) < MIN_SAMPLES_PER_TARGET:
                print(
                    f"[calibrate] only {len(collected)} usable samples for point "
                    f"{index + 1}; keeping them but the fit will be weaker"
                )
            features.extend(collected)
            targets.extend([target] * len(collected))
    finally:
        cv2.destroyAllWindows()
        # macOS needs a few event-loop turns to actually tear the window down.
        for _ in range(4):
            cv2.waitKey(1)
        tracker.close()
        camera.stop()

    if aborted:
        print("[calibrate] cancelled; existing calibration left untouched")
        return 1
    if len(features) < len(TARGETS) * MIN_SAMPLES_PER_TARGET:
        print("[calibrate] not enough usable samples; nothing saved", file=sys.stderr)
        return 3

    model = GazeModel.fit(np.vstack(features), np.array(targets, dtype=np.float64))
    model.save(GAZE_MODEL_PATH)
    print(
        f"[calibrate] saved {GAZE_MODEL_PATH} from {len(features)} samples; "
        f"mean error {model.quality * 100:.1f}% of screen"
    )
    return 0


def _wait_for_camera(camera: CameraWorker, tracker: GazeTracker, width: int, height: int) -> None:
    deadline = time.monotonic() + 2.5
    while time.monotonic() < deadline:
        frame = camera.latest()
        if frame is not None:
            tracker.process(frame)
        canvas = _canvas(width, height)
        _draw_caption(
            canvas,
            [
                "Look at each dot until its ring closes.",
                "Keep your head still and comfortable. Esc cancels.",
            ],
        )
        cv2.imshow(WINDOW, canvas)
        if cv2.waitKey(30) & 0xFF == 27:
            return


def _collect_point(
    camera: CameraWorker,
    tracker: GazeTracker,
    cfg: Config,
    target: tuple[float, float],
    index: int,
    width: int,
    height: int,
) -> list[np.ndarray] | None:
    """Show one dot and gather samples. None means the user pressed Escape."""
    samples: list[np.ndarray] = []
    started = time.monotonic()
    last_sequence = -1

    while True:
        elapsed = time.monotonic() - started
        recording = elapsed >= SETTLE_S
        if recording:
            frame = camera.latest()
            if frame is not None and frame.sequence != last_sequence:
                last_sequence = frame.sequence
                observation = tracker.process(frame)
                # Blinks and lost faces are skipped rather than averaged in;
                # a closed eye says nothing about where you are looking.
                if observation.usable and observation.openness >= cfg.gaze.blink_ear:
                    assert observation.features is not None
                    samples.append(observation.features)
            if len(samples) >= SAMPLES_PER_TARGET or elapsed > SETTLE_S + SAMPLE_TIMEOUT_S:
                return samples
            progress = len(samples) / SAMPLES_PER_TARGET
        else:
            progress = elapsed / SETTLE_S

        canvas = _canvas(width, height)
        _draw_target(canvas, target, min(progress, 1.0), "recording" if recording else "settle")
        _draw_caption(canvas, [f"Point {index + 1} of {len(TARGETS)}"])
        cv2.imshow(WINDOW, canvas)
        if cv2.waitKey(15) & 0xFF == 27:
            return None


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
