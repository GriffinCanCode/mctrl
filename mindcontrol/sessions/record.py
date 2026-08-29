"""Guided capture of a labelled gesture session.

Walks you through each pose in turn and records what your hands actually measure
while you hold it. The prompt is the label, so the resulting file is supervised
data: not just "here are some hand shapes" but "here are 150 frames that were
definitely meant to be a fist".

Two prompts are deliberately about *transitions* rather than poses -- pinching
repeatedly, and swiping -- because clicks and swipes are events in time, and a
threshold fitted only to held poses would have nothing to say about them.

Every configured camera is recorded, not just the primary, so a replay can drive
the fusion path with genuine cross-viewpoint disagreement rather than the same
image twice.

Runs in the foreground: it owns the cameras and a preview window, and it wants
your attention for about a minute.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from ..camera.capture import CameraBank, Frame
from ..config import Config, load
from ..gestures.geometry import SKELETON, HandFeatures
from ..logs import muffled
from ..tracking.hands import HandTracker
from .store import SESSIONS_DIR, RecordedFrame, RecordedHand, RecordedView, SessionWriter

# How long to wait for every camera to produce its first frame before starting.
# Generous because a phone joined over Continuity has been measured taking most of
# five seconds, and timing it out would silently record without it.
WARM_TIMEOUT_S = 12.0

WINDOW = "mindcontrol recorder"


@dataclass(frozen=True)
class Prompt:
    label: str
    seconds: float
    instruction: str
    detail: str = ""


# Order matters: the baseline comes first so that "no hand" is on record, and the
# unambiguous held poses come before the fiddly transitions.
#
# ONE HAND ONLY, throughout. A second hand resting in frame is not idle data --
# it is a different pose wearing the same label, and it corrupts every threshold
# fitted from that prompt. The instructions say so repeatedly on purpose.
#
# Prompts that drive a gesture the app reads from *motion* -- scrolling with a
# fist, swiping with a palm -- ask for that motion. A held-still fist records the
# shape but says nothing about whether scrolling works.
SCRIPT: tuple[Prompt, ...] = (
    Prompt(
        "none",
        3.0,
        "Both hands right out of frame",
        "Drop them in your lap. Nothing visible at all.",
    ),
    Prompt(
        "ready",
        5.0,
        "ONE hand: the READY pose",
        "Index and thumb out, relaxed. Drift around slowly. Other hand away.",
    ),
    Prompt(
        "pinch_closed",
        5.0,
        "ONE hand: thumb to INDEX, other THREE fingers STAYING OUT",
        "Do not ball your hand up -- a curled pinch is a fist and scrolls instead.",
    ),
    Prompt(
        "pinch_cycle",
        9.0,
        "ONE hand: QUICK taps, thumb to index, three fingers OUT",
        "Shut and open immediately. A pinch held over ~1/4 second is a drag.",
    ),
    Prompt(
        "pinch_middle_closed",
        5.0,
        "ONE hand: pinch thumb to MIDDLE finger",
        "Curl ring and little finger in too. Other hand away.",
    ),
    Prompt(
        "fist",
        6.0,
        "ONE hand: fist, and scroll it up and down",
        "Thumb wrapped in. Move as if dragging a page. Other hand away.",
    ),
    Prompt(
        "open_palm",
        5.0,
        "ONE hand: open palm at the camera, still",
        "All five fingers out, facing the lens. Hold it. Other hand away.",
    ),
    Prompt(
        "telephone",
        4.0,
        "ONE hand: thumb and little finger out",
        "The 'call me' hand. Other hand away.",
    ),
    Prompt(
        "swipe",
        8.0,
        "ONE hand: open palm, sweep left and right, palm FLAT to the lens",
        "Brisk sweeps, fingers spread the whole way. Do not let the palm rotate.",
    ),
)

COUNTDOWN_S = 2.0

# Subsets worth recording on their own, when the full script already worked for
# everything else and one gesture needs another attempt.
#
# Each group carries the prompts its fit *depends on*, not just the failing one.
# A threshold is a boundary between two clusters, so re-recording only the low
# side leaves nothing to separate it from: fitting `pinch_close` needs open hands
# to contrast against, and a pinch alone would simply be declined.
FOCUS: dict[str, tuple[str, ...]] = {
    # A fist is in the pinch group because `pinch_close` has to sit below it, not
    # just below an open hand: a fist measures as pinched, and a pinch that fires
    # on the way into one suppresses scrolling for the rest of the gesture.
    "pinch": ("none", "ready", "open_palm", "fist", "pinch_closed", "pinch_cycle"),
    "swipe": ("none", "open_palm", "swipe"),
    "poses": ("none", "ready", "fist", "open_palm", "telephone"),
}


def select(focus: tuple[str, ...] | None) -> tuple[Prompt, ...]:
    """The prompts to run, in script order, for the named focus groups."""
    if not focus:
        return SCRIPT
    unknown = set(focus) - set(FOCUS)
    if unknown:
        raise ValueError(
            f"no such focus: {', '.join(sorted(unknown))}; try {', '.join(sorted(FOCUS))}"
        )
    wanted = {label for name in focus for label in FOCUS[name]}
    return tuple(prompt for prompt in SCRIPT if prompt.label in wanted)


class Rig:
    """Every configured camera, each with its own tracker."""

    def __init__(self, cfg: Config) -> None:
        self.bank = CameraBank(cfg.cameras)
        self.problems = self.bank.start()
        # Muffled because this is a guided session: the user has to read the
        # prompts, and MediaPipe prints six lines of startup trivia per camera.
        with muffled():
            self.trackers = {
                camera_id: HandTracker(cfg.tracking, cfg.gestures, cfg.cameras.mirror)
                for camera_id in self.bank.workers
            }
        self._seen: dict[int, int] = {}

    def __len__(self) -> int:
        return len(self.bank)

    def warm(self, timeout_s: float = WARM_TIMEOUT_S) -> list[int]:
        """Wait for every camera to deliver a frame, priming the trackers.

        A camera that is still waking contributes no views, and frames recorded
        during that window are indistinguishable, later, from a camera that saw
        nothing -- so the baseline prompt would inherit a fault that was really
        just a cold start. USB and Continuity cameras are the slow ones.

        Priming here also absorbs MediaPipe's remaining log line, which comes from
        its first inference rather than from construction and would otherwise land
        on top of the first prompt.

        Returns the cameras that never woke.
        """
        deadline = time.monotonic() + timeout_s
        awake: set[int] = set()
        with muffled():
            while time.monotonic() < deadline and len(awake) < len(self.trackers):
                frames, _, _ = self.read()
                awake |= frames.keys()
                time.sleep(0.02)
        return sorted(set(self.trackers) - awake)

    def read(self) -> tuple[dict[int, Frame], dict[int, list[HandFeatures]], bool]:
        """Latest frame and measured hands per camera.

        ``fresh`` reports whether any camera produced a new image, so the caller
        can redraw the preview continuously while only recording real frames.
        """
        frames = self.bank.latest()
        fresh = False
        for camera_id, frame in frames.items():
            if self._seen.get(camera_id) != frame.sequence:
                self._seen[camera_id] = frame.sequence
                fresh = True
        hands = {
            camera_id: self.trackers[camera_id].process(frame)
            for camera_id, frame in frames.items()
            if camera_id in self.trackers
        }
        return frames, hands, fresh

    def views(self, frames: dict[int, Frame], hands: dict[int, list[HandFeatures]]):
        """Turn one instant into recordable per-camera views."""
        if not frames:
            return []
        newest = max(frame.timestamp_ms for frame in frames.values())
        return [
            RecordedView(
                camera_id=camera_id,
                age_ms=float(newest - frames[camera_id].timestamp_ms),
                hands=[
                    RecordedHand(
                        handedness=hand.handedness,
                        seen_handedness=hand.seen_handedness,
                        score=hand.score,
                        world=hand.world,
                        image=hand.landmarks,
                    )
                    for hand in found
                ],
            )
            for camera_id, found in hands.items()
        ]

    def close(self) -> None:
        # Muffled too: closing a graph is when MediaPipe's telemetry uploader
        # gives up trying to reach Google, once per camera, at error level. Those
        # lines land on top of the "wrote N frames" line that matters.
        with muffled():
            for tracker in self.trackers.values():
                tracker.close()
        self.bank.stop()


def run(
    cfg: Config | None = None,
    out: Path | None = None,
    note: str = "",
    focus: tuple[str, ...] | None = None,
) -> int:
    """Record a session. Returns a process exit code."""
    cfg = cfg or load()
    try:
        script = select(focus)
    except ValueError as bad:
        print(f"[record] {bad}", file=sys.stderr)
        return 2
    rig = Rig(cfg)
    for problem in rig.problems:
        print(f"[record] {problem}", file=sys.stderr)
    if not len(rig):
        print("[record] no cameras available", file=sys.stderr)
        rig.close()
        return 2

    print(f"[record] waking {len(rig)} camera(s)...", flush=True)
    for camera_id in rig.warm():
        print(
            f"[record] camera {camera_id} never woke; recording without it",
            file=sys.stderr,
        )

    print(f"[record] recording from camera(s) {sorted(rig.trackers)}")
    path = out or SESSIONS_DIR / f"session-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    cv2.namedWindow(WINDOW, cv2.WINDOW_AUTOSIZE)

    aborted = False
    started = time.monotonic()
    frames = 0
    try:
        with SessionWriter(path, note=note) as writer:
            for index, prompt in enumerate(script):
                if not _countdown(rig, prompt, index, len(script)) or not _capture(
                    rig, prompt, index, len(script), writer, started
                ):
                    aborted = True
                    break
            frames = writer.frames
    finally:
        cv2.destroyAllWindows()
        for _ in range(4):
            cv2.waitKey(1)
        rig.close()

    if aborted:
        print(f"[record] cancelled; partial session kept at {path}")
        return 1
    print(f"[record] wrote {frames} frames to {path}")
    print("[record] next: mindcontrol autotune")
    return 0


TILE_WIDTH = 640


def _tile(frame: Frame, hands: list[HandFeatures], colour, camera_id: int):
    """One camera's frame, annotated with its own skeletons and readout."""
    image = frame.image
    canvas = cv2.resize(image, (TILE_WIDTH, int(image.shape[0] * TILE_WIDTH / image.shape[1])))
    height, width = canvas.shape[:2]

    for hand in hands:
        points = [(int(p[0] * width), int(p[1] * height)) for p in hand.landmarks[:, :2]]
        for a, b in SKELETON:
            cv2.line(canvas, points[a], points[b], colour, 1, cv2.LINE_AA)

    label = f"cam {camera_id}"
    if hands:
        first = hands[0]
        label += (
            f"  {first.handedness} {first.pose.value}"
            f"  pinch {first.pinch_index:.2f}/{first.pinch_middle:.2f}"
        )
    else:
        label += "  no hand"
    cv2.rectangle(canvas, (0, height - 26), (width, height), (18, 18, 18), -1)
    cv2.putText(
        canvas, label, (8, height - 8), cv2.FONT_HERSHEY_SIMPLEX, 0.48, colour, 1, cv2.LINE_AA
    )
    return canvas


