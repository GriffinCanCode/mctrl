"""Fan-out, and what happens when a consumer stops reading.

The single rule the hub exists to keep: the frame loop never waits. A camera
delivers regardless of what any consumer is doing, so a subscriber that stalls
has to lose its own frames rather than apply back pressure into the gesture
engine. Everything below is a way of asking whether that still holds.
"""

from __future__ import annotations

import threading
import time

from mindcontrol.api.hub import EventHub


def test_nobody_watching_costs_nothing():
    """The gate, which is why the API is free when it is not in use."""
    hub = EventHub()
    assert not hub.wanted
    hub.publish("hands", object())  # no subscriber, no work, no complaint
    assert hub.watching == 0


def test_a_subscriber_gets_only_what_it_asked_for():
    hub = EventHub()
    watcher = hub.subscribe(["gestures"])

    hub.publish("gestures", "click")
    hub.publish("hands", "ignored")

    assert [d.payload for d in watcher.take(0.5)] == ["click"]


def test_two_subscribers_do_not_share_a_buffer():
    hub = EventHub()
    first = hub.subscribe(["status"])
    second = hub.subscribe(["status"])

    hub.publish("status", "one")

    assert [d.payload for d in first.take(0.5)] == ["one"]
    assert [d.payload for d in second.take(0.5)] == ["one"]


def test_a_slow_subscriber_loses_its_oldest_and_is_told_how_many():
    """Drop the past, keep the present, count the difference.

    A consumer that comes back after a stall wants current data, not a minute of
    history it can no longer act on -- and it wants to know that is what happened,
    because a gap it cannot see is indistinguishable from the hand holding still.
    """
    hub = EventHub()
    watcher = hub.subscribe(["hands"], depth=3)

    for number in range(10):
        hub.publish("hands", number)

    drained = watcher.take(0.5)
    assert [d.payload for d in drained] == [7, 8, 9]
    # The tally has to survive its own carrier being evicted, so it is the sum
    # across what arrived that accounts for the loss, not any one frame's count.
    assert sum(d.dropped for d in drained) == 7


def test_a_stalled_subscriber_does_not_stall_the_publisher():
    """The whole point. One consumer's problem stays its own."""
    hub = EventHub()
    stalled = hub.subscribe(["hands"], depth=2)
    healthy = hub.subscribe(["hands"], depth=64)

    started = time.monotonic()
    for number in range(2000):
        hub.publish("hands", number)
    elapsed = time.monotonic() - started

    assert elapsed < 1.0, "publishing must not wait for anyone"
    assert len(healthy.take(0.5)) == 64
    assert len(stalled.take(0.5)) == 2


def test_a_throttle_thins_sampled_streams():
    hub = EventHub()
    watcher = hub.subscribe(["status"], interval_ms=50.0)

    for _ in range(5):
        hub.publish("status", "now")
    first = watcher.take(0.5)
    time.sleep(0.06)
    hub.publish("status", "later")
    second = watcher.take(0.5)

    assert len(first) == 1, "a burst inside the interval collapses to one frame"
    assert len(second) == 1, "and the next one lands once the interval has passed"


def test_a_throttle_never_thins_gestures():
    """Intents are discrete; there is no such thing as a sampled click.

    Throttling status is thinning a measurement. Throttling a gesture is losing
    the click, which the consumer can never recover.
    """
    hub = EventHub()
    watcher = hub.subscribe(["gestures"], interval_ms=1000.0)

    for action in ("drag_start", "drag_move", "drag_end"):
        hub.publish("gestures", action)

    assert [d.payload for d in watcher.take(0.5)] == ["drag_start", "drag_move", "drag_end"]


def test_landmarks_are_wanted_only_when_somebody_wants_them():
    hub = EventHub()
    plain = hub.subscribe(["hands"])
    assert not hub.wants_landmarks

    drawn = hub.subscribe(["hands"], landmarks=True)
    assert hub.wants_landmarks

    drawn.close()
    assert not hub.wants_landmarks
    assert hub.wants("hands"), "the plain subscriber still wants the stream"
    plain.close()
    assert not hub.wanted


def test_changing_streams_reopens_the_gate():
    hub = EventHub()
    watcher = hub.subscribe(["status"])
    assert not hub.wants("gaze")

    watcher.set_streams({"status", "gaze"})
    assert hub.wants("gaze")

    watcher.set_streams({"status"})
    assert not hub.wants("gaze")


def test_a_reader_waiting_for_a_frame_is_woken_by_one():
    hub = EventHub()
    watcher = hub.subscribe(["gestures"])
    seen: list[str] = []

    def read() -> None:
        seen.extend(d.payload for d in watcher.take(2.0))

    reader = threading.Thread(target=read)
    reader.start()
    time.sleep(0.05)
    hub.publish("gestures", "click")
    reader.join(timeout=2.0)

    assert seen == ["click"], "a blocked reader should be notified, not polled"


def test_closing_ends_the_iteration_rather_than_hanging():
    hub = EventHub()
    watcher = hub.subscribe(["hands"])
    drained: list[object] = []

    def read() -> None:
        drained.extend(watcher.frames())

    reader = threading.Thread(target=read)
    reader.start()
    time.sleep(0.05)
    hub.publish("hands", "one")
    watcher.close()
    reader.join(timeout=2.0)

    assert not reader.is_alive(), "a closed subscriber must let its reader go"
    assert [d.payload for d in drained] == ["one"]


def test_closing_the_hub_lets_everyone_go():
    hub = EventHub()
    first = hub.subscribe(["hands"])
    second = hub.subscribe(["status"])

    hub.close()

    assert first.closed and second.closed
    assert not hub.wanted
    hub.publish("hands", "nobody home")
