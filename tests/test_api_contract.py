"""The contract, on its own.

Everything here runs with no pipeline, no camera and no macOS frameworks, which
is the point: the protocol is a thing another program can hold us to, so it has
to be checkable without starting the program.

Two failures are worth more attention than the rest. A parameter that is
accepted and then ignored, because the call succeeds while nothing happens and
the consumer has no way to find out why. And a snapshot that does not survive
JSON, because the in-process facade and the socket would then disagree about what
the same verb returns.
"""

from __future__ import annotations

import numpy as np
import pytest

from mindcontrol.api import contract as api
from mindcontrol.fusion import FusedHand
from mindcontrol.pipeline import PipelineStatus

# ------------------------------------------------------------------- the catalogue


def test_every_verb_is_listed_once_and_documented():
    listed = [verb["verb"] for verbs in api.catalogue()["modules"].values() for verb in verbs]
    assert listed == [verb.id for verb in api.VERBS]
    assert len(set(listed)) == len(listed)
    for verb in api.VERBS:
        assert verb.summary.endswith("."), f"{verb.id} should read as a sentence"
        for param in verb.params:
            assert param.summary, f"{verb.id}.{param.name} needs a summary"


def test_the_catalogue_publishes_how_long_to_wait():
    """A client that guesses one timeout for every verb picks a bad one.

    Reading the status is instant and reopening the cameras rebuilds the
    MediaPipe graphs, so a single number either abandons the slow verb or hangs
    for half a minute on a typo. The budget is therefore part of the contract.
    """
    budgets = {
        verb["verb"]: verb["budget_s"]
        for verbs in api.catalogue()["modules"].values()
        for verb in verbs
    }
    assert budgets["system.resume"] > budgets["status.get"]
    assert budgets["system.reload_config"] > budgets["status.get"]


def test_every_stream_is_named_summarised_and_typed():
    named = [stream["name"] for stream in api.catalogue()["streams"]]
    assert named == list(api.STREAMS)
    assert set(api.STREAM_TYPES) == set(api.STREAMS)
    assert set(api.STREAM_SUMMARIES) == set(api.STREAMS)
    with pytest.raises(api.ApiError):
        api.decode_stream("elbows", {})


# ------------------------------------------------------------------- parameters


def test_a_missing_parameter_is_refused():
    with pytest.raises(api.ApiError) as raised:
        api.coerce(api.lookup("input.move_by"), {"dx": 1.0})
    assert raised.value.code == api.BAD_PARAMS
    assert "dy" in raised.value.message


def test_an_unknown_parameter_is_refused_rather_than_dropped():
    """Silently ignoring it is the worst available outcome.

    The call would succeed, the cursor would not move, and the consumer would
    have nothing to go on. A misspelled key is a bug, and it is cheapest to hear
    about at the moment it is sent.
    """
    with pytest.raises(api.ApiError) as raised:
        api.coerce(api.lookup("input.move_by"), {"dx": 1.0, "dy": 2.0, "speed": 3.0})
    assert raised.value.code == api.BAD_PARAMS
    assert "speed" in raised.value.message


def test_choices_are_enforced():
    with pytest.raises(api.ApiError):
        api.coerce(api.lookup("modes.set"), {"mode": "sideways"})
    assert api.coerce(api.lookup("modes.set"), {"mode": "active"}) == {"mode": "active"}


def test_a_number_must_be_a_number():
    for value in ("12", True, None, [1]):
        with pytest.raises(api.ApiError):
            api.coerce(api.lookup("input.move_by"), {"dx": value, "dy": 0.0})


def test_defaults_are_filled_in():
    assert api.coerce(api.lookup("input.click"), None) == {"button": "left"}
    assert api.coerce(api.lookup("input.release"), None) == {"button": None}


def test_a_verb_with_no_parameters_says_so():
    with pytest.raises(api.ApiError) as raised:
        api.coerce(api.lookup("status.get"), {"since": 1})
    assert "no parameters" in raised.value.message


def test_an_unknown_verb_points_at_the_catalogue():
    with pytest.raises(api.ApiError) as raised:
        api.lookup("input.teleport")
    assert raised.value.code == api.UNKNOWN_VERB
    assert "system.describe" in raised.value.message


def test_streams_default_to_all_of_them_and_reject_inventions():
    assert api.resolve_streams(None) == api.STREAMS
    assert api.resolve_streams(("hands", "hands")) == ("hands",)
    with pytest.raises(api.ApiError):
        api.resolve_streams(("elbows",))


# ---------------------------------------------------------------------- the wire


def test_a_request_needs_a_verb():
    with pytest.raises(api.ApiError):
        api.Request.from_json(["status.get"])
    with pytest.raises(api.ApiError):
        api.Request.from_json({"params": {}})
    with pytest.raises(api.ApiError):
        api.Request.from_json({"verb": "status.get", "params": 7})
    request = api.Request.from_json({"verb": "status.get", "id": 4})
    assert (request.verb, request.id, request.params) == ("status.get", 4, {})


