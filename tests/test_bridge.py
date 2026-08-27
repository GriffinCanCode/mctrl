"""The seam between Python and the native helper.

Two processes in two languages agreeing on a byte layout is exactly the kind of
contract that rots silently: nothing fails to compile, the frames simply stop
meaning what the reader thinks they mean. So the tests here read the Swift source
and hold it to the Python, then push real datagrams through a real socket to
confirm the agreement survives contact.

The rest is about what happens when the helper is not there. That has to be a
non-event -- the gesture pipeline predates the helper and still works without it.
"""

from __future__ import annotations

import json
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import fields
from pathlib import Path

import pytest

from mindcontrol.config import NativeConfig
from mindcontrol.control import bridge

NATIVE = Path(__file__).resolve().parent.parent / "native" / "Sources" / "BridgeCore"
# Stored properties only. The computed `borderCGColor` and friends are derived
# from the knobs rather than being knobs, and are not on the wire.
KNOB = re.compile(r"^ {4}var (\w+): \S+ = ", re.MULTILINE)


def camel(name: str) -> str:
    head, *rest = name.split("_")
    return head + "".join(word.title() for word in rest)


@pytest.fixture
def state(tmp_path_factory, monkeypatch):
    """A short-pathed stand-in for the state directory.

    Unix socket paths are capped near 104 bytes and pytest's own temporary paths
    are long enough to blow through it, so this borrows the system temp root
    directly and keeps the name to a few characters.
    """
    root = Path(tempfile.mkdtemp(prefix="mc"))
    monkeypatch.setattr(bridge, "STATE_DIR", root)
    monkeypatch.setattr(bridge, "SOCKET_PATH", root / "b.sock")
    monkeypatch.setattr(bridge, "TUNING_PATH", root / "tuning.json")
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


# --------------------------------------------------------------------- the contract


def test_frame_is_the_size_swift_expects():
    declared = int(
        re.search(r"byteCount = (\d+)", (NATIVE / "Protocol.swift").read_text()).group(1)
    )
    assert bridge._FRAME.size == declared


def test_intent_codes_agree_with_the_swift_enum():
    source = (NATIVE / "Protocol.swift").read_text()
    swift = {
        name: int(value)
        for name, value in re.findall(r"case (\w+) = (\d+)", source.split("struct Frame")[0])
    }
    ours = {
        camel(name.lower()): getattr(bridge, name)
        for name in dir(bridge)
        if name.isupper() and isinstance(getattr(bridge, name), int) and not name.startswith("_")
    }
    # Every intent the helper can act on must be reachable from here, with the
    # same number. A silent renumbering would turn clicks into scrolls.
    assert {name: ours[name] for name in swift} == swift


def test_mode_flags_agree_with_the_swift_option_set():
    pattern = r"static let (\w+) = ModeFlags\(rawValue: 1 << (\d)\)"
    swift = {
        name: 1 << int(shift)
        for name, shift in re.findall(pattern, (NATIVE / "Protocol.swift").read_text())
    }
    assert swift == {
        "engaged": bridge.ENGAGED,
        "pointing": bridge.POINTING,
        "sweeping": bridge.SWEEPING,
    }


def test_every_tuning_knob_reaches_the_helper():
    """NativeConfig and Tuning must name the same things.

    A field added on one side and forgotten on the other is invisible: Python
    writes a key nobody reads, or the helper keeps a default the config claims to
    have changed. Neither raises, and both look like the setting doing nothing.
    """
    swift = set(KNOB.findall((NATIVE / "Tuning.swift").read_text()))
    # 'enabled' is Python's decision about whether to run the helper at all, and
    # double_click_ms is bolted on from [gestures] rather than [native].
    ours = {camel(f.name) for f in fields(NativeConfig) if f.name != "enabled"} | {"doubleClickMs"}
    assert swift == ours


def test_tuning_file_carries_exactly_those_keys(state):
    bridge.Bridge(NativeConfig(), 400.0, spawn=False).write_tuning()
    written = json.loads((state / "tuning.json").read_text())
    assert {camel(key) for key in written} == set(
        KNOB.findall((NATIVE / "Tuning.swift").read_text())
    )


# ------------------------------------------------------------------- the transport


