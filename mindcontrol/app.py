"""The background app.

rumps owns the main thread because a macOS status-bar item needs the Cocoa run
loop, so the pipeline runs on a worker thread and the two meet only through the
status snapshot and a handful of menu callbacks.

``--debug`` skips the menu bar entirely and runs the pipeline on the main thread
with the overlay window inline, which is the mode to use while tuning thresholds.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import config as config_module
from .config import Config, load
from .control.modes import Mode
from .debug_view import DebugView, render
from .pipeline import Pipeline, PipelineStatus

# Status-bar glyphs. Text rather than an icon file keeps the app one directory.
GLYPHS = {Mode.ACTIVE: "\u25c9", Mode.SUSPENDED: "\u25d0", Mode.OFF: "\u25cb"}

# Names the menu bar slot so macOS remembers it across launches. Unnamed items
# get a fresh leftmost slot every time, and on a notched display with a full
# menu bar that slot is *under the notch*: hosted, on level 25, and invisible.
STATUS_ITEM_NAME = "MindControl"


def camera_state(prompt: bool = True) -> str:
    """Camera authorisation, as one of ``ok``, ``waiting`` or ``refused``.

    OpenCV's AVFoundation backend asks for access itself and then fails the same
    frame if the answer is not already yes, so nothing may open a camera until
    this says ``ok``. The dialog is answered by a human, which takes many frames,
    hence ``waiting`` rather than a second attempt.
    """
    try:
        from AVFoundation import (
            AVAuthorizationStatusAuthorized,
            AVAuthorizationStatusNotDetermined,
            AVCaptureDevice,
            AVMediaTypeVideo,
        )
    except ImportError:
        return "ok"
    status = AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeVideo)
    if status == AVAuthorizationStatusAuthorized:
        return "ok"
    if status != AVAuthorizationStatusNotDetermined:
        return "refused"
    if prompt:
        AVCaptureDevice.requestAccessForMediaType_completionHandler_(
            AVMediaTypeVideo, lambda _granted: None
        )
    return "waiting"


def check_accessibility(prompt: bool = True) -> bool:
    """Report whether we may post input events, prompting once if not.

    Without this permission the app runs, sees your hands, and silently fails to
    move anything, so it is worth saying so loudly at startup.
    """
    try:
        from ApplicationServices import (
            AXIsProcessTrusted,
            AXIsProcessTrustedWithOptions,
            kAXTrustedCheckOptionPrompt,
        )
    except ImportError:
        return True
    if AXIsProcessTrusted():
        return True
    if prompt:
        AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: True})
    return False


class MindControlApp:
    """Status-bar front end for the pipeline."""

    def __init__(self, cfg: Config) -> None:
        import rumps

        self._rumps = rumps
        self.cfg = cfg
        self.pipeline = Pipeline(cfg)
        self.view = DebugView()
        self._calibrating = False

        self.app = rumps.App("MindControl", title=GLYPHS[Mode.OFF], quit_button=None)
        self._status_item = rumps.MenuItem("Starting...")
        self._engage_item = rumps.MenuItem("Engage hands", callback=self._on_engage)
        self._overlay_item = rumps.MenuItem("Show overlay", callback=self._on_overlay)
        self._gaze_item = rumps.MenuItem("Calibrate gaze...", callback=self._on_calibrate)
        self.app.menu = [
            self._status_item,
            None,
            self._engage_item,
            self._overlay_item,
            None,
            self._gaze_item,
            rumps.MenuItem("Reload config", callback=self._on_reload),
            rumps.MenuItem("Open config folder", callback=self._on_open_config),
            None,
            rumps.MenuItem("Quit", callback=self._on_quit),
        ]
        self._timer = rumps.Timer(self._on_tick, 0.5)
        self._booted = False
        self._camera_prompted = False

    # ----------------------------------------------------------------- lifecycle

    def run(self) -> None:
        # rumps creates the status item, then the run loop. Cameras, models and
        # the Accessibility prompt wait until the first tick so Control Center
        # has a hosted glyph before any modal appears in front of nothing.
        self._rumps.events.before_start.register(self._pin_status_item)
        self._timer.start()
        self.app.run()

    def _pin_status_item(self) -> None:
        """Name the menu bar slot, and seed it on the right the first time.

        rumps creates the item unnamed, so this runs from ``before_start``, which
        is the first moment it exists. Naming it there still moves it. The
        position is only seeded when nothing is stored, so a later Cmd-drag is
        remembered instead of being overwritten on every launch.
        """
        from Foundation import NSUserDefaults

        defaults = NSUserDefaults.standardUserDefaults()
        key = f"NSStatusItem Preferred Position {STATUS_ITEM_NAME}"
        if defaults.objectForKey_(key) is None:
            # Measured from the right edge, so zero is beside Control Center --
            # the one place a new item cannot land behind the notch.
            defaults.setObject_forKey_(0, key)
        item = getattr(self.app._nsapp, "nsstatusitem", None)
        if item is not None:
            item.setAutosaveName_(STATUS_ITEM_NAME)

    def _boot(self) -> bool:
        camera = camera_state(prompt=not self._camera_prompted)
        self._camera_prompted = True
        if camera == "waiting":
            self._status_item.title = "Waiting for camera permission..."
            return False
        if camera == "refused":
            print(
                "[app] Camera access was refused, so there is nothing to track.\n"
                "      System Settings > Privacy & Security > Camera, then start it again."
            )
        if not check_accessibility():
            print(
                "[app] Accessibility permission is required to move the cursor.\n"
                "      System Settings > Privacy & Security > Accessibility, then "
                "enable the app running this process and start it again."
            )
        self.pipeline.frame_hook = self._on_frame
        self.pipeline.start()
        if self.cfg.debug.overlay:
            self.view.open()
        return True

    def _shutdown(self) -> None:
        self._timer.stop()
        self.pipeline.stop()
        self.view.close()

    # ------------------------------------------------------------------ callbacks

    def _on_frame(self, frame, hands, status: PipelineStatus) -> None:
        """Called on the pipeline thread for every processed frame."""
        if self.view.running:
            self.view.push(render(frame, hands, status, status.gaze_point))

    def _on_tick(self, _timer) -> None:
        if not self._booted:
            if not self._boot():
                return
            self._booted = True
        status = self.pipeline.status
        mode = self.pipeline.modes.mode
        self.app.title = GLYPHS[mode]
        self._engage_item.title = "Disengage hands" if mode is Mode.ACTIVE else "Engage hands"
        self._overlay_item.title = "Hide overlay" if self.view.running else "Show overlay"
        self._status_item.title = self._summary(status)

    def _summary(self, status: PipelineStatus) -> str:
        if self._calibrating:
            return "Calibrating gaze..."
        if status.problems:
            return status.problems[0][:60]
        cameras = ",".join(str(c) for c in status.cameras) or "none"
        gaze = "gaze on" if status.gaze_ready else "gaze uncalibrated"
        # Worth showing: without the helper the cursor still works but is neither
        # smoothed nor snapped, and that is exactly the complaint it exists to fix.
        pointer = "snapping" if status.native else "raw pointer"
        return (
            f"{self.pipeline.modes.describe()} - {status.hands} hand(s) - "
            f"cam {cameras} - {status.fps:.0f} fps - {gaze} - {pointer}"
        )

    def _on_engage(self, _sender) -> None:
        self.pipeline.modes.toggle()
        if self.pipeline.modes.mode is Mode.ACTIVE:
            self.pipeline.engine.rebase()

    def _on_overlay(self, _sender) -> None:
        if self.view.running:
            self.view.close()
        else:
            self.view.open()

    def _on_calibrate(self, _sender) -> None:
        if self._calibrating:
            return
        self._calibrating = True
        # Calibration needs the camera and a fullscreen window of its own, so the
        # pipeline lets go for the duration and reloads the result afterwards.
        threading.Thread(target=self._calibrate, name="calibrate", daemon=True).start()

    def _calibrate(self) -> None:
        previous = self.pipeline.modes.mode
        self.pipeline.modes.set_mode(Mode.OFF)
        self.pipeline.pause()
        try:
            result = subprocess.run(
                [sys.executable, "-m", "mindcontrol.calibrate"],
                cwd=Path.cwd(),
                check=False,
            )
            if result.returncode != 0:
                print(f"[app] calibration exited with {result.returncode}")
        except OSError as exc:
            print(f"[app] could not start calibration: {exc}")
        finally:
            self.pipeline.resume()
            self.pipeline.modes.set_mode(previous)
            self._calibrating = False

    def _on_reload(self, _sender) -> None:
        self.cfg = load()
        self.pipeline.apply_config(self.cfg)
        print(f"[app] reloaded config from {self.cfg.source_path}")

    def _on_open_config(self, _sender) -> None:
        target = self.cfg.source_path or config_module.APP_DIR
        subprocess.run(["open", str(Path(target).parent if target.is_file() else target)])

    def _on_quit(self, _sender) -> None:
        self._shutdown()
        self._rumps.quit_application()


def run_headless(cfg: Config, overlay: bool) -> int:
    """Run without the menu bar, drawing the overlay on the main thread."""
    import cv2

    pipeline = Pipeline(cfg)
    latest: dict[str, object] = {}

    def hook(frame, hands, status) -> None:
        if overlay:
            latest["image"] = render(frame, hands, status, status.gaze_point)

    pipeline.frame_hook = hook
    pipeline.start()
    keys = "Ctrl-C stops" + (", Esc closes the window" if overlay else "")
    print(f"[app] running headless; {keys}", flush=True)
    try:
        while True:
            image = latest.pop("image", None)
            if overlay and image is not None:
                cv2.imshow("mindcontrol", image)
            if overlay and cv2.waitKey(10) & 0xFF == 27:
                break
            if not overlay:
                time.sleep(0.2)
                status = pipeline.status
                print(
                    f"\r{status.mode:<22} {status.gesture:<34} "
                    f"{status.hands} hand(s)  {status.fps:5.1f} fps",
                    end="",
                    flush=True,
                )
    except KeyboardInterrupt:
        pass
    finally:
        pipeline.stop()
        if overlay:
            cv2.destroyAllWindows()
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mindcontrol",
        description="Control your Mac with your hands and eyes.",
    )
    parser.add_argument("--config", type=Path, help="path to config.toml")
    parser.add_argument(
        "--debug",
        action="store_true",
        help="run in the foreground with the tuning overlay instead of the menu bar",
    )
    parser.add_argument("--no-overlay", action="store_true", help="with --debug, skip the window")
    parser.add_argument("--engaged", action="store_true", help="start with hands already engaged")

    subcommands = parser.add_subparsers(dest="command")
    subcommands.add_parser("run", help="run the menu-bar app (the default)")
    subcommands.add_parser("calibrate", help="nine-point gaze calibration")

    cameras = subcommands.add_parser("cameras", help="list capture devices")
    cameras.add_argument("--max-index", type=int, default=8, help="highest index to probe")
    cameras.add_argument(
        "--preview", action="store_true", help="show a frame from each, to confirm which is which"
    )

    recorder = subcommands.add_parser("record", help="capture a labelled gesture session")
    recorder.add_argument("--out", type=Path, help="where to write the session")
    recorder.add_argument("--note", default="", help="note stored in the session header")
    recorder.add_argument(
        "--focus",
        help="record only one part of the script, e.g. pinch, swipe, poses (comma-separated)",
    )

    tuner = subcommands.add_parser("autotune", help="fit thresholds to a recorded session")
    tuner.add_argument("session", nargs="?", type=Path, help="session file (default: newest)")
    tuner.add_argument(
        "--apply", action="store_true", help="write the proposals into config.toml"
    )
    tuner.add_argument(
        "--only",
        help="comma-separated thresholds to write, e.g. thumb_extended,pinch_close",
    )

    player = subcommands.add_parser("replay", help="run a recorded session through the engine")
    player.add_argument("session", nargs="?", type=Path, help="session file (default: newest)")

    helper = subcommands.add_parser(
        "bridge", help="build the native helper that smooths and snaps the cursor"
    )
    helper.add_argument("--rebuild", action="store_true", help="rebuild even if a binary exists")
    # Not --debug: that is already a top-level flag, and argparse would let the
    # subcommand silently shadow it.
    helper.add_argument("--dev", action="store_true", help="build unoptimised, for helper work")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)

    config_module.ensure_dirs()
    cfg = load(args.config)
    if args.engaged:
        cfg.modes.start_engaged = True

    command = args.command or "run"

    if command == "cameras":
        from . import devices

        return devices.run(args.max_index, show=args.preview)

    if command == "calibrate":
        from . import calibrate

        return calibrate.run(cfg)

    if command == "bridge":
        from .control import bridge

        return bridge.run(rebuild=args.rebuild, debug=args.dev)

    if command == "record":
        from . import record

        focus = tuple(f.strip() for f in args.focus.split(",") if f.strip()) if args.focus else None
        return record.run(cfg, out=args.out, note=args.note, focus=focus)

    if command in {"autotune", "replay"}:
        from . import session

        try:
            chosen = session.resolve(args.session)
        except FileNotFoundError as missing:
            # An expected situation, not a crash: nobody has recorded yet.
            print(f"[{command}] {missing}")
            return 2

        if command == "autotune":
            from . import autotune

            only = {k.strip() for k in args.only.split(",") if k.strip()} if args.only else None
            return autotune.run(
                chosen, cfg.gestures, cfg.source_path, args.apply, only, cfg.tracking
            )

        from . import replay

        return replay.run(session.Session.load(chosen), cfg)

    if args.debug:
        if not check_accessibility():
            print(
                "[app] Accessibility permission is required to move the cursor.\n"
                "      System Settings > Privacy & Security > Accessibility, then "
                "enable the app running this process and start it again."
            )
        return run_headless(cfg, overlay=not args.no_overlay)

    try:
        from Foundation import NSBundle

        ident = NSBundle.mainBundle().bundleIdentifier()
    except Exception:
        ident = None
    print(f"[app] bundle {ident or 'none'}", flush=True)

    MindControlApp(cfg).run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