def _draw(
    rig: Rig,
    prompt: Prompt,
    index: int,
    total: int,
    banner: str,
    progress: float,
    recording: bool,
):
    """Render every camera side by side. Returns what was seen this instant."""
    frames, hands, fresh = rig.read()
    if not frames:
        return frames, hands, fresh

    colour = (90, 220, 120) if recording else (200, 200, 200)
    tiles = [
        _tile(frames[camera_id], hands.get(camera_id, []), colour, camera_id)
        for camera_id in sorted(frames)
    ]
    # Cameras can differ in aspect ratio, so pad to the tallest before stacking.
    tallest = max(tile.shape[0] for tile in tiles)
    padded = [
        tile
        if tile.shape[0] == tallest
        else np.vstack(
            [tile, np.zeros((tallest - tile.shape[0], tile.shape[1], 3), dtype=tile.dtype)]
        )
        for tile in tiles
    ]
    body = np.hstack(padded)

    width = body.shape[1]
    header = np.zeros((86, width, 3), dtype=body.dtype)
    cv2.putText(
        header,
        f"{index + 1}/{total}  {prompt.instruction}",
        (12, 30),
        cv2.FONT_HERSHEY_SIMPLEX,
        0.72,
        (245, 245, 245),
        2,
        cv2.LINE_AA,
    )
    cv2.putText(
        header, prompt.detail, (12, 54), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (165, 165, 165), 1
    )
    cv2.putText(header, banner, (12, 76), cv2.FONT_HERSHEY_SIMPLEX, 0.5, colour, 1)
    cv2.rectangle(header, (0, 82), (int(width * progress), 86), colour, -1)

    cv2.imshow(WINDOW, np.vstack([header, body]))
    return frames, hands, fresh


