"""The contract: what may be asked for, and what comes back.

One table of verbs, one set of immutable snapshots, one JSON encoding. Both
transports -- the in-process facade and the socket server -- dispatch from this
file, so the two cannot drift into different APIs for the same program.

Nothing here imports the pipeline, Quartz, or MediaPipe. A consumer that only
wants to speak the protocol, and a test that only wants to check it, can import
this module on its own.

Two rules shape the shape of everything below.

*Snapshots are frozen.* The live ``PipelineStatus`` is one mutable object that
the frame loop rewrites in place; handing it out would give every consumer a
racing view of a different frame. Every type here is a copy taken at a known
instant instead.

*Verbs are declared, not written twice.* The parameter list is data, so
validation, the socket dispatch table and ``system.describe`` all read the same
declaration. Adding a verb is one row.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

# The bindable gesture names, so `bindings.set` publishes its choices rather than
# describing them. Importing them costs nothing: `control.bindings` is the
# resolution rule and the table, and it loads no framework of any kind.
from ..control.bindings import BINDABLE

if TYPE_CHECKING:  # pragma: no cover - annotations only, never imported at runtime
    from ..gestures.engine import GestureEvent
    from ..gestures.fusion import FusedHand
    from ..pipeline import PipelineStatus

PROTOCOL_VERSION = 1

# Fractional coordinates and normalised landmarks carry no meaning past the
# fifth decimal -- that is a thousandth of a pixel on a 4K display -- and the
# rounding roughly halves the bytes on the wire at camera rate.
_PLACES = 5


def _round(value: float) -> float:
    return round(float(value), _PLACES)


# --------------------------------------------------------------------- errors


class ApiError(Exception):
    """A failure with a code a machine can branch on.

    Consumers are other programs, so the code is the contract and the message is
    for whoever is reading the log.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


UNKNOWN_VERB = "unknown_verb"
BAD_REQUEST = "bad_request"
BAD_PARAMS = "bad_params"
UNAVAILABLE = "unavailable"
BUSY = "busy"
INTERNAL = "internal"


# ------------------------------------------------------------------ snapshots


