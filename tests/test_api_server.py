"""The API end to end: a real pipeline, a real socket, a real client.

The pipeline here is the real one with no cameras configured, so its frame loop
genuinely runs -- it simply never finds an image. That is what makes the
threading claims testable: work queued from a socket thread really does execute
on the loop, and the answer says which thread it landed on.

Frames are then pushed through the hooks by hand, exactly as the loop would, so
the path from a fused hand to a decoded snapshot in a consumer is exercised
whole rather than in halves.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from mindcontrol.api import MindControl
from mindcontrol.api import contract as api
from mindcontrol.api.client import Client
from mindcontrol.api.facade import Local
from mindcontrol.api.runtime import Runtime
from mindcontrol.api.server import ApiServer
from mindcontrol.camera.capture import Frame
from mindcontrol.config import ApiConfig
from mindcontrol.control.modes import Mode
from mindcontrol.gestures.engine import Action, GestureEvent
from mindcontrol.gestures.fusion import FusedHand
from mindcontrol.pipeline import Pipeline


class Cursor:
    """Stands in for the real one, remembering what it was told and by whom.

    The thread name is the interesting half: it is how a test can tell that a
    verb arriving on a socket was marshalled onto the frame loop rather than
    posting events from wherever it happened to arrive.
    """

    dragging = False

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple, str]] = []

    def __getattr__(self, name: str):
        def record(*args, **kwargs) -> None:
            self.calls.append((name, args, threading.current_thread().name))

        return record

    def named(self) -> list[str]:
        return [name for name, _, _ in self.calls]

    def wait_for(self, name: str, within: float = 2.0) -> tuple[str, tuple, str]:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            for call in self.calls:
                if call[0] == name:
                    return call
            time.sleep(0.01)
        raise AssertionError(f"{name} never reached the cursor; saw {self.named()}")


@pytest.fixture
def short_root():
    """A short directory, because a Unix socket path is capped near 104 bytes."""
    root = Path(tempfile.mkdtemp(prefix="mc"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


@pytest.fixture
def bare_config(cfg):
    """The shipped config with nothing that needs hardware.

    No cameras, no event tap, no native helper -- everything else is exactly what
    ships, so the thresholds and timings under test are the real ones.
    """
    return replace(
        cfg,
        cameras=replace(cfg.cameras, devices=[]),
        modes=replace(cfg.modes, suspend_on_physical_input=False, start_engaged=False),
        native=replace(cfg.native, enabled=False),
    )


@pytest.fixture
def pipeline(bare_config):
    """The real pipeline with nothing to look at.

    No cameras means no MediaPipe and no images, but the loop, the command queue
    and the hooks are all the shipped ones.
    """
    running = Pipeline(bare_config)
    running.start()
    yield running
    running.stop()


@pytest.fixture
def runtime(pipeline):
    live = Runtime(pipeline)
    live.attach()
    yield live
    live.close()


@pytest.fixture
def served(runtime, short_root):
    path = short_root / "a.sock"
    server = ApiServer(runtime, ApiConfig(enabled=True, socket=str(path)))
    assert server.start(), server.error
    yield server, path
    server.stop()


@pytest.fixture
def mc(served):
    _, path = served
    with MindControl.connect(path) as connected:
        yield connected


class Raw:
    """A socket with no client library on it, for testing the wire itself."""

    def __init__(self, path: Path) -> None:
        self._handle = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._handle.settimeout(3.0)
        self._handle.connect(str(path))
        self._stream = self._handle.makefile("rwb")
        self.greeting = json.loads(self._stream.readline())

    def send(self, line: bytes) -> None:
        self._stream.write(line + b"\n")
        self._stream.flush()

    def reply(self) -> dict:
        return json.loads(self._stream.readline())

    def close(self) -> None:
        self._stream.close()
        self._handle.close()


@pytest.fixture
def raw(served):
    _, path = served
    connection = Raw(path)
    yield connection
    connection.close()


def a_hand(gestures, camera_id: int = 0, cameras: tuple[int, ...] = (0,)) -> FusedHand:
    from conftest import synthetic

    return FusedHand(
        features=synthetic(gestures), camera_id=camera_id, cameras=cameras, rebased=False
    )


def push(pipeline: Pipeline, hands: list[FusedHand]) -> None:
    """Deliver one frame through the hooks, as the loop does."""
    frame = Frame(
        camera_id=0,
        image=np.zeros((4, 4, 3), dtype=np.uint8),
        timestamp_ms=1234,
        sequence=7,
    )
    assert pipeline.frame_hook is not None
    pipeline.frame_hook(frame, hands, pipeline.status)


# ------------------------------------------------------------------ the frame loop


def test_queued_work_runs_on_the_frame_loop(pipeline):
    """Which is the whole basis of the input verbs being safe to expose.

    The cursor, the gesture engine and the socket to the native helper have one
    writer by construction. A verb that posted events from a socket thread would
    quietly give up that guarantee.
    """
    where = pipeline.submit(lambda: threading.current_thread().name, timeout=2.0)
    assert where == "pipeline"


def test_a_failed_command_is_reported_and_does_not_end_the_loop(pipeline):
    """Carried back to whoever asked, rather than raised into the frame loop.

    Otherwise one consumer's bad call takes the tracker down with it, which is
    exactly the failure the queue is meant to contain.
    """

    def unhappy() -> None:
        raise ValueError("no")

    with pytest.raises(ValueError, match="no"):
        pipeline.submit(unhappy, timeout=2.0)

    assert pipeline.running
    assert pipeline.submit(lambda: "still here", timeout=2.0) == "still here"


def test_work_queued_while_paused_still_runs(pipeline):
    """Because resuming is itself queued work, and a paused loop must accept it."""
    pipeline.submit(pipeline.pause, timeout=2.0)
    try:
        assert pipeline.submit(lambda: "drained", timeout=2.0) == "drained"
    finally:
        pipeline.submit(pipeline.resume, timeout=10.0)


def test_gestures_are_announced_before_they_are_acted_on(pipeline):
    """A consumer should hear about a click no later than the system does."""
    seen: list[list[GestureEvent]] = []
    order: list[str] = []
    cursor = Cursor()
    pipeline.mouse = cursor

    def hook(events: list[GestureEvent]) -> None:
        seen.append(events)
        order.append("announced")

    pipeline.gesture_hook = hook
    pipeline._dispatch([GestureEvent(Action.CLICK, button="right")])
    order.append("acted")

    assert [event.action for event in seen[0]] == [Action.CLICK]
    assert order == ["announced", "acted"]
    assert cursor.named() == ["click"]


def test_a_mode_change_from_outside_still_releases_the_button(pipeline):
    """The engage gesture and an API call go through one door on purpose.

    A held button that survives a mode change is the one failure that makes the
    machine unusable, and it must not depend on which route asked.
    """
    cursor = Cursor()
    pipeline.mouse = cursor
    pipeline.submit(lambda: pipeline.apply_mode(Mode.ACTIVE), timeout=2.0)

    pipeline.submit(lambda: pipeline.apply_mode(Mode.OFF), timeout=2.0)

    assert "release" in cursor.named()
    assert pipeline.modes.mode is Mode.OFF


# --------------------------------------------------------------------- the socket


def test_the_socket_is_user_only(served):
    """It can move the cursor, so it is nobody else's business."""
    _, path = served
    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode & (stat.S_IRWXG | stat.S_IRWXO) == 0, oct(mode)


