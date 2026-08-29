# mctrl: Hand and Eye Control for macOS

Your gaze aims the cursor across the screen and your hand refines the last inch
and clicks, using the cameras you already own.

It lives in the menu bar. Touch the real mouse or keyboard and it yields
mid-motion, then comes back on its own a few seconds after you stop.

Everything it does is also a library, so another program can read what your
hands are doing or drive the cursor through the same path a pinch takes.

## Contents

- [Download](#download)
- [The Source Build](#the-source-build)
- [Permissions](#permissions)
- [The Interface](#the-interface)
- [The Gestures](#the-gestures)
- [The Library](#the-library)
- [Tuning](#tuning)
- [The Manual](#the-manual)
- [Bug Reports](#bug-reports)
- [Other Ways To Do This](#other-ways-to-do-this)

## Download

The package is on [PyPI](https://pypi.org/project/mctrl/) and wants macOS on
Apple silicon with Python 3.11 or 3.12:

```bash
pip install mctrl        # or: uv add mctrl
```

That gives you the `mindcontrol` command and the importable library. There is no
published `.dmg`; [the source build](#the-source-build) makes one.

The two model bundles download themselves into `~/.cache/mindcontrol/models/`
the first time you run.

## The Source Build

Cloning gets you the pinned dependency set in `uv.lock`, which is checked in
because MediaPipe's macOS wheels have disagreed with themselves about both Metal
and NumPy 2:

```bash
git clone https://github.com/GriffinCanCode/mctrl && cd mctrl
uv sync
```

Then build the native helper, which is what makes the cursor smooth and lets it
snap to what you are aiming at:

```bash
mindcontrol bridge
```

It needs the Xcode command line tools. Skip it and the app still runs, posting
events straight from Python: one cursor move per camera frame, and nothing snaps
or highlights.

For an icon in the Dock instead of a shell you have to keep open:

```bash
make app        # build/MindControl.app, from its own CPython up
make install    # copy it into /Applications
make dmg        # dist/MindControl-<version>.dmg
```

The bundle carries its own interpreter and every pinned wheel, so once installed
it reads nothing out of the checkout it was built from. Later, `make update`
reinstalls just this project into the existing bundle in about fifteen seconds,
and `make help` lists the rest.

## Permissions

Three grants are needed, all for whichever app launches the process — your
terminal from a shell, or `MindControl.app` from the bundle. `make permissions`
opens the panes.

- **Camera** — macOS asks the first time. If you miss the prompt, System
  Settings > Privacy & Security > Camera.
- **Accessibility** — required to move the cursor and to notice when you touch
  the real mouse. Without it the app tracks your hands perfectly and silently
  moves nothing, so it says so loudly at startup.
- **Menu Bar** — macOS 26 hosts every third-party status item through Control
  Center, so without System Settings > Menu Bar > Allow in the Menu Bar the
  process runs and there is nothing to click.

Running from a checkout, the native helper needs Accessibility in its own right,
because macOS attaches the permission to a binary rather than to a project.
Installed as a bundle it is covered by the app's own grant.

## The Interface

The menu-bar glyph is your state at a glance: `◉` engaged, `◐` suspended because
you touched hardware, `○` off.

The menu underneath carries a live status line — mode, hands seen, cameras,
frame rate, whether gaze is calibrated, and whether the cursor is `snapping` or
a `raw pointer` — above the controls:

- **Engage hands** — hand the cursor over, or take it back. The same thing the
  palm gesture does.
- **Show overlay** — the tuning window, drawing each hand's skeleton, its
  classified pose, its live pinch distance, `x2` when cameras are being merged,
  and a small screen proxy of where gaze thinks you are looking.
- **Calibrate gaze...** — nine dots appear; look at each until its ring closes.
  Escape cancels and leaves any existing calibration untouched.
- **Reload config** — apply edits to `config.toml` without restarting.
- **Open config folder** — `~/.config/mindcontrol/`, where your own copy of
  `config.toml` is put the first time the app runs.

Calibration learns your main display specifically, so recalibrate if you move
the camera or change seat. Until you calibrate, everything except gaze works and
the pointer is purely hand-driven.

A bundled app has no terminal to complain to, so everything it would have
printed goes to `~/.local/state/mindcontrol/app.log`.

To skip the menu bar and run in the foreground with the overlay inline, which is
the mode to use while tuning:

```bash
mindcontrol --debug
```

## The Gestures

The whole system rests on one distinction: a hand that is talking to the
computer looks different from a hand that is just there. Only these poses do
anything, so your hand can rest on the desk, hold a coffee, or gesture while you
talk.

- **Open palm, held still** — engage or disengage. The only gesture recognised
  while control is off, so you can always put your hands down safely.
- **Gaze** — aims. The cursor jumps to wherever your eyes settle, and stands
  down whenever the hand is moving, so the two never fight.
- **Ready pose**, index finger and thumb out with the hand in a loose C — turns
  your hand into a trackpad floating in the air, moving the cursor relative to
  where it already is.
- **Pinch thumb to index, quick tap** — left click. Two taps make a real chained
  double click, which Finder treats exactly like a trackpad one.
- **Pinch thumb to middle finger** — right click.
- **Pinch and hold, then move** — grab and drag. Release to drop.
- **Fist, then move** — scroll and pan, as if you had grabbed the page. Open
  your hand to let go.

Those need no setup in any application: the native helper aims them at whatever
the accessibility API reports under the cursor — a button, a link, a word — so
they work in an app that has never heard of this one.

The four below are the ones with no single right answer, since a sweep left is
"previous desktop" on the desktop and "back" in a browser. They ship as system
controls and can be rebound per application:

- **Open palm, swipe left or right** — switch desktops.
- **Open palm, push up** — Mission Control.
- **Open palm, push down** — nothing, until you bind it. Recognised and reported,
  but inert by default, because lowering an open palm is also how a hand comes to
  rest.
- **Thumb and pinky out, held** — toggle dictation, mirroring the key you
  assigned under System Settings > Keyboard > Dictation.

```bash
mindcontrol bind                                # what everything does right now
mindcontrol bind swipe_left cmd+[ --app Safari  # ...and in Safari, go back
mindcontrol bind palm_push_up none --app Keynote
```

An action is a key chord written the way a menu writes one, or a name from
`[keys]`. An app scope matches the bundle id, the app's name, or the tail of the
id, so `Safari` and `com.apple.Safari` mean the same thing; `none` mutes a global
binding in one app without clearing it everywhere. `config.toml` holds these
under `[bindings.Safari]` sub-tables when you want them to survive a restart, and
[the manual](docs/manual.md#binding-gestures-to-an-application) covers the rest.

An open palm that moves is a swipe and an open palm that sits still is the
engage toggle, so those two can never be confused.

There is no air keyboard. Typing goes through dictation.

## The Library

Everything the app does is reachable from another program: what your hands are
doing, what the engine decided it meant, which mode control is in, and the
cursor itself.

Attach to a MindControl that is already running, or run the whole pipeline
inside your own process:

```python
from mindcontrol.api import MindControl

with MindControl.connect() as mc:        # the running menu-bar app
    mc.modes.engage()
    for intent in mc.tracking.gestures(timeout=10.0):
        print(intent.action, intent.dx, intent.dy)

with MindControl.launch() as mc:         # no app, no menu bar: just the tracker
    print(mc.status().mode)
```

`launch` is for a kiosk, a test harness, or an application embedding the
tracker. It owns the cameras, the models and the native helper, so only one may
exist at a time on one machine.

Every verb behaves identically either way, which is the point of both sides
dispatching from one contract in `api/contract.py`.

The modules are `status` (`get`), `modes` (`get`, `set`, `toggle`), `tracking`
(`subscribe`, `unsubscribe`), `input` (`move_by`, `move_to`, `click`, `press`,
`release`, `scroll`, `key`), `bindings` (`get`, `set`, `clear`) and `system`
(`calibrate`, `reload_config`, `pause`, `resume`, `describe`).

The streams are `status`, one snapshot per processed frame; `hands`, fused hands
with pose and measurements; `gaze`, the filtered point as a fraction of the
calibrated display; and `gestures`, intents as the engine emits them. Landmarks
are opt-in, since the 21 points per hand dwarf everything else on the wire.

Nothing you send touches the cursor directly. An `input` verb is queued and
executed between frames, through the same `Mouse` a pinch goes through, so your
warp is smoothed and snapped exactly as a gesture would be.

`bindings` is how a program teaches this one its own gestures, and `input.key`
with `status.app` is how it handles them itself instead — read a swipe, see what
is in front, and send the chord:

```python
with MindControl.connect() as mc:
    mc.bindings.set("swipe_left", "cmd+[", app="Safari")
    print(mc.bindings.resolved())        # what each gesture does right now
```

Importing the package is cheap: neither MediaPipe nor Quartz is loaded until
something asks to run a pipeline in this process.

From another language the same surface is a Unix socket at
`~/.local/state/mindcontrol/api.sock`, mode 0600, one JSON object per line, with
no network port and no dependency to install:

```
$ nc -U ~/.local/state/mindcontrol/api.sock
{"hello":{"protocol":1,"app":"mindcontrol"}}
{"verb":"tracking.subscribe","params":{"streams":["gestures"]},"id":1}
{"ok":true,"id":1,"result":{"streams":["gestures"],"landmarks":false,"interval_ms":0.0}}
{"stream":"gestures","data":{"action":"click","dx":0.0,"dy":0.0,"button":"right"}}
```

It is a command too, and it describes itself:

```bash
mindcontrol api                              # the whole catalogue, no app needed
mindcontrol api modes.set mode=active
mindcontrol api --watch gestures,hands --seconds 5
```

Turn the whole thing off with `enabled = false` under `[api]` in `config.toml`.

## Tuning

The shipped thresholds are reasoned, not measured — chosen against hand
proportions in the literature, and nobody's hands are the literature. Measure
your own once instead:

```bash
mindcontrol record       # ~1 minute: hold each pose when prompted
mindcontrol autotune     # see what your hands imply, change nothing
mindcontrol replay       # run the recording back through the engine
mindcontrol autotune --apply
```

Use one hand and keep the other out of frame, because a second hand resting in
shot is a different pose wearing the same label.

`autotune` puts each threshold in the gap between your own clusters, and
declines when two of them overlap rather than emitting a number indistinguishable
from noise. Use `replay` before `--apply`, not after: it reads recorded
timestamps rather than the clock, so it is the only way to tell whether a change
helped.

Every threshold lives in `config.toml` in palm units, so they hold whether you
are at the keyboard or across the room. [The manual](docs/manual.md#tuning-by-hand)
lists them by symptom.

## The Manual

Multiple cameras, the tuning knobs one by one, what snapping actually does, why
the cursor moved to Swift, the test layers and the measured limits are all in
[docs/manual.md](docs/manual.md).

## Bug Reports

Bugs and security reports both belong in
[the issue tracker](https://github.com/GriffinCanCode/mctrl/issues).

When the cursor snaps to the wrong thing, include what the helper says it can
see, which prints every candidate with its role, distance and size and marks the
one it would choose:

```bash
native/.build/release/mindcontrol-bridge --inspect
```

It takes no single-instance claim, so it works while the real helper is running.

## Other Ways To Do This

This is macOS on Apple silicon, driven by ordinary cameras, and none of it
ports.

- **On another platform, or without a camera** — [Talon Voice](https://talonvoice.com)
  does the same job with speech and mouth noises, and runs everywhere.
- **Wanting pixel-accurate gaze** — a webcam is good for regions and not for
  pixels, which is exactly why the hand does the last inch here. Dedicated
  hardware such as a [Tobii](https://gaming.tobii.com) tracker is the answer.
- **Wanting something built in** — System Settings > Accessibility > Pointer
  Control has a Head Pointer, and Voice Control types.