@dataclass(frozen=True)
class StatusSnapshot:
    """Everything the menu bar knows, taken at one instant."""

    fps: float = 0.0
    mode: str = "off"
    gesture: str = "idle"
    hands: int = 0
    cameras: tuple[int, ...] = ()
    merged: bool = False
    gaze_ready: bool = False
    gaze_point: tuple[float, float] | None = None
    warps: int = 0
    # False means the cursor is being driven from Python: it still moves, but it
    # is neither smoothed nor snapped. Worth surfacing, because a consumer
    # building a pointing UX feels the difference.
    native: bool = False
    # Bundle identifier of the application in front. Reported because a consumer
    # that wants to react to a gesture differently per application would
    # otherwise have to ask macOS itself, and would then be answering about a
    # different instant than the gesture arrived from.
    app: str = ""
    problems: tuple[str, ...] = ()

    @classmethod
    def of(cls, status: PipelineStatus) -> StatusSnapshot:
        return cls(
            fps=_round(status.fps),
            mode=status.mode,
            gesture=status.gesture,
            hands=status.hands,
            cameras=tuple(status.cameras),
            merged=status.merged,
            gaze_ready=status.gaze_ready,
            gaze_point=None
            if status.gaze_point is None
            else (_round(status.gaze_point[0]), _round(status.gaze_point[1])),
            warps=status.warps,
            native=status.native,
            app=status.app,
            problems=tuple(status.problems),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "fps": self.fps,
            "mode": self.mode,
            "gesture": self.gesture,
            "hands": self.hands,
            "cameras": list(self.cameras),
            "merged": self.merged,
            "gaze_ready": self.gaze_ready,
            "gaze_point": None if self.gaze_point is None else list(self.gaze_point),
            "warps": self.warps,
            "native": self.native,
            "app": self.app,
            "problems": list(self.problems),
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> StatusSnapshot:
        point = data.get("gaze_point")
        return cls(
            fps=float(data.get("fps", 0.0)),
            mode=str(data.get("mode", "off")),
            gesture=str(data.get("gesture", "idle")),
            hands=int(data.get("hands", 0)),
            cameras=tuple(int(c) for c in data.get("cameras", ())),
            merged=bool(data.get("merged", False)),
            gaze_ready=bool(data.get("gaze_ready", False)),
            gaze_point=None if point is None else (float(point[0]), float(point[1])),
            warps=int(data.get("warps", 0)),
            native=bool(data.get("native", False)),
            app=str(data.get("app", "")),
            problems=tuple(str(p) for p in data.get("problems", ())),
        )


@dataclass(frozen=True)
class HandSnapshot:
    """One fused hand: its shape, where it is, and which cameras saw it.

    The measurements are the scale-invariant ones the gesture engine itself
    reads, in palm spans rather than pixels, so a consumer comparing against a
    threshold from ``config.toml`` is comparing like with like.
    """

    handedness: str
    score: float
    pose: str
    anchor: tuple[float, float]
    palm_size: float
    pinch: float
    pinch_index: float
    pinch_middle: float
    pinch_is_middle: bool
    extended: tuple[bool, bool, bool, bool, bool]
    spread: float
    facing: float
    camera_id: int
    cameras: tuple[int, ...]
    # Normalised image-space (x, y) for MediaPipe's 21 points, and empty unless
    # asked for: it is by far the largest thing on the wire, and most consumers
    # want the pose label rather than the skeleton.
    landmarks: tuple[tuple[float, float], ...] = ()

    @property
    def merged(self) -> bool:
        return len(self.cameras) > 1

    @classmethod
    def of(cls, hand: FusedHand, *, landmarks: bool = False) -> HandSnapshot:
        features = hand.features
        points: tuple[tuple[float, float], ...] = ()
        if landmarks:
            points = tuple(
                (_round(row[0]), _round(row[1])) for row in features.landmarks[:, :2].tolist()
            )
        return cls(
            handedness=features.handedness,
            score=_round(features.score),
            pose=features.pose.value,
            anchor=(_round(features.anchor[0]), _round(features.anchor[1])),
            palm_size=_round(features.palm_size),
            pinch=_round(features.pinch),
            pinch_index=_round(features.pinch_index),
            pinch_middle=_round(features.pinch_middle),
            pinch_is_middle=features.pinch_is_middle,
            extended=tuple(bool(flag) for flag in features.extended),  # type: ignore[arg-type]
            spread=_round(features.spread),
            facing=_round(features.facing),
            camera_id=hand.camera_id,
            cameras=tuple(hand.cameras),
            landmarks=points,
        )

    def to_json(self, *, landmarks: bool = True) -> dict[str, Any]:
        payload = {
            "handedness": self.handedness,
            "score": self.score,
            "pose": self.pose,
            "anchor": list(self.anchor),
            "palm_size": self.palm_size,
            "pinch": self.pinch,
            "pinch_index": self.pinch_index,
            "pinch_middle": self.pinch_middle,
            "pinch_is_middle": self.pinch_is_middle,
            "extended": list(self.extended),
            "spread": self.spread,
            "facing": self.facing,
            "camera_id": self.camera_id,
            "cameras": list(self.cameras),
        }
        if landmarks and self.landmarks:
            payload["landmarks"] = [list(point) for point in self.landmarks]
        return payload

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> HandSnapshot:
        extended = tuple(bool(flag) for flag in data.get("extended", (False,) * 5))
        return cls(
            handedness=str(data["handedness"]),
            score=float(data["score"]),
            pose=str(data["pose"]),
            anchor=(float(data["anchor"][0]), float(data["anchor"][1])),
            palm_size=float(data["palm_size"]),
            pinch=float(data["pinch"]),
            pinch_index=float(data["pinch_index"]),
            pinch_middle=float(data["pinch_middle"]),
            pinch_is_middle=bool(data["pinch_is_middle"]),
            extended=extended,  # type: ignore[arg-type]
            spread=float(data["spread"]),
            facing=float(data["facing"]),
            camera_id=int(data["camera_id"]),
            cameras=tuple(int(c) for c in data.get("cameras", ())),
            landmarks=tuple(
                (float(point[0]), float(point[1])) for point in data.get("landmarks", ())
            ),
        )


@dataclass(frozen=True)
class HandsFrame:
    """The hands from one processed frame, with the frame they came from.

    ``sequence`` is the capturing camera's own counter, so a consumer can tell a
    repeated publication from a genuinely new frame without trusting wall clocks.

    ``t`` and ``timestamp_ms`` both come from the monotonic clock this process
    started on, which makes them exact for measuring intervals and meaningless as
    a date. Nothing downstream needs a date, and a wall clock that steps
    backwards mid-gesture would be worse than useless.
    """

    t: float
    camera_id: int
    sequence: int
    timestamp_ms: int
    hands: tuple[HandSnapshot, ...] = ()

    def to_json(self, *, landmarks: bool = True) -> dict[str, Any]:
        return {
            "t": self.t,
            "camera_id": self.camera_id,
            "sequence": self.sequence,
            "timestamp_ms": self.timestamp_ms,
            "hands": [hand.to_json(landmarks=landmarks) for hand in self.hands],
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> HandsFrame:
        return cls(
            t=float(data.get("t", 0.0)),
            camera_id=int(data.get("camera_id", 0)),
            sequence=int(data.get("sequence", 0)),
            timestamp_ms=int(data.get("timestamp_ms", 0)),
            hands=tuple(HandSnapshot.from_json(hand) for hand in data.get("hands", ())),
        )


@dataclass(frozen=True)
class GazeSnapshot:
    """Where the eyes are pointing, as a fraction of the calibrated display.

    ``point`` is ``None`` until a calibration exists and a face is visible; it is
    the filtered estimate, not the raw prediction, because the raw one is too
    jittery to put anything on screen at.
    """

    ready: bool = False
    point: tuple[float, float] | None = None
    warps: int = 0

    def to_json(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "point": None if self.point is None else list(self.point),
            "warps": self.warps,
        }

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GazeSnapshot:
        point = data.get("point")
        return cls(
            ready=bool(data.get("ready", False)),
            point=None if point is None else (float(point[0]), float(point[1])),
            warps=int(data.get("warps", 0)),
        )


@dataclass(frozen=True)
class GestureEventMsg:
    """One intent the gesture engine emitted.

    ``dx`` and ``dy`` are pixel deltas for the motion actions and zero for the
    rest, which mirrors ``GestureEvent`` exactly: the API reports what the engine
    decided, not a re-derivation of it.
    """

    action: str
    dx: float = 0.0
    dy: float = 0.0
    button: str = "left"

    @classmethod
    def of(cls, event: GestureEvent) -> GestureEventMsg:
        return cls(
            action=event.action.value,
            dx=_round(event.dx),
            dy=_round(event.dy),
            button=event.button,
        )

    def to_json(self) -> dict[str, Any]:
        return {"action": self.action, "dx": self.dx, "dy": self.dy, "button": self.button}

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> GestureEventMsg:
        return cls(
            action=str(data["action"]),
            dx=float(data.get("dx", 0.0)),
            dy=float(data.get("dy", 0.0)),
            button=str(data.get("button", "left")),
        )


# The four things worth watching, and what each carries.
STATUS = "status"
HANDS = "hands"
GAZE = "gaze"
GESTURES = "gestures"

STREAMS: tuple[str, ...] = (STATUS, HANDS, GAZE, GESTURES)

STREAM_TYPES: dict[str, type] = {
    STATUS: StatusSnapshot,
    HANDS: HandsFrame,
    GAZE: GazeSnapshot,
    GESTURES: GestureEventMsg,
}

STREAM_SUMMARIES: dict[str, str] = {
    STATUS: "one snapshot per processed frame: mode, gesture, fps, cameras, problems",
    HANDS: "fused hands per processed frame, with pose and scale-invariant measurements",
    GAZE: "filtered gaze point as a fraction of the calibrated display",
    GESTURES: "discrete intents as the engine emits them, including motion deltas",
}


def decode_stream(stream: str, data: dict[str, Any]) -> Any:
    """Rebuild a stream payload, so a remote consumer gets the same types."""
    kind = STREAM_TYPES.get(stream)
    if kind is None:
        raise ApiError(BAD_REQUEST, f"unknown stream {stream!r}")
    return kind.from_json(data)  # type: ignore[attr-defined]


# ---------------------------------------------------------------------- verbs


@dataclass(frozen=True)
class Param:
    """One parameter, described well enough to validate and to publish."""

    name: str
    kind: str
    summary: str
    required: bool = True
    default: Any = None
    choices: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "name": self.name,
            "type": self.kind,
            "summary": self.summary,
            "required": self.required,
        }
        if self.default is not None:
            payload["default"] = self.default
        if self.choices:
            payload["choices"] = list(self.choices)
        return payload


@dataclass(frozen=True)
class Verb:
    module: str
    name: str
    summary: str
    params: tuple[Param, ...] = ()
    # Fire and forget: accepted for later execution on the frame loop, and
    # answered as soon as it is queued rather than when it lands.
    deferred: bool = False
    # Seconds a caller should be willing to wait. Published rather than assumed,
    # because the honest budget differs by an order of magnitude between reading
    # the status and reopening the cameras, and a client that guesses one number
    # for both either gives up too early or hangs for no reason.
    budget: float = 12.0

    @property
    def id(self) -> str:
        return f"{self.module}.{self.name}"

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "verb": self.id,
            "summary": self.summary,
            "params": [param.to_json() for param in self.params],
            "budget_s": self.budget,
        }
        if self.deferred:
            payload["deferred"] = True
        return payload


