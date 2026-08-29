"""Fitting thresholds to the hands that were actually recorded.

Every threshold in this system separates two clusters of a measurement -- pinched
from open, curled from extended. Guessing where the boundary goes is what the
shipped defaults do. Given a labelled recording the boundary can instead be
*measured*, because the label says which cluster each sample belongs to.

The method is the same in every case:

1. Collect the measurement under labels where it should be low, and under labels
   where it should be high.
2. Look at the gap between the two clusters, using percentiles rather than
   min/max so one bad frame cannot define the boundary.
3. Place the threshold inside that gap.
4. If there is no gap, refuse. Overlapping clusters mean the pose is genuinely
   not separable this way, and a fabricated number would only hide that.

Refusing is the important part. A tuner that always emits a value is
indistinguishable from one that emits noise.

One sample is taken per frame, from the hand that was *performing* the prompt.
Recordings routinely show both hands while only one does the work -- the other
rests in view -- and pooling them merges two different shapes into one cluster.
Observed in practice: a fist segment where the working hand measured 0.85 and the
resting hand 1.18 looked like a single smear from 0.68 to 1.28, and the thumb was
declared unseparable. Picking the frame's extreme value in the direction the
prompt implies recovers the performing hand without needing to be told which it
was.

That choice is made *after* fusion, never across cameras. Picking a frame's
extreme across viewpoints would be a different operation wearing the same clothes:
it takes the lowest of three cameras for a low cluster and the highest for a high
one, inventing a gap that no camera measured. A three-camera recording was fitted
that way and put `pinch_close` at 0.498 when the closed pinches actually sat
around 0.604 -- below every frame it was meant to catch. Fitting the fused hand
also means the number is fitted to what the engine will compare it against.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from itertools import pairwise
from pathlib import Path

import numpy as np

from ..config import GestureConfig, TrackingConfig
from ..gestures.fusion import FusedHand, fuse_session
from ..gestures.geometry import PALM_POINTS, palm_span
from .store import RecordedFrame, Session

PALM_POINTS_IDX = list(PALM_POINTS)

# One frame of a recording after fusion: the prompt it was captured under, and the
# merged hands the engine would have seen.
Fused = list[tuple[RecordedFrame, list[FusedHand]]]

# Percentiles used as cluster edges: the point below which almost all of the low
# cluster sits, and above which almost all of the high cluster sits.
LOW_EDGE = 90.0
HIGH_EDGE = 10.0


@dataclass
class Suggestion:
    """One proposed threshold, with the evidence behind it."""

    section: str
    key: str
    current: float
    proposed: float | None
    reason: str
    samples: int = 0

    @property
    def actionable(self) -> bool:
        if self.proposed is None:
            return False
        return abs(self.proposed - self.current) > 1e-4

    def describe(self) -> str:
        if self.proposed is None:
            return f"  {self.key:<20} keep {self.current:<7.3f}  {self.reason}"
        arrow = "->" if self.actionable else "=="
        return (
            f"  {self.key:<20} {self.current:<7.3f} {arrow} {self.proposed:<7.3f}"
            f"  {self.reason}"
        )


def _percentile(values: list[float], q: float) -> float:
    return float(np.percentile(np.asarray(values, dtype=np.float64), q))


def _split_threshold(
    low: list[float], high: list[float], position: float, name: str
) -> tuple[float | None, str]:
    """Place a boundary in the gap between a low and a high cluster.

    ``position`` slides the result across the gap: 0.0 hugs the low cluster, 1.0
    hugs the high one.
    """
    if len(low) < 20 or len(high) < 20:
        return None, f"not enough samples ({len(low)} low, {len(high)} high)"
    low_edge = _percentile(low, LOW_EDGE)
    high_edge = _percentile(high, HIGH_EDGE)
    if high_edge <= low_edge:
        return None, (
            f"clusters overlap (low p{LOW_EDGE:.0f}={low_edge:.3f} >= "
            f"high p{HIGH_EDGE:.0f}={high_edge:.3f}); {name} not separable"
        )
    value = low_edge + position * (high_edge - low_edge)
    return round(value, 3), f"gap {low_edge:.3f}..{high_edge:.3f}"


def _segment(fused: Fused, labels: tuple[str, ...]) -> Fused:
    return [entry for entry in fused if entry[0].label in labels]


def _per_frame(fused: Fused, labels: tuple[str, ...], metric, low: bool) -> list[float]:
    """One sample per frame, from whichever fused hand was performing the prompt.

    ``low`` says which direction the prompt implies: a fist means the lowest
    thumb-to-palm distance in the frame, an open palm the highest. The choice is
    between the user's two hands, not between cameras -- those are already merged.
    """
    samples: list[float] = []
    for _, hands in _segment(fused, labels):
        values = [metric(hand.features) for hand in hands]
        finite = [v for v in values if v is not None and math.isfinite(v)]
        if finite:
            samples.append(min(finite) if low else max(finite))
    return samples


def _finger_ratio(hand, finger: int) -> float:
    """Tip-to-wrist over knuckle-to-wrist, what `finger_extended` compares against."""
    from ..gestures.geometry import FINGERS, WRIST

    tip, pip = FINGERS[finger]
    points = hand.world
    reach = float(np.linalg.norm(points[pip] - points[WRIST]))
    if reach < 1e-6:
        return float("nan")
    return float(np.linalg.norm(points[tip] - points[WRIST])) / reach


def _finger_ratios(fused: Fused, labels: tuple[str, ...], low: bool) -> list[float]:
    """All four finger ratios, per frame, from the hand performing the prompt.

    The hand is chosen once per frame by total extension, so all four ratios come
    from the same hand -- picking per finger could mix a curled finger from one
    hand with an extended one from the other.
    """
    values: list[float] = []
    for _, hands in _segment(fused, labels):
        best: list[float] | None = None
        best_total: float | None = None
        for hand in hands:
            ratios = [_finger_ratio(hand.features, finger) for finger in range(4)]
            if not all(math.isfinite(r) for r in ratios):
                continue
            total = sum(ratios)
            if best_total is None or (total < best_total if low else total > best_total):
                best, best_total = ratios, total
        if best is not None:
            values += best
    return values


def _thumb_distance(hand) -> float:
    from ..gestures.geometry import PINKY_MCP, THUMB_TIP

    points = hand.world
    return float(np.linalg.norm(points[THUMB_TIP] - points[PINKY_MCP])) / palm_span(points)


def _measure(fused: Fused, labels: tuple[str, ...], pick, low: bool) -> list[float]:
    return _per_frame(fused, labels, pick, low)


def analyse(
    session: Session, cfg: GestureConfig, tracking: TrackingConfig | None = None
) -> list[Suggestion]:
    """Work out every threshold this recording has something to say about."""
    out: list[Suggestion] = []
    # Fused once, under the current thresholds, and reused by every fit below.
    fused: Fused = list(fuse_session(session, cfg, tracking))

    # --- pinch -------------------------------------------------------------
    # Closed while pinching, open while holding ready or an open palm. The
    # pinch_cycle segment spans both states, so it is left out of the clusters
    # and used later by the replay test instead.
    # A fist belongs in the open cluster even though it is not an open hand. Its
    # thumb sits alongside the curled fingers, so it measures as pinched -- and a
    # pinch that closes on the way into a fist latches PINCHED, which outranks the
    # scroll branch and silently costs every scroll after it. Fitting against ready
    # and open palms alone once proposed 0.676 on a hand whose fists sat at 0.537,
    # which replayed to zero scrolls. If the two genuinely overlap there is no safe
    # threshold, and declining is the right answer.
    closed = _measure(fused, ("pinch_closed",), lambda h: h.pinch_index, low=True)
    opened = _measure(
        fused, ("ready", "open_palm", "fist"), lambda h: h.pinch_index, low=False
    )
    close_value, close_reason = _split_threshold(closed, opened, 0.30, "pinch")
    open_value, open_reason = _split_threshold(closed, opened, 0.60, "pinch")
    out.append(
        Suggestion(
            "gestures", "pinch_close", cfg.pinch_close, close_value, close_reason,
            len(closed) + len(opened),
        )
    )
    out.append(
        Suggestion(
            "gestures", "pinch_open", cfg.pinch_open, open_value, open_reason,
            len(closed) + len(opened),
        )
    )

    # --- finger extension --------------------------------------------------
    # A fist is the cleanest "everything curled" and an open palm the cleanest
    # "everything extended", so the boundary is fitted across all four fingers
    # pooled. Pooling matters: a per-finger threshold would drift with finger
    # length, which is exactly what the ratio is designed to cancel out.
    curled = _finger_ratios(fused, ("fist",), low=True)
    extended = _finger_ratios(fused, ("open_palm",), low=False)
    # Placed nearer the extended cluster than the midpoint, because the two
    # reference poses are the extremes: a fist is fully curled, an open palm fully
    # straight, and the fingers that matter most sit between them. In a relaxed
    # pinching hand the ring and little finger are only half folded, and a midpoint
    # threshold reads them as extended -- which stops the hand being `ready` at all.
    value, reason = _split_threshold(curled, extended, 0.7, "finger extension")
    out.append(
        Suggestion(
            "gestures", "finger_extended", cfg.finger_extended, value, reason,
            len(curled) + len(extended),
        )
    )

    # --- thumb -------------------------------------------------------------
    thumb_in = _per_frame(fused, ("fist",), _thumb_distance, low=True)
    thumb_out = _per_frame(fused, ("telephone", "open_palm"), _thumb_distance, low=False)
    value, reason = _split_threshold(thumb_in, thumb_out, 0.5, "thumb")
    out.append(
        Suggestion(
            "gestures", "thumb_extended", cfg.thumb_extended, value, reason,
            len(thumb_in) + len(thumb_out),
        )
    )

    # --- open palm ---------------------------------------------------------
    # One-sided: there is no "should be low" cluster, so the threshold simply
    # sits below what open palms actually measured, with headroom.
    spreads = _measure(fused, ("open_palm",), lambda h: h.spread, low=False)
    if len(spreads) >= 20:
        floor = _percentile(spreads, HIGH_EDGE)
        out.append(
            Suggestion(
                "gestures", "palm_spread", cfg.palm_spread, round(floor * 0.8, 3),
                f"open palms measured p{HIGH_EDGE:.0f}={floor:.3f}", len(spreads),
            )
        )
    else:
        out.append(
            Suggestion("gestures", "palm_spread", cfg.palm_spread, None, "no open-palm samples")
        )

    facings = _measure(fused, ("open_palm",), lambda h: h.facing, low=False)
    if len(facings) >= 20:
        floor = _percentile(facings, HIGH_EDGE)
        # Never propose a negative gate; that would accept a palm facing away.
        out.append(
            Suggestion(
                "gestures", "palm_facing", cfg.palm_facing, round(max(floor * 0.7, 0.0), 3),
                f"open palms measured p{HIGH_EDGE:.0f}={floor:.3f}", len(facings),
            )
        )

    # --- holding still -----------------------------------------------------
    drift = _hold_drift(fused, ("open_palm", "telephone"))
    if drift:
        worst = _percentile(drift, 95.0)
        out.append(
            Suggestion(
                "gestures", "hold_max_travel", cfg.hold_max_travel,
                round(max(worst * 1.3, 0.02), 3),
                f"you drift up to {worst:.3f} while holding still", len(drift),
            )
        )

    # --- swipes ------------------------------------------------------------
    speeds = _swipe_speeds(fused)
    if len(speeds) >= 5:
        gentlest = _percentile(speeds, 25.0)
        out.append(
            Suggestion(
                "gestures", "swipe_min_speed", cfg.swipe_min_speed,
                round(gentlest * 0.6, 3),
                f"your swipes peaked at p25={gentlest:.2f} units/s", len(speeds),
            )
        )
    else:
        out.append(
            Suggestion(
                "gestures", "swipe_min_speed", cfg.swipe_min_speed, None,
                "too few swipes detected to fit",
            )
        )

    return out


def _tracks(fused: Fused, labels: tuple[str, ...]) -> list[list[tuple[float, tuple[float, float]]]]:
    """Timed anchor paths, one per unbroken run of a single hand on one camera.

    Split by handedness, because pooling both hands' anchors measures the distance
    *between* the hands rather than the motion of either.

    Split again at every rebase. Fused position comes from whichever camera
    currently leads, so a change of leader moves the anchor by the parallax
    between two viewpoints while the hand itself has not moved. Carried into a
    drift figure that reads as wander; into a speed, as a hand that teleported.
    The live pointer rebases at exactly these moments for the same reason.
    """
    runs: dict[str, list[list[tuple[float, tuple[float, float]]]]] = {}
    for frame, hands in _segment(fused, labels):
        for hand in hands:
            side = hand.features.handedness
            chain = runs.setdefault(side, [[]])
            if hand.rebased and chain[-1]:
                chain.append([])
            chain[-1].append((frame.time, hand.features.anchor))
    return [run for chain in runs.values() for run in chain if run]


def _hold_drift(fused: Fused, labels: tuple[str, ...]) -> list[float]:
    """How far a palm wanders during poses meant to be held still.

    Measured on the steadiest run in each segment, because the gesture that
    cares about this -- holding a palm up to engage -- only asks one hand to be
    still. The other hand shifting about is not the user failing to hold a pose.
    """
    values: list[float] = []
    for label in labels:
        candidates: list[list[float]] = []
        for run in _tracks(fused, (label,)):
            if len(run) < 5:
                continue
            anchors = [point for _, point in run]
            centre_x = float(np.mean([a[0] for a in anchors]))
            centre_y = float(np.mean([a[1] for a in anchors]))
            candidates.append([math.hypot(a[0] - centre_x, a[1] - centre_y) for a in anchors])
        if candidates:
            values += min(candidates, key=lambda drift: float(np.median(drift)))
    return values


def _swipe_speeds(fused: Fused) -> list[float]:
    """Peak anchor speeds during the swipe prompt, in units per second."""
    speeds: list[float] = []
    # Timestamps run alongside the anchors so each pair divides by its own
    # interval; a dropped frame otherwise reads as an impossibly fast hand.
    for run in _tracks(fused, ("swipe",)):
        for (t0, a), (t1, b) in pairwise(run):
            dt = t1 - t0
            if dt <= 1e-4:
                continue
            speeds.append(math.hypot(b[0] - a[0], b[1] - a[1]) / dt)

    # Only the fast part of a sweep is the swipe; the turnarounds at each end are
    # slow by definition and would drag the estimate down.
    if not speeds:
        return []
    fast = _percentile(speeds, 70.0)
    return [s for s in speeds if s >= fast]


# Thresholds that only mean anything as a pair: the first must stay below the
# second. `pinch_close`/`pinch_open` is a hysteresis band -- close the pinch below
# one, release it above the other -- and the gap between them is what stops a hand
# hovering at the boundary from chattering.
ORDERED_PAIRS: tuple[tuple[str, str], ...] = (("pinch_close", "pinch_open"),)


def _broken_pairs(cfg: GestureConfig, suggestions: list[Suggestion]) -> list[str]:
    """Complaints about any pair a partial write would invert.

    Worth checking because writing half a pair is an easy and quiet mistake. Fit
    both and the ordering is preserved; take only the lower one and it can land
    above the upper, at which point a single steady hand reads as closed *and*
    open and emits a click every frame. Observed: six clicks in one second.
    """
    proposed = {s.key: s.proposed for s in suggestions if s.proposed is not None}
    complaints: list[str] = []
    for lower, upper in ORDERED_PAIRS:
        low = proposed.get(lower, getattr(cfg, lower))
        high = proposed.get(upper, getattr(cfg, upper))
        if low >= high:
            complaints.append(
                f"{lower}={low} would sit at or above {upper}={high}, "
                f"which inverts the hysteresis and makes a steady hand chatter. "
                f"Write both, or neither."
            )
    return complaints


def patch_config(path: Path, suggestions: list[Suggestion]) -> list[str]:
    """Rewrite thresholds in place, preserving comments and layout.

    A TOML round-trip would strip the commentary that makes this file worth
    reading, so the assignments are edited line by line instead.
    """
    applied: list[str] = []
    lines = path.read_text().splitlines(keepends=True)
    wanted = {(s.section, s.key): s for s in suggestions if s.actionable}
    section = ""

    for index, line in enumerate(lines):
        header = re.match(r"\s*\[([^\]]+)\]", line)
        if header:
            section = header.group(1)
            continue
        match = re.match(r"(\s*)([A-Za-z_][A-Za-z0-9_]*)(\s*=\s*)(.+?)(\s*(?:#.*)?)$", line)
        if not match:
            continue
        suggestion = wanted.get((section, match.group(2)))
        if suggestion is None or suggestion.proposed is None:
            continue
        indent, key, sep, _old, trailing = match.groups()
        lines[index] = f"{indent}{key}{sep}{suggestion.proposed}{trailing.rstrip()}\n"
        applied.append(f"{section}.{key} = {suggestion.proposed}")

    if applied:
        path.write_text("".join(lines))
    return applied


def run(
    session_path: Path,
    cfg: GestureConfig,
    config_path: Path | None,
    apply: bool,
    only: set[str] | None = None,
    tracking: TrackingConfig | None = None,
) -> int:
    session = Session.load(session_path)
    print(f"[autotune] {session_path.name}: {session.summary()}\n")

    problems = session.problems()
    if problems:
        print("recording quality — these limit what can be fitted:")
        for problem in problems:
            print(f"  ! {problem}")
        print()

    suggestions = analyse(session, cfg, tracking)
    print("threshold                current    proposed  evidence")
    for suggestion in suggestions:
        print(suggestion.describe())

    changes = [s for s in suggestions if s.actionable]
    refused = [s for s in suggestions if s.proposed is None]
    print(f"\n{len(changes)} change(s) proposed, {len(refused)} declined")

    if only is not None:
        unknown = only - {s.key for s in suggestions}
        if unknown:
            print(f"[autotune] no such threshold: {', '.join(sorted(unknown))}")
            return 2
        # A fit can be sound and still be a regression -- several of these
        # thresholds trade one pose against another. Replaying a candidate is the
        # only way to tell, so writing a chosen subset has to be possible.
        suggestions = [s for s in suggestions if s.key in only]
        broken = _broken_pairs(cfg, suggestions)
        if broken:
            for complaint in broken:
                print(f"[autotune] refusing: {complaint}")
            return 2
        print(f"[autotune] writing only: {', '.join(sorted(only))}")

    if not apply:
        print("[autotune] dry run; pass --apply to write these into config.toml")
        return 0
    if config_path is None:
        print("[autotune] no config.toml found to write to")
        return 2
    applied = patch_config(config_path, suggestions)
    for entry in applied:
        print(f"[autotune] set {entry}")
    print(f"[autotune] updated {config_path}" if applied else "[autotune] nothing to change")
    return 0
