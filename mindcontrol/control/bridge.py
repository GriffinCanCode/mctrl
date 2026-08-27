"""Client for the native interaction helper.

The helper (``native/``, Swift) owns the cursor. This module is the near end of the
socket to it: it finds the binary, keeps it running, and turns each gesture intent
into one 48-byte datagram.

Why the work moved out of Python at all is a measurement, not a preference. The
snapping this exists to serve needs to ask the system what is on screen, and
accessibility queries are synchronous IPC into other applications:

===========================================  ==========  ==========
operation                                    pyobjc      native
===========================================  ==========  ==========
one attribute read                              1140 us      382 us
whole-window tree walk (2419 nodes)             2490 ms     4251 ms
single-point hit test                                 -     0.43 ms
===========================================  ==========  ==========

Walking a window is hopeless in either language. The hit test is affordable in
both, but it is only 3x cheaper here -- the rest is IPC, which no language avoids.
What actually cannot be done in this process is the other half: integrating the
cursor at display rate on a thread that is never behind the GIL while MediaPipe is
running inference, and holding the sole write handle on the cursor so that a warp
and a hand delta can never be posted from two threads against the same stale
position.

Sending is fire and forget. Deltas travel rather than positions, so nothing here
ever needs to read the cursor back, which keeps the round trip count at zero.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from ..config import STATE_DIR, NativeConfig

SOCKET_PATH = STATE_DIR / "bridge.sock"
TUNING_PATH = STATE_DIR / "bridge-tuning.json"

_MAGIC = 0x4D494E44  # "MIND", the same tag the synthetic events carry
_VERSION = 1
# magic, version, intent, sequence, flags, a, b, sent_at, button, pad
_FRAME = struct.Struct("<IHHIIdddII")

MOVE_BY = 1
WARP_TO_FRACTION = 2
CLICK = 3
PRESS = 4
RELEASE = 5
SCROLL = 6
SET_MODE = 7
RELEASE_ALL = 8
RELOAD_CONFIG = 9
SHUTDOWN = 10

ENGAGED = 1 << 0
POINTING = 1 << 1
SWEEPING = 1 << 2

_BUTTONS = {"left": 0, "right": 1}


def helper_path() -> Path | None:
    """Locate the helper binary, preferring an explicit override.

    The release build is checked before the debug one so that a stale debug
    binary left over from development does not quietly win.
    """
    override = os.environ.get("MINDCONTROL_BRIDGE")
    if override:
        candidate = Path(override).expanduser()
        return candidate if candidate.is_file() else None

    root = Path(__file__).resolve().parents[2] / "native" / ".build"
    for build in ("release", "debug"):
        candidate = root / build / "mindcontrol-bridge"
        if candidate.is_file():
            return candidate
    return None


def package_dir() -> Path:
    """The Swift package, which lives beside the Python one."""
    return Path(__file__).resolve().parents[2] / "native"


def build_hint() -> str:
    return "run `mindcontrol bridge` to build it"


def build(*, release: bool = True) -> int:
    """Compile the helper. Returns swift's exit status, or 127 if it is absent."""
    package = package_dir()
    if not (package / "Package.swift").is_file():
        print(f"[bridge] no Swift package at {package}")
        return 2
    command = ["swift", "build", "--package-path", str(package)]
    if release:
        command += ["-c", "release"]
    print(f"[bridge] {' '.join(command)}")
    try:
        return subprocess.run(command, check=False).returncode
    except FileNotFoundError:
        print(
            "[bridge] swift not found. Install the Xcode command line tools with "
            "`xcode-select --install`."
        )
        return 127


def run(*, rebuild: bool = False, debug: bool = False) -> int:
    """CLI entry: build the helper if needed, then report where it stands."""
    if rebuild or helper_path() is None:
        status = build(release=not debug)
        if status != 0:
            return status

    binary = helper_path()
    if binary is None:
        print("[bridge] built, but no binary was produced")
        return 1
    print(f"[bridge] helper at {binary}")

    trusted = _accessibility_granted()
    if trusted is None:
        print("[bridge] could not check Accessibility permission")
    elif trusted:
        print("[bridge] Accessibility permission granted to this process")
    else:
        print(
            "[bridge] no Accessibility permission yet. Snapping and highlighting need it.\n"
            "         The helper asks for it in its own right the first time it runs, so\n"
            "         look for it in System Settings > Privacy & Security > Accessibility."
        )
    return 0


def _accessibility_granted() -> bool | None:
    try:
        from ApplicationServices import AXIsProcessTrusted
    except ImportError:
        return None
    return bool(AXIsProcessTrusted())