_BUTTON = ("left", "right")
_MODES = ("off", "active", "suspended")
# Opening cameras rebuilds the MediaPipe graphs, which is seconds rather than
# milliseconds, so the two verbs that do it get their own budget.
_SLOW = 35.0


VERBS: tuple[Verb, ...] = (
    Verb("status", "get", "Current pipeline status as one immutable snapshot."),
    Verb("modes", "get", "Which mode control is in, and whether hands are driving."),
    Verb(
        "modes",
        "set",
        "Put control into a mode outright.",
        (Param("mode", "str", "off, active or suspended", choices=_MODES),),
    ),
    Verb("modes", "toggle", "Flip between off and active, clearing any suspension."),
    Verb(
        "tracking",
        "subscribe",
        "Start receiving stream frames on this connection.",
        (
            Param(
                "streams",
                "str[]",
                f"any of {', '.join(STREAMS)}; omit for all of them",
                required=False,
            ),
            Param(
                "landmarks",
                "bool",
                "include the 21 hand points in the hands stream",
                required=False,
                default=False,
            ),
            Param(
                "interval_ms",
                "float",
                "minimum gap between frames of one stream; 0 sends every frame",
                required=False,
                default=0.0,
            ),
        ),
    ),
    Verb(
        "tracking",
        "unsubscribe",
        "Stop receiving named streams, or every stream when none are named.",
        (Param("streams", "str[]", "streams to drop; omit for all", required=False),),
    ),
    Verb(
        "input",
        "move_by",
        "Move the cursor by a pixel delta, through the native helper when it is up.",
        (Param("dx", "float", "pixels right"), Param("dy", "float", "pixels down")),
        deferred=True,
    ),
    Verb(
        "input",
        "move_to",
        "Warp the cursor to a fraction of the calibrated display.",
        (
            Param("x", "float", "0 at the left edge, 1 at the right"),
            Param("y", "float", "0 at the top edge, 1 at the bottom"),
        ),
        deferred=True,
    ),
    Verb(
        "input",
        "click",
        "Click, chaining into a double or triple click when repeated quickly.",
        (Param("button", "str", "left or right", required=False, default="left", choices=_BUTTON),),
        deferred=True,
    ),
    Verb(
        "input",
        "press",
        "Hold a button down. Ignored while one is already held.",
        (Param("button", "str", "left or right", required=False, default="left", choices=_BUTTON),),
        deferred=True,
    ),
    Verb(
        "input",
        "release",
        "Let go of a held button. Safe when nothing is held.",
        (
            Param(
                "button",
                "str",
                "left or right; omit for whatever is held",
                required=False,
                choices=_BUTTON,
            ),
        ),
        deferred=True,
    ),
    Verb(
        "input",
        "scroll",
        "Scroll by a pixel delta.",
        (Param("dx", "float", "pixels right"), Param("dy", "float", "pixels down")),
        deferred=True,
    ),
    Verb(
        "input",
        "key",
        "Tap a key chord, so a consumer can act on a gesture as well as read it.",
        (Param("action", "str", "a chord like cmd+shift+p, or a name from [keys]"),),
        deferred=True,
    ),
    Verb(
        "bindings",
        "get",
        "Every bindable gesture, the bindings in force, and what each means right now.",
        (
            Param(
                "app",
                "str",
                "resolve against this application; omit for whichever is in front",
                required=False,
            ),
        ),
    ),
    Verb(
        "bindings",
        "set",
        "Bind a gesture to a key chord, for one application or for all of them.",
        (
            Param("gesture", "str", "the gesture to bind", choices=BINDABLE),
            Param("action", "str", "a chord like cmd+[, a name from [keys], or none to mute it"),
            Param(
                "app",
                "str",
                "bundle id, app name, or its tail; omit to bind everywhere",
                required=False,
            ),
        ),
    ),
    Verb(
        "bindings",
        "clear",
        "Drop a binding, leaving the gesture to fall through or do nothing.",
        (
            Param("gesture", "str", "the gesture to unbind", choices=BINDABLE),
            Param(
                "app",
                "str",
                "the application scope to drop it from; omit for the global one",
                required=False,
            ),
        ),
    ),
    Verb("system", "calibrate", "Run the nine-point gaze calibration, releasing the cameras."),
    Verb(
        "system",
        "reload_config",
        "Re-read config.toml and adopt it without restarting.",
        budget=_SLOW,
    ),
    Verb("system", "pause", "Release the cameras, keeping the process up."),
    Verb(
        "system",
        "resume",
        "Reopen the cameras and pick up a new calibration.",
        budget=_SLOW,
    ),
    Verb("system", "describe", "This catalogue: every module, verb, parameter and stream."),
)