@pytest.fixture
def listener(state):
    """A socket standing in for the helper, at the path the client will dial."""
    with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as server:
        server.bind(str(bridge.SOCKET_PATH))
        server.settimeout(1.0)
        yield server


def unpack(server: socket.socket) -> dict:
    magic, version, intent, sequence, flags, a, b, sent_at, button, _ = bridge._FRAME.unpack(
        server.recv(bridge._FRAME.size)
    )
    assert magic == bridge._MAGIC and version == bridge._VERSION
    return {
        "intent": intent,
        "sequence": sequence,
        "flags": flags,
        "a": a,
        "b": b,
        "sent_at": sent_at,
        "button": button,
    }


@pytest.fixture
def connected(listener):
    handle = bridge.Bridge(NativeConfig(), 400.0, spawn=False)
    assert handle.start(), handle.error
    yield handle, listener
    handle.stop()


def test_intents_arrive_as_the_helper_decodes_them(connected):
    handle, server = connected

    handle.move_by(3.5, -2.25)
    frame = unpack(server)
    assert (frame["intent"], frame["a"], frame["b"]) == (bridge.MOVE_BY, 3.5, -2.25)

    handle.warp_to_fraction(0.25, 0.75)
    frame = unpack(server)
    assert (frame["intent"], frame["a"], frame["b"]) == (bridge.WARP_TO_FRACTION, 0.25, 0.75)

    handle.click("right")
    frame = unpack(server)
    assert (frame["intent"], frame["button"]) == (bridge.CLICK, 1)

    handle.press("left")
    assert unpack(server)["intent"] == bridge.PRESS
    handle.release("left")
    assert unpack(server)["intent"] == bridge.RELEASE

    handle.scroll(0.0, -4.0)
    frame = unpack(server)
    assert (frame["intent"], frame["b"]) == (bridge.SCROLL, -4.0)

    handle.release_all()
    assert unpack(server)["intent"] == bridge.RELEASE_ALL


def test_sequence_advances_so_loss_is_visible(connected):
    handle, server = connected
    handle.move_by(1.0, 0.0)
    handle.move_by(1.0, 0.0)
    first, second = unpack(server), unpack(server)
    assert second["sequence"] == first["sequence"] + 1


def test_mode_is_sent_on_change_and_not_otherwise(connected):
    """One datagram per transition, not one per frame.

    Mode is computed every camera frame and is the same on almost all of them.
    Sending it regardless would triple the traffic to say nothing.
    """
    handle, server = connected
    handle.set_mode(engaged=True, pointing=True, sweeping=False)
    assert unpack(server)["flags"] == bridge.ENGAGED | bridge.POINTING

    for _ in range(5):
        handle.set_mode(engaged=True, pointing=True, sweeping=False)
    server.settimeout(0.1)
    with pytest.raises(TimeoutError):
        server.recv(bridge._FRAME.size)

    server.settimeout(1.0)
    handle.set_mode(engaged=True, pointing=False, sweeping=True)
    assert unpack(server)["flags"] == bridge.ENGAGED | bridge.SWEEPING


def test_reload_follows_an_edited_config(connected, state):
    handle, server = connected
    handle.apply_config(NativeConfig(snap_radius=222.0), double_click_ms=250.0)

    assert unpack(server)["intent"] == bridge.RELOAD_CONFIG
    written = json.loads((state / "tuning.json").read_text())
    assert written["snap_radius"] == 222.0
    assert written["double_click_ms"] == 250.0


# ------------------------------------------------------------------ absent helper


def test_disabled_config_declines_without_complaint():
    handle = bridge.Bridge(NativeConfig(enabled=False), spawn=False)
    assert not handle.start()
    assert handle.error and "disabled" in handle.error
    assert not handle.connected


def test_missing_binary_says_how_to_get_one(monkeypatch):
    monkeypatch.setattr(bridge, "helper_path", lambda: None)
    handle = bridge.Bridge(NativeConfig(), spawn=False)
    assert not handle.start()
    assert handle.error and "mindcontrol bridge" in handle.error


