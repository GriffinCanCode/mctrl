"""Configuration loading.

The whole app is driven by ``config.toml``; this module resolves where that file
lives, merges it over the built-in defaults, and exposes it as nested dataclasses
so the rest of the code gets attribute access and type checking instead of dict
spelunking.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field, fields, is_dataclass
from pathlib import Path
from typing import Any

APP_DIR = Path.home() / ".config" / "mindcontrol"
STATE_DIR = Path.home() / ".local" / "state" / "mindcontrol"
CACHE_DIR = Path.home() / ".cache" / "mindcontrol"
GAZE_MODEL_PATH = STATE_DIR / "gaze_calibration.json"


@dataclass
class CameraConfig:
    devices: list[int] = field(default_factory=lambda: [0])
    primary_gaze: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 30
    mirror: bool = True


@dataclass
class TrackingConfig:
    max_hands: int = 2
    hand_detection_confidence: float = 0.6
    hand_presence_confidence: float = 0.5
    hand_tracking_confidence: float = 0.5
    face_enabled: bool = True
    face_detection_confidence: float = 0.5
    stale_after_ms: float = 250.0
    # How much better a rival camera must score before it takes over position.
    # Handovers are cheap now that motion is stitched across them, so this only
    # stops the lead flitting between views of near-equal confidence.
    leader_margin: float = 0.15


@dataclass
class PointerConfig:
    mode: str = "hybrid"
    sensitivity: float = 2600.0
    gain_min: float = 0.55
    gain_max: float = 3.1
    gain_speed_reference: float = 1.4
    deadzone: float = 0.0012
    filter_fc_min: float = 1.4
    filter_beta: float = 0.035


@dataclass
class GazeConfig:
    fixation_ms: float = 150.0
    fixation_radius: float = 0.045
    warp_min_distance: float = 0.09
    hand_quiet_speed: float = 0.25
    filter_fc_min: float = 0.9
    filter_beta: float = 0.008
    blink_ear: float = 0.14


@dataclass
class GestureConfig:
    pinch_close: float = 0.30
    pinch_open: float = 0.42
    tap_max_ms: float = 260.0
    tap_max_travel: float = 0.05
    double_click_ms: float = 400.0
    finger_extended: float = 1.18
    thumb_extended: float = 0.80
    palm_spread: float = 0.18
    palm_facing: float = 0.0
    engage_hold_ms: float = 1000.0
    dictation_hold_ms: float = 700.0
    hold_max_travel: float = 0.09
    scroll_sensitivity: float = 2200.0
    scroll_deadzone: float = 0.002
    swipe_min_speed: float = 1.1
    swipe_min_travel: float = 0.16
    # How long a sweep keeps its open-palm status after the pose flickers out.
    # Zero demands the pose every frame, which is what a swipe used to require.
    swipe_grace_ms: float = 0.0
    gesture_cooldown_ms: float = 500.0


@dataclass
class ModesConfig:
    suspend_on_physical_input: bool = True
    resume_after_s: float = 3.0
    start_engaged: bool = False


@dataclass
class NativeConfig:
    """Settings for the native interaction helper in ``native/``.

    Field names are the wire contract with the Swift side, which decodes this
    dataclass verbatim from JSON. Renaming one here means renaming it in
    ``native/Sources/Bridge/Tuning.swift`` too.
    """

    # Fall back to posting events straight from Python. Everything still works,
    # but without smoothing, snapping or a highlight.
    enabled: bool = True

    # --- motion ---
    # Seconds for the cursor to close most of the gap to where the hand points.
    # The camera offers thirty positions a second and the display wants a hundred
    # and twenty, so the difference has to be interpolated rather than stepped.
    motion_time_constant: float = 0.045
    minimum_step_pixels: float = 0.35
    maximum_speed: float = 26000.0

    # --- snapping ---
    snap_enabled: bool = True
    # Pixels from a target at which its pull starts to be felt.
    snap_radius: float = 96.0
    snap_strength: float = 0.75
    # Targets no larger than this pull to their centre; bigger ones pull only to
    # their nearest edge, so a large panel can be entered anywhere.
    small_target_pixels: float = 72.0
    # Bonus the current target keeps, so the highlight does not flicker between
    # two adjacent controls. Hysteresis in space, like pinch_close/pinch_open in time.
    snap_stickiness: float = 1.4
    # How much to favour targets in the direction the hand is travelling.
    snap_heading_weight: float = 0.45
    # Snap to words inside text, not only to the text element as a whole.
    text_snap_enabled: bool = True

    # --- probing ---
    # Milliseconds between accessibility hit tests. Measured at 0.43 ms median but
    # 3.46 ms at p95, which is why it runs on its own thread.
    probe_interval_ms: float = 16.0
    probe_lookahead_s: float = 0.08
    # Give up on an application that will not answer this quickly, in seconds.
    probe_timeout_s: float = 0.05
    target_lifetime_ms: float = 350.0

    # --- highlight ---
    overlay_enabled: bool = True
    overlay_corner_radius: float = 6.0
    overlay_border_width: float = 2.0
    overlay_glide_s: float = 0.11
    overlay_border_color: list[float] = field(default_factory=lambda: [0.36, 0.72, 1.0, 0.95])
    overlay_fill_color: list[float] = field(default_factory=lambda: [0.36, 0.72, 1.0, 0.14])


@dataclass
class DebugConfig:
    overlay: bool = False
    stats_interval_s: float = 0.0


@dataclass
class KeyBinding:
    key: str
    mods: list[str] = field(default_factory=list)


DEFAULT_BINDINGS: dict[str, str] = {
    "swipe_left": "desktop_left",
    "swipe_right": "desktop_right",
    "palm_push_up": "mission_control",
    "telephone": "dictation",
}

DEFAULT_KEYS: dict[str, KeyBinding] = {
    "dictation": KeyBinding("f5", []),
    "mission_control": KeyBinding("up", ["ctrl"]),
    "desktop_left": KeyBinding("left", ["ctrl"]),
    "desktop_right": KeyBinding("right", ["ctrl"]),
}


@dataclass
class Config:
    cameras: CameraConfig = field(default_factory=CameraConfig)
    tracking: TrackingConfig = field(default_factory=TrackingConfig)
    pointer: PointerConfig = field(default_factory=PointerConfig)
    gaze: GazeConfig = field(default_factory=GazeConfig)
    gestures: GestureConfig = field(default_factory=GestureConfig)
    modes: ModesConfig = field(default_factory=ModesConfig)
    native: NativeConfig = field(default_factory=NativeConfig)
    debug: DebugConfig = field(default_factory=DebugConfig)
    bindings: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_BINDINGS))
    keys: dict[str, KeyBinding] = field(default_factory=lambda: dict(DEFAULT_KEYS))
    source_path: Path | None = None


def _apply(target: Any, values: dict[str, Any], where: str) -> None:
    """Overlay a TOML table onto a dataclass instance, ignoring unknown keys."""
    known = {f.name: f.type for f in fields(target)}
    for key, value in values.items():
        if key not in known:
            print(f"[config] ignoring unknown key {where}.{key}")
            continue
        setattr(target, key, value)


def config_search_path(explicit: Path | None = None) -> list[Path]:
    if explicit:
        return [explicit]
    return [Path.cwd() / "config.toml", APP_DIR / "config.toml"]


def load(explicit: Path | None = None) -> Config:
    """Load config from the first path that exists, else return defaults."""
    cfg = Config()
    for candidate in config_search_path(explicit):
        if not candidate.is_file():
            continue
        with candidate.open("rb") as handle:
            raw = tomllib.load(handle)
        for name, value in raw.items():
            if name == "bindings":
                cfg.bindings.update(value)
            elif name == "keys":
                cfg.keys.update(
                    {
                        action: KeyBinding(spec["key"], list(spec.get("mods", [])))
                        for action, spec in value.items()
                    }
                )
            elif hasattr(cfg, name) and is_dataclass(getattr(cfg, name)):
                _apply(getattr(cfg, name), value, name)
            else:
                print(f"[config] ignoring unknown section [{name}]")
        cfg.source_path = candidate
        break
    return cfg


def ensure_dirs() -> None:
    for path in (APP_DIR, STATE_DIR, CACHE_DIR):
        path.mkdir(parents=True, exist_ok=True)