BY_ID: dict[str, Verb] = {verb.id: verb for verb in VERBS}

MODULES: tuple[str, ...] = tuple(dict.fromkeys(verb.module for verb in VERBS))


def lookup(verb_id: str) -> Verb:
    verb = BY_ID.get(verb_id)
    if verb is None:
        raise ApiError(UNKNOWN_VERB, f"no verb {verb_id!r}; try system.describe")
    return verb


def catalogue() -> dict[str, Any]:
    """The whole surface as data, which is what ``system.describe`` returns."""
    return {
        "protocol": PROTOCOL_VERSION,
        "modules": {
            module: [verb.to_json() for verb in VERBS if verb.module == module]
            for module in MODULES
        },
        "streams": [{"name": name, "summary": STREAM_SUMMARIES[name]} for name in STREAMS],
    }


def coerce(verb: Verb, params: dict[str, Any] | None) -> dict[str, Any]:
    """Check a parameter dict against a verb and return usable keyword arguments.

    Unknown keys are refused rather than ignored. A silently dropped parameter is
    the worst failure an API of this shape can have: the call succeeds, nothing
    happens, and the consumer has no way to find out why.
    """
    given = dict(params or {})
    if not isinstance(given, dict):  # pragma: no cover - defensive against odd JSON
        raise ApiError(BAD_PARAMS, "params must be an object")

    known = {param.name for param in verb.params}
    unknown = sorted(set(given) - known)
    if unknown:
        raise ApiError(
            BAD_PARAMS,
            f"{verb.id} does not take {', '.join(unknown)}; expected {', '.join(sorted(known))}"
            if known
            else f"{verb.id} takes no parameters",
        )

    resolved: dict[str, Any] = {}
    for param in verb.params:
        if param.name not in given or given[param.name] is None:
            if param.required:
                raise ApiError(BAD_PARAMS, f"{verb.id} needs {param.name}")
            resolved[param.name] = param.default
            continue
        resolved[param.name] = _convert(verb, param, given[param.name])
    return resolved