def test_a_client_is_greeted_with_the_protocol_version(served):
    _, path = served
    with Client(path).open() as client:
        deadline = time.monotonic() + 2.0
        while not client.greeting and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.greeting == {"protocol": api.PROTOCOL_VERSION, "app": "mindcontrol"}
        assert client.error is None


def test_a_second_server_refuses_rather_than_stealing_the_socket(runtime, served):
    """Binding unlinks whatever is there, which would take the API away silently."""
    _, path = served
    intruder = ApiServer(runtime, ApiConfig(enabled=True, socket=str(path)))
    assert not intruder.start()
    assert intruder.error and "already serving" in intruder.error


def test_a_disabled_api_declines_without_complaint(runtime, short_root):
    server = ApiServer(runtime, ApiConfig(enabled=False, socket=str(short_root / "off.sock")))
    assert not server.start()
    assert server.error and "disabled" in server.error


def test_connecting_to_nothing_says_what_to_check(short_root):
    with pytest.raises(api.ApiError) as raised:
        Client(short_root / "absent.sock").open()
    assert raised.value.code == api.UNAVAILABLE
    assert "config.toml" in raised.value.message


# ---------------------------------------------------------------------- the verbs


def test_every_declared_verb_is_implemented(runtime):
    """The catalogue is a promise. Nothing may be listed and unbound."""
    assert set(runtime.session()._handlers) == set(api.BY_ID)