def _countdown(rig: Rig, prompt: Prompt, index: int, total: int) -> bool:
    """Give the user time to get into position. False if they pressed Escape."""
    started = time.monotonic()
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= COUNTDOWN_S:
            return True
        _draw(
            rig,
            prompt,
            index,
            total,
            f"get ready... {COUNTDOWN_S - elapsed:.1f}s   (Esc cancels)",
            elapsed / COUNTDOWN_S,
            recording=False,
        )
        if cv2.waitKey(15) & 0xFF == 27:
            return False


def _capture(
    rig: Rig,
    prompt: Prompt,
    index: int,
    total: int,
    writer: SessionWriter,
    origin: float,
) -> bool:
    """Record one prompt's worth of frames. False if the user pressed Escape."""
    started = time.monotonic()
    kept = 0
    while True:
        elapsed = time.monotonic() - started
        if elapsed >= prompt.seconds:
            print(f"[record] {prompt.label}: {kept} frames")
            return True

        frames, hands, fresh = _draw(
            rig,
            prompt,
            index,
            total,
            f"RECORDING {prompt.label}  {prompt.seconds - elapsed:.1f}s left",
            elapsed / prompt.seconds,
            recording=True,
        )
        # Only store genuinely new images; the preview redraws faster than the
        # cameras deliver, and duplicates would weight the fit toward whichever
        # moments happened to be shown twice.
        if fresh and frames:
            writer.add(
                RecordedFrame(
                    time=time.monotonic() - origin,
                    label=prompt.label,
                    views=rig.views(frames, hands),
                )
            )
            kept += 1

        if cv2.waitKey(5) & 0xFF == 27:
            return False


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