def _convert(verb: Verb, param: Param, value: Any) -> Any:
    if param.kind == "float":
        return _as_float(verb, param, value)
    if param.kind == "int":
        return int(_as_float(verb, param, value))
    if param.kind == "bool":
        if not isinstance(value, bool):
            raise ApiError(BAD_PARAMS, f"{verb.id}.{param.name} must be true or false")
        return value
    if param.kind == "str[]":
        return _as_strings(verb, param, value)
    return _as_string(verb, param, value)


def _as_float(verb: Verb, param: Param, value: Any) -> float:
    # Explicitly not accepting a numeric string: a consumer sending "12" has a
    # bug worth hearing about now rather than a coordinate worth guessing at.
    if isinstance(value, bool) or not isinstance(value, int | float):
        raise ApiError(BAD_PARAMS, f"{verb.id}.{param.name} must be a number")
    return float(value)


def _as_string(verb: Verb, param: Param, value: Any) -> str:
    if not isinstance(value, str):
        raise ApiError(BAD_PARAMS, f"{verb.id}.{param.name} must be a string")
    if param.choices and value not in param.choices:
        raise ApiError(
            BAD_PARAMS,
            f"{verb.id}.{param.name} must be one of {', '.join(param.choices)}",
        )
    return value


def _as_strings(verb: Verb, param: Param, value: Any) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, list | tuple):
        raise ApiError(BAD_PARAMS, f"{verb.id}.{param.name} must be a list of strings")
    return tuple(_as_string(verb, param, item) for item in value)