def test_nothing_raises_at_a_dead_helper(state):
    """Sends into the void are dropped, not thrown.

    This runs inside the camera loop. A frame lost to a helper that has just
    died costs a fraction of a millimetre of cursor travel; an exception out of
    it would take the whole pipeline down with the sidecar, which is the one
    thing running the sidecar separately was supposed to prevent.
    """
    handle = bridge.Bridge(NativeConfig(), spawn=False)
    server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    server.bind(str(bridge.SOCKET_PATH))
    assert handle.start(), handle.error

    server.close()
    bridge.SOCKET_PATH.unlink()
    for _ in range(3):  # the first send notices, the rest are no-ops
        handle.move_by(1.0, 1.0)
        handle.click()
        handle.set_mode(engaged=True, pointing=True, sweeping=False)
    assert not handle.connected
    assert handle.error and "unreachable" in handle.error


def test_reconnection_backs_off(monkeypatch):
    """A helper that will not start must not be retried every frame."""
    monkeypatch.setattr(bridge, "helper_path", lambda: Path("/nonexistent/helper"))
    handle = bridge.Bridge(NativeConfig(), spawn=False)
    monkeypatch.setattr(handle, "_connect", lambda: False)

    assert not handle.reconnect()
    assert not handle.reconnect(), "the second attempt is inside the backoff window"
    assert handle._next_attempt > 0.0


# --------------------------------------------------------------- one helper only


@pytest.fixture
def helper(state):
    """The built helper binary, or a skip. Needs `mindcontrol bridge` to have run."""
    binary = bridge.helper_path()
    if binary is None:
        pytest.skip("native helper not built")
    bridge.Bridge(NativeConfig(), 400.0, spawn=False).write_tuning()
    started: list = []

    def launch():
        process = subprocess.Popen(
            [str(binary), "--socket", str(bridge.SOCKET_PATH), "--tuning", str(bridge.TUNING_PATH)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        started.append(process)
        return process

    try:
        yield launch
    finally:
        for process in started:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


def settled(process: subprocess.Popen, alive: bool, within: float = 8.0) -> bool:
    """Wait for a process to reach the state expected of it."""
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if (process.poll() is None) == alive:
            return True
        time.sleep(0.05)
    return (process.poll() is None) == alive


def ready(process: subprocess.Popen, within: float = 8.0) -> bool:
    """Wait until the helper is listening, not merely running.

    The socket is bound late in start-up, so its existence is the honest signal
    that everything before it -- the signal handlers, the claim -- is in place.
    Treating "the process exists" as ready is how the eviction test first managed
    to SIGTERM an incumbent that had not yet installed a handler for it.
    """
    deadline = time.monotonic() + within
    while time.monotonic() < deadline:
        if bridge.SOCKET_PATH.exists():
            return True
        if process.poll() is not None:
            return False
        time.sleep(0.05)
    return False


def test_a_second_helper_evicts_the_first(helper):
    """Two helpers is the cursor fighting itself, so there can only be one.

    The socket cannot express this: binding unlinks whatever was there, so a
    newcomer takes every frame and looks healthy while the incumbent keeps a live
    motion thread, a live probe and a second highlight window. Restarting is the
    common case, so the newcomer wins rather than refusing to start -- an orphan
    nobody can see must not be able to block every future launch.
    """
    first = helper()
    assert ready(first), "the first helper should come up listening"

    second = helper()
    assert settled(first, alive=False), "the incumbent should be asked to leave"
    assert settled(second, alive=True), "the newcomer should hold the claim"
    assert first.returncode == 0, "and should have left cleanly, not been killed"


def test_inspect_works_while_a_helper_is_running(helper):
    """`--inspect` reads; it never drives. Blocking it would remove the only way to
    diagnose a live cursor from outside the process."""
    running = helper()
    assert ready(running)

    done = subprocess.run(
        [str(bridge.helper_path()), "--inspect"], capture_output=True, text=True, timeout=30
    )
    assert done.returncode == 0, done.stderr
    assert "cursor at" in done.stdout
    assert running.poll() is None, "inspecting must not evict the helper"


def test_unfindable_helper_is_reported_as_missing(monkeypatch, tmp_path):
    monkeypatch.setenv("MINDCONTROL_BRIDGE", str(tmp_path / "not-here"))
    assert bridge.helper_path() is None

    real = tmp_path / "helper"
    real.write_bytes(b"")
    monkeypatch.setenv("MINDCONTROL_BRIDGE", str(real))
    assert bridge.helper_path() == real