class Bridge:
    """Owns the helper process and the socket to it.

    Every method is safe to call when the helper is absent or has died; the caller
    checks :attr:`connected` to decide whether to fall back to posting events
    itself. Nothing here raises on a send failure -- a dropped frame costs a
    fraction of a millimetre of cursor travel, and tearing down the pipeline over
    one would be a far worse outcome.
    """

    def __init__(self, cfg: NativeConfig, double_click_ms: float = 400.0, *, spawn: bool = True):
        self._cfg = cfg
        # Lives in [gestures] rather than [native], because it is a property of how
        # you pinch, not of the helper. The helper needs it because it is the side
        # that stamps the click-state field two quick pinches chain through.
        self._double_click_ms = double_click_ms
        self._spawn = spawn
        self._socket: socket.socket | None = None
        self._process: subprocess.Popen[bytes] | None = None
        self._sequence = 0
        self._flags = -1
        self._next_attempt = 0.0
        self._attempts = 0
        self.error: str | None = None

    # ------------------------------------------------------------------ lifecycle

    def start(self) -> bool:
        """Write the tuning file, start the helper, and connect. False if unavailable."""
        if not self._cfg.enabled:
            self.error = "native bridge disabled in config"
            return False
        binary = helper_path()
        if binary is None:
            self.error = f"native helper not built; {build_hint()}"
            return False

        self.write_tuning()
        if self._spawn and not self._launch(binary):
            return False
        return self._connect()

    def _launch(self, binary: Path) -> bool:
        if self._process is not None and self._process.poll() is None:
            return True
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            self._process = subprocess.Popen(
                [
                    str(binary),
                    "--socket",
                    str(SOCKET_PATH),
                    "--tuning",
                    str(TUNING_PATH),
                ],
                stdout=subprocess.DEVNULL,
                # The helper reports permission problems and its listening address
                # on stderr; inheriting it puts those where the user is looking.
                stderr=None,
            )
        except OSError as problem:
            self.error = f"could not start native helper: {problem}"
            return False
        return True

    # Longer than the helper's own eviction grace, which is the slowest thing that
    # can stand between launching it and it listening: a helper that is patiently
    # waiting for a previous one to stand down must not be given up on as broken.
    _CONNECT_TIMEOUT = 8.0

    def _connect(self) -> bool:
        """Connect the datagram socket, waiting for the helper to bind it."""
        deadline = time.monotonic() + self._CONNECT_TIMEOUT
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                self.error = f"native helper exited with status {self._process.returncode}"
                return False
            try:
                handle = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                handle.connect(str(SOCKET_PATH))
            except OSError:
                time.sleep(0.05)
                continue
            self._socket = handle
            self.error = None
            self._flags = -1
            return True
        self.error = f"native helper did not open {SOCKET_PATH}"
        return False

    @property
    def connected(self) -> bool:
        return self._socket is not None

    @property
    def alive(self) -> bool:
        """True when the helper process is still running, if we started it."""
        return self._process is None or self._process.poll() is None

    def stop(self) -> None:
        if self._socket is not None:
            self._send(SHUTDOWN)
            self._socket.close()
            self._socket = None
        if self._process is not None:
            try:
                self._process.wait(timeout=2.0)
            except subprocess.TimeoutExpired:
                self._process.terminate()
                try:
                    self._process.wait(timeout=1.0)
                except subprocess.TimeoutExpired:
                    self._process.kill()
            self._process = None

    def write_tuning(self) -> None:
        """Publish the native-side knobs as JSON.

        The TOML stays the single place anything is tuned; this is a projection of
        it, so the helper does not need a TOML parser to honour an edit.
        """
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        payload = {key: value for key, value in asdict(self._cfg).items() if key != "enabled"}
        payload["double_click_ms"] = self._double_click_ms
        TUNING_PATH.write_text(json.dumps(payload, indent=2))

    def apply_config(self, cfg: NativeConfig, double_click_ms: float | None = None) -> None:
        """Adopt edited settings and tell the helper to re-read them."""
        self._cfg = cfg
        if double_click_ms is not None:
            self._double_click_ms = double_click_ms
        self.write_tuning()
        self._send(RELOAD_CONFIG)

    # -------------------------------------------------------------------- sending

    def _send(self, intent: int, a: float = 0.0, b: float = 0.0, flags: int = 0, button: int = 0):
        handle = self._socket
        if handle is None:
            return
        self._sequence = (self._sequence + 1) & 0xFFFFFFFF
        frame = _FRAME.pack(
            _MAGIC, _VERSION, intent, self._sequence, flags, a, b, time.monotonic(), button, 0
        )
        try:
            handle.send(frame)
        except OSError as problem:
            # The helper has gone. Drop to the fallback path rather than raising
            # into the frame loop, and let reconnect() pick it up if it returns.
            self.error = f"native helper unreachable: {problem}"
            self._socket = None
            handle.close()

    def reconnect(self) -> bool:
        """Try to re-establish a dropped helper, with backoff. False if not yet."""
        if self.connected:
            return True
        now = time.monotonic()
        if now < self._next_attempt:
            return False
        self._attempts += 1
        self._next_attempt = now + min(0.5 * self._attempts, 10.0)
        binary = helper_path()
        if binary is None:
            return False
        if self._spawn and not self._launch(binary):
            return False
        if self._connect():
            self._attempts = 0
            return True
        return False

    # --------------------------------------------------------------------- intents

    def move_by(self, dx: float, dy: float) -> None:
        self._send(MOVE_BY, dx, dy)

    def warp_to_fraction(self, fx: float, fy: float) -> None:
        self._send(WARP_TO_FRACTION, fx, fy)

    def click(self, button: str = "left") -> None:
        self._send(CLICK, button=_BUTTONS.get(button, 0))

    def press(self, button: str = "left") -> None:
        self._send(PRESS, button=_BUTTONS.get(button, 0))

    def release(self, button: str = "left") -> None:
        self._send(RELEASE, button=_BUTTONS.get(button, 0))

    def scroll(self, dx: float, dy: float) -> None:
        self._send(SCROLL, dx, dy)

    def release_all(self) -> None:
        self._send(RELEASE_ALL)

    def set_mode(self, *, engaged: bool, pointing: bool, sweeping: bool) -> None:
        """Tell the helper what kind of gesture is running.

        Sent only on change. The helper needs this to know when to look for targets
        at all: a scroll or a swipe is not aiming at anything, and snapping during
        one would fight the hand.
        """
        flags = (ENGAGED if engaged else 0) | (POINTING if pointing else 0)
        flags |= SWEEPING if sweeping else 0
        if flags == self._flags:
            return
        self._flags = flags
        self._send(SET_MODE, flags=flags)