def test_status_arrives_as_a_snapshot(mc, pipeline):
    pipeline.status.gesture = "pointing [ready] Right"
    pipeline.status.hands = 2

    snapshot = mc.status()

    assert isinstance(snapshot, api.StatusSnapshot)
    assert snapshot.gesture == "pointing [ready] Right"
    assert snapshot.hands == 2


def test_modes_can_be_read_and_driven(mc, pipeline):
    assert mc.modes.mode == "off"

    mc.modes.engage()
    assert mc.modes.engaged
    assert pipeline.modes.mode is Mode.ACTIVE

    mc.modes.disengage()
    assert not mc.modes.engaged


def test_describe_is_the_same_catalogue_the_server_dispatches_from(mc):
    published = mc.system.describe()
    assert published == api.catalogue()


def test_an_unknown_verb_comes_back_as_a_code(served):
    """Checked locally as well, so a typo costs no round trip."""
    _, path = served
    with Client(path).open() as client:
        with pytest.raises(api.ApiError) as raised:
            client.call("input.teleport", {"x": 1})
        assert raised.value.code == api.UNKNOWN_VERB


def test_bad_parameters_come_back_as_a_code_from_the_far_end(raw):
    """Sent raw, bypassing the local check, to prove the server checks too."""
    raw.send(b'{"id":1,"verb":"input.move_by","params":{"dx":"far"}}')
    answer = raw.reply()

    assert answer["ok"] is False
    assert answer["error"]["code"] == api.BAD_PARAMS
    assert answer["id"] == 1


def test_malformed_json_does_not_drop_the_connection(raw):
    raw.send(b"{not json")
    assert raw.reply()["error"]["code"] == api.BAD_REQUEST

    raw.send(b'{"id":2,"verb":"status.get"}')
    assert raw.reply()["ok"] is True, "one bad line is not a reason to hang up"


def test_input_is_marshalled_onto_the_frame_loop(mc, pipeline):
    cursor = Cursor()
    pipeline.mouse = cursor

    mc.input.click("right")
    _, args, thread = cursor.wait_for("click")

    assert args == ("right",)
    assert thread == "pipeline", "a socket thread must not post events itself"


def test_every_input_verb_reaches_the_cursor(mc, pipeline):
    cursor = Cursor()
    pipeline.mouse = cursor

    mc.input.move_by(4.0, -2.0)
    mc.input.move_to(0.25, 0.5)
    mc.input.press("left")
    mc.input.release()
    mc.input.scroll(0.0, 6.0)

    cursor.wait_for("scroll")
    assert [(name, args) for name, args, _ in cursor.calls] == [
        ("move_by", (4.0, -2.0)),
        ("move_to_fraction", (0.25, 0.5)),
        ("press", ("left",)),
        ("release", (None,)),
        ("scroll", (0.0, 6.0)),
    ]


def test_pause_and_resume_are_answered(mc, pipeline):
    assert mc.system.pause() == {"paused": True}
    assert mc.system.resume() == {"paused": False}
    assert pipeline.running


# -------------------------------------------------------------------- the streams


def test_hands_reach_a_consumer_as_typed_snapshots(mc, pipeline, cfg):
    stream = mc.tracking.events(["hands"], timeout=2.0)
    push(pipeline, [a_hand(cfg.gestures)])

    delivery = next(stream)

    assert delivery.stream == "hands"
    assert isinstance(delivery.payload, api.HandsFrame)
    assert delivery.payload.sequence == 7
    assert delivery.payload.hands[0].pose == "ready"
    assert delivery.payload.hands[0].landmarks == (), "not asked for, not sent"