def test_a_reply_carries_the_id_it_answers():
    assert api.reply(9, {"ok": 1})["id"] == 9
    assert "id" not in api.reply(None, {})
    failed = api.failure(2, api.ApiError(api.BUSY, "later"))
    assert failed == {"ok": False, "id": 2, "error": {"code": "busy", "message": "later"}}


def test_a_stream_frame_reports_what_went_missing():
    """Loss is told, not hidden.

    For anything driving a pointer, a feed that is merely slow and a feed that is
    dropping frames call for opposite responses, and only the second is the
    consumer's own fault.
    """
    assert "dropped" not in api.event("hands", {})
    assert api.event("hands", {}, dropped=3)["dropped"] == 3


# ----------------------------------------------------------------- the snapshots


def test_status_survives_the_round_trip():
    live = PipelineStatus(
        fps=29.97,
        mode="active",
        gesture="pointing [ready] Right",
        hands=2,
        cameras=(0, 1),
        merged=True,
        gaze_ready=True,
        gaze_point=(0.25, 0.75),
        warps=4,
        native=True,
        problems=["camera 2 unavailable"],
    )
    snapshot = api.StatusSnapshot.of(live)
    assert api.StatusSnapshot.from_json(snapshot.to_json()) == snapshot


def test_a_snapshot_does_not_change_under_the_frame_loop():
    """The live status is one object rewritten in place, thirty times a second.

    Handing it out would give every consumer a racing view of a different frame,
    so what leaves is a copy -- including the mutable problem list.
    """
    live = PipelineStatus(mode="off", problems=["one"])
    snapshot = api.StatusSnapshot.of(live)

    live.mode = "active"
    live.problems.append("two")

    assert snapshot.mode == "off"
    assert snapshot.problems == ("one",)


def hand(gestures, camera_id: int = 0, cameras: tuple[int, ...] = (0,)) -> FusedHand:
    from conftest import synthetic

    return FusedHand(
        features=synthetic(gestures), camera_id=camera_id, cameras=cameras, rebased=False
    )


def test_a_hand_survives_the_round_trip(cfg):
    snapshot = api.HandSnapshot.of(hand(cfg.gestures, cameras=(0, 1)))
    assert snapshot.merged
    assert api.HandSnapshot.from_json(snapshot.to_json()) == snapshot


def test_landmarks_are_left_out_unless_asked_for(cfg):
    """They are most of the payload and least of the interest.

    Twenty-one points per hand per frame dwarfs everything else on the wire, and a
    consumer reacting to a pinch wants the pose label, not the skeleton.
    """
    plain = api.HandSnapshot.of(hand(cfg.gestures))
    assert plain.landmarks == ()
    assert "landmarks" not in plain.to_json()

    drawn = api.HandSnapshot.of(hand(cfg.gestures), landmarks=True)
    assert len(drawn.landmarks) == 21
    assert len(drawn.to_json()["landmarks"]) == 21
    assert "landmarks" not in drawn.to_json(landmarks=False)


def test_a_frame_of_hands_survives_the_round_trip(cfg):
    frame = api.HandsFrame(
        t=1.5,
        camera_id=0,
        sequence=91,
        timestamp_ms=1234,
        hands=(api.HandSnapshot.of(hand(cfg.gestures), landmarks=True),),
    )
    assert api.HandsFrame.from_json(frame.to_json()) == frame
    assert api.decode_stream("hands", frame.to_json()) == frame


def test_gaze_and_gestures_survive_the_round_trip():
    gaze = api.GazeSnapshot(ready=True, point=(0.5, 0.25), warps=2)
    assert api.decode_stream("gaze", gaze.to_json()) == gaze

    from mindcontrol.gestures.engine import Action, GestureEvent

    event = api.GestureEventMsg.of(GestureEvent(Action.CLICK, button="right"))
    assert event.action == "click"
    assert api.decode_stream("gestures", event.to_json()) == event


def test_measurements_reach_a_consumer_in_the_units_the_config_uses(cfg):
    """Palm spans, not pixels.

    A consumer comparing a reported pinch against `pinch_close` from config.toml
    has to be comparing the same quantity the engine compares, or the number is
    decorative.
    """
    features = hand(cfg.gestures).features
    snapshot = api.HandSnapshot.of(hand(cfg.gestures))
    assert snapshot.pinch == pytest.approx(features.pinch, abs=1e-5)
    assert snapshot.pinch_index == pytest.approx(features.pinch_index, abs=1e-5)
    assert snapshot.pose == features.pose.value
    assert np.isclose(snapshot.spread, features.spread, atol=1e-5)