def resolve_streams(names: tuple[str, ...] | None) -> tuple[str, ...]:
    """Turn a requested stream list into a checked one; nothing means everything."""
    if not names:
        return STREAMS
    unknown = sorted(set(names) - set(STREAMS))
    if unknown:
        raise ApiError(
            BAD_PARAMS,
            f"no stream {', '.join(unknown)}; there is {', '.join(STREAMS)}",
        )
    return tuple(dict.fromkeys(names))


# ----------------------------------------------------------------- the wire


@dataclass(frozen=True)
class Request:
    """One call. ``id`` is echoed back so a client may have several in flight."""

    verb: str
    params: dict[str, Any] = field(default_factory=dict)
    id: int | None = None

    def to_json(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"verb": self.verb}
        if self.params:
            payload["params"] = self.params
        if self.id is not None:
            payload["id"] = self.id
        return payload

    @classmethod
    def from_json(cls, data: Any) -> Request:
        if not isinstance(data, dict):
            raise ApiError(BAD_REQUEST, "each line must be a JSON object")
        verb = data.get("verb")
        if not isinstance(verb, str):
            raise ApiError(BAD_REQUEST, "a request needs a verb")
        params = data.get("params") or {}
        if not isinstance(params, dict):
            raise ApiError(BAD_PARAMS, "params must be an object")
        identifier = data.get("id")
        if identifier is not None and not isinstance(identifier, int):
            raise ApiError(BAD_REQUEST, "id must be an integer")
        return cls(verb=verb, params=params, id=identifier)


def reply(identifier: int | None, result: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"ok": True, "result": result}
    if identifier is not None:
        payload["id"] = identifier
    return payload


def failure(identifier: int | None, error: ApiError) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": {"code": error.code, "message": error.message},
    }
    if identifier is not None:
        payload["id"] = identifier
    return payload


def event(stream: str, data: dict[str, Any], *, dropped: int = 0) -> dict[str, Any]:
    """A pushed stream frame.

    ``dropped`` is how many frames of this stream were discarded before this one
    because the consumer was not reading fast enough. Reported rather than
    hidden: for a pointing UX the difference between a slow feed and a lossy one
    is the difference between a bug in the app and a bug in the consumer.
    """
    payload: dict[str, Any] = {"stream": stream, "data": data}
    if dropped:
        payload["dropped"] = dropped
    return payload
