"""Replaying a recorded session through the gesture engine.

This is the part that makes the system testable. A recording of real hands can be
pushed through the state machine offline, deterministically, as many times as we
like -- so "does a pinch still produce exactly one click" becomes a question that
can be answered without a human, a camera, or good lighting.

It also closes the loop on tuning. Change a threshold, replay, and see whether
the number of clicks went up or down. That is a measurement of the change, not an
opinion about it.

Replay uses the recorded timestamps rather than wall-clock time, so every run is
identical and the durations the state machine cares about -- tap length, hold
length, cooldowns -- stay faithful to what actually happened.

Multi-camera recordings go through the same `HandFusion` the live pipeline uses,
so a replay tests the merge as well as the state machine. That path is otherwise
very hard to test: it needs two cameras genuinely disagreeing about one hand, and
feeding it the same image twice proves almost nothing.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from ..config import Config
from ..gestures.engine import Action, GestureEngine, GestureEvent
from ..gestures.fusion import HandFusion, Observation, fuse_session
from .store import Session


@dataclass
class ReplayResult:
    """Everything the engine did during a replay."""

    events: list[tuple[float, str, GestureEvent]] = field(default_factory=list)
    merged_frames: int = 0
    rebases: int = 0

    def counts(self, label: str | None = None) -> Counter[Action]:
        """How many of each action fired, optionally within one prompt's frames."""
        return Counter(
            event.action
            for _, event_label, event in self.events
            if label is None or event_label == label
        )

    def during(self, label: str) -> list[GestureEvent]:
        return [event for _, event_label, event in self.events if event_label == label]

    def total(self, action: Action, label: str | None = None) -> int:
        return self.counts(label)[action]

    def summary(self) -> str:
        counts = self.counts()
        if not counts:
            return "no events"
        return ", ".join(
            f"{action.value} x{count}" for action, count in sorted(counts.items(), key=str)
        )


def replay(session: Session, cfg: Config, engaged: bool = True) -> ReplayResult:
    """Push a recording through a fresh engine and collect what it emits.

    ``engaged`` mirrors the live app's mode gate: replaying disengaged is how the
    engage gesture itself gets tested.
    """
    engine = GestureEngine(cfg.pointer, cfg.gestures, cfg.tracking)
    fusion = HandFusion(cfg.tracking, cfg.gestures)
    result = ReplayResult()
    previous_time: float | None = None

    for frame in session.frames:
        dt = 1 / 30.0 if previous_time is None else max(frame.time - previous_time, 1e-4)
        previous_time = frame.time

        fused = fusion.fuse(
            [
                Observation(
                    camera_id=view.camera_id,
                    hands=[hand.remeasure(cfg.gestures) for hand in view.hands],
                    age_ms=view.age_ms,
                )
                for view in frame.views
            ]
        )
        # The live pipeline rebases the pointer when the leading camera changes,
        # so that a viewpoint switch does not fling the cursor. Replaying without
        # it would report jumps the real app never makes.
        if any(hand.rebased for hand in fused):
            engine.rebase()
            result.rebases += 1
        if any(hand.merged for hand in fused):
            result.merged_frames += 1

        for event in engine.update([hand.features for hand in fused], frame.time, dt, engaged):
            result.events.append((frame.time, frame.label, event))

    return result


def pose_report(session: Session, cfg: Config) -> dict[str, Counter[str]]:
    """Which poses each prompt actually classified as.

    The diagonal is the interesting part: if the frames labelled ``fist`` mostly
    classify as something else, the thresholds are wrong for this user and every
    downstream gesture built on that pose will misbehave.

    Scored on the fused hand rather than each camera's view of it, so the figure
    answers "would the engine have recognised this", not "did some camera see it".
    Counting views instead lets a three-camera recording report a pose as 33%
    recognised when the engine recognised it every single frame.
    """
    report: dict[str, Counter[str]] = {}
    for frame, hands in fuse_session(session, cfg.gestures, cfg.tracking):
        bucket = report.setdefault(frame.label, Counter())
        if not hands:
            bucket["<no hand>"] += 1
        for hand in hands:
            bucket[hand.features.pose.value] += 1
    return report


# What each prompt should predominantly classify as. Transition prompts are left
# out: they legitimately span several poses.
EXPECTED_POSE = {
    "ready": "ready",
    "fist": "fist",
    "open_palm": "open_palm",
    "telephone": "telephone",
}


def accuracy(session: Session, cfg: Config) -> dict[str, float]:
    """Fraction of each held prompt's frames that classified as intended."""
    scores: dict[str, float] = {}
    for label, expected in EXPECTED_POSE.items():
        counts = pose_report(session, cfg).get(label)
        if not counts:
            continue
        total = sum(counts.values())
        scores[label] = counts.get(expected, 0) / total if total else 0.0
    return scores


def run(session: Session, cfg: Config) -> int:
    """Print a readable replay report."""
    print(f"[replay] {session.path.name if session.path else 'session'}: {session.summary()}\n")

    problems = session.problems()
    if problems:
        print("recording quality — read the results below with these in mind:")
        for problem in problems:
            print(f"  ! {problem}")
        print()

    print("pose classification, by prompt")
    report = pose_report(session, cfg)
    for label, counts in report.items():
        total = sum(counts.values())
        top = ", ".join(
            f"{pose} {count / total:.0%}" for pose, count in counts.most_common(3)
        )
        expected = EXPECTED_POSE.get(label)
        mark = ""
        if expected:
            share = counts.get(expected, 0) / total if total else 0.0
            mark = "  OK" if share >= 0.7 else f"  <-- wanted {expected}"
        print(f"  {label:<22} {top}{mark}")

    result = replay(session, cfg)
    if len(session.cameras) > 1:
        print(
            f"\nfusion: {result.merged_frames} frame(s) merged across cameras, "
            f"{result.rebases} pointer rebase(s) on a viewpoint change"
        )
    print(f"\nevents emitted while engaged: {result.summary()}")
    print("\nby prompt")
    for label in session.labels():
        counts = result.counts(label)
        if counts:
            detail = ", ".join(f"{a.value} x{c}" for a, c in sorted(counts.items(), key=str))
            print(f"  {label:<22} {detail}")

    disengaged = replay(session, cfg, engaged=False)
    engages = disengaged.total(Action.ENGAGE_TOGGLE)
    others = sum(v for k, v in disengaged.counts().items() if k is not Action.ENGAGE_TOGGLE)
    print(f"\nwhile disengaged: {engages} engage toggle(s), {others} other event(s)")
    if others:
        print("  WARNING: gestures leaked through while control was off")
    return 0