def test_landmarks_arrive_when_asked_for(mc, pipeline, cfg):
    stream = mc.tracking.events(["hands"], timeout=2.0, landmarks=True)
    push(pipeline, [a_hand(cfg.gestures)])

    delivery = next(stream)

    assert len(delivery.payload.hands[0].landmarks) == 21


def test_gestures_reach_a_consumer_the_moment_the_engine_decides(mc, pipeline):
    intents = mc.tracking.gestures(timeout=2.0)
    assert pipeline.gesture_hook is not None
    pipeline.gesture_hook(
        [GestureEvent(Action.SWIPE_LEFT), GestureEvent(Action.CLICK, button="right")]
    )

    first, second = next(intents), next(intents)

    assert (first.action, second.action) == ("swipe_left", "click")
    assert second.button == "right"


def test_a_subscription_is_additive(mc, pipeline, cfg):
    """Asking for one more stream must not silently stop the ones already flowing."""
    assert mc.tracking.subscribe(["status"])["streams"] == ["status"]
    assert mc.tracking.subscribe(["hands"])["streams"] == ["status", "hands"]

    assert mc.tracking.unsubscribe(["status"])["streams"] == ["hands"]
    assert mc.tracking.unsubscribe()["streams"] == []


def test_unsubscribing_stops_the_frames(raw, pipeline, cfg):
    raw.send(b'{"id":1,"verb":"tracking.subscribe","params":{"streams":["hands"]}}')
    assert raw.reply()["result"]["streams"] == ["hands"]
    push(pipeline, [a_hand(cfg.gestures)])
    assert raw.reply()["stream"] == "hands"

    raw.send(b'{"id":2,"verb":"tracking.unsubscribe"}')
    assert raw.reply()["result"]["streams"] == []

    push(pipeline, [a_hand(cfg.gestures)])
    raw.send(b'{"id":3,"verb":"status.get"}')
    answer = raw.reply()
    assert answer.get("id") == 3, "a dropped stream must send nothing further"


def test_the_status_stream_can_be_thinned(mc, pipeline):
    """Thirty status frames a second is noise for a consumer watching the mode."""
    stream = mc.tracking.events(["status"], timeout=0.5, interval_ms=1000.0)
    for _ in range(20):
        push(pipeline, [])

    assert len(list(stream)) == 1, "a burst inside the interval collapses to one frame"


# --------------------------------------------------------------- in this process


def test_the_embedded_facade_answers_the_same_verbs(bare_config):
    """launch() and connect() differ in one line, and must not differ in answers.

    A consumer written against a pipeline in its own process should work
    unchanged against one in the menu-bar app, which is the whole reason both
    sides dispatch from the same contract.
    """
    with MindControl.launch(bare_config) as mc:
        assert isinstance(mc.status(), api.StatusSnapshot)
        assert mc.modes.mode == "off"
        assert mc.system.describe() == api.catalogue()
        assert mc.input.click() == {"queued": True}
        assert _loops() == 1

    assert _loops() == 0, "closing the facade should take its pipeline with it"


def _loops() -> int:
    return sum(1 for thread in threading.enumerate() if thread.name == "pipeline")


def test_the_embedded_facade_streams_without_serialising(runtime, pipeline, cfg):
    """Same verbs, same snapshots, and no JSON anywhere in between."""
    mc = MindControl(Local(runtime))
    stream = mc.tracking.events(["hands"], timeout=2.0, landmarks=True)
    delivered = a_hand(cfg.gestures)
    push(pipeline, [delivered])

    delivery = next(stream)
    mc.close()

    assert isinstance(delivery.payload, api.HandsFrame)
    assert delivery.payload.hands[0].pose == delivered.features.pose.value
    assert len(delivery.payload.hands[0].landmarks) == 21


def test_a_consumer_that_leaves_does_not_disturb_the_loop(served, pipeline, cfg):
    """A socket whose peer wandered off must cost the pipeline nothing."""
    _, path = served
    client = Client(path).open()
    client.call("tracking.subscribe", {"streams": ["hands"]})
    client.close()

    for _ in range(200):
        push(pipeline, [a_hand(cfg.gestures)])

    assert pipeline.running
    assert pipeline.submit(lambda: "unbothered", timeout=2.0) == "unbothered"
