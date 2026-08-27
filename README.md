# mindcontrol

Control your Mac with your hands and eyes, using the cameras you already own.

It runs as a menu-bar background app. Your gaze aims the cursor across the
screen, your hand refines the last inch and clicks, and the moment you touch the
real mouse or keyboard the hands get out of the way. Stand up, walk around, sit
back down — control follows whichever camera can see you.

## The gesture vocabulary

The whole system is built on one distinction: **a hand that is talking to the
computer looks different from a hand that is just there.** Only the poses below
do anything. Your hand can rest on the desk, hold a coffee, or gesture while you
talk, and nothing happens.

### Getting in and out

**Open palm, held still for 1 second** — engage or disengage hand control. This
is the only gesture recognised while control is off, so you can always put your
hands down safely.

**Touch the mouse, trackpad or keyboard** — control suspends instantly,
mid-motion. It comes back on its own about three seconds after you stop, or
immediately if you palm-toggle. You never "exit" hand mode; you just reach for
the trackpad and it yields.

### Pointing

**Gaze** aims. Look somewhere and the cursor jumps to that region once your eyes
settle there for ~150ms. Eyes are excellent at crossing a screen and poor at
holding still, which is why they only ever do the coarse half.

**Ready pose** — index finger and thumb both out, hand relaxed in a loose C —
turns your hand into a trackpad floating in the air. Move it and the cursor
moves relative to where it already is, so small wrist motions do fine work.
Pointer acceleration means slow movement is precise and fast movement is
sweeping.

Gaze politely stands down whenever the hand is moving, dragging or scrolling, so
the two never fight over the cursor.

### Acting

**Pinch thumb to index, quick tap** — left click.

**Two quick pinch taps** — double click. Sent as a real chained click, so Finder
and everything else treat it exactly like a trackpad double click.

**Pinch thumb to middle finger** — right click.

**Pinch and hold, then move** — grab and drag. Hold the pinch past a quarter
second and you are now holding the thing under the cursor; release to drop it.
This is the main way to manipulate anything directly.

**Close your hand into a fist and move** — scroll and pan, as if you had grabbed
the page and were pulling it. Open your hand to let go.

### System control

**Open palm, swipe left or right** — switch desktops.

**Open palm, push up** — Mission Control.

**Thumb and pinky out ("telephone hand"), held** — toggle dictation, for typing
by voice. macOS has no shortcut for this that can be synthesised reliably, so
assign a real key in System Settings > Keyboard > Dictation and mirror it under
`[keys]` in `config.toml` (default `f5`).

An open palm that *moves* is a swipe and an open palm that *sits still* is the
engage toggle, so those two can never be confused. Every held gesture fires once
and then waits for you to change pose, so holding a palm for three seconds
toggles control exactly once.

## Install

Requires macOS on Apple silicon and Python 3.11 or 3.12.

```bash
uv sync            # installs the exact versions in uv.lock
```

MediaPipe is pinned below 1.0 deliberately: the 1.x macOS arm64 wheels abort
inside `TensorsToDetectionsCalculator` asking for a Metal service the wheel does
not ship. `uv.lock` is checked in because that is not the only sharp edge in this
dependency set — the 0.10 line has also disagreed with itself about NumPy 2 — so
the combination known to work is recorded rather than re-resolved.

The two model bundles (~11 MB) download themselves into
`~/.cache/mindcontrol/models/` the first time you run.

## Permissions

Two grants are needed, both for whichever app launches the process (your
terminal, if you start it from a shell):

**Camera** — macOS asks the first time. If you miss the prompt, System Settings
> Privacy & Security > Camera.

**Accessibility** — required to move the cursor and to notice when you touch the
real mouse. System Settings > Privacy & Security > Accessibility. Without it the
app runs and tracks your hands perfectly while silently failing to move
anything, so it says so loudly at startup.

## Running

```bash
mindcontrol                      # menu-bar background app
mindcontrol --debug              # foreground, with the tuning overlay
mindcontrol --debug --no-overlay # foreground, status line only
mindcontrol calibrate            # nine-point gaze calibration
mindcontrol cameras              # list capture devices
mindcontrol record               # capture a labelled gesture session
mindcontrol autotune             # fit thresholds to that session
mindcontrol replay               # run a session back through the engine
```

The menu-bar glyph is your state at a glance: `◉` engaged, `◐` suspended because
you touched hardware, `○` off. The menu carries a live status line (mode, hands
seen, cameras, frame rate, whether gaze is calibrated), an engage toggle, the
overlay toggle, and calibration.

MediaPipe announces its GL version, its XNNPACK delegate and two feedback
managers on every start, which buries the output you actually want to read during
a guided recording. Those lines are suppressed while the models load:

```bash
MINDCONTROL_VERBOSE=1 mindcontrol record   # put them back
```

Suppression only covers model construction, and only when it succeeds. If a model
fails to load, everything it logged on the way down is printed, since that is the
one time those lines are worth having.

### Calibrating gaze

Run `mindcontrol calibrate`, or pick "Calibrate gaze..." from the menu. Nine
dots appear; look at each until its ring closes. Escape cancels and leaves any
existing calibration untouched.

Calibration learns your main display specifically — the one the camera watched
you look at. Hand movement can roam across every monitor; gaze warps land on
that one screen. Recalibrate if you move the camera or change seat.

Until you calibrate, everything except gaze works, and the pointer is purely
hand-driven.

## Using more than one camera

Find out what you have:

```bash
mindcontrol cameras            # names, resolutions, and which indices work
mindcontrol cameras --preview  # a frame from each, to see which is which
```

Names come from AVFoundation, whose ordering has been seen to disagree with
OpenCV's indices, and there is no shared identifier to reconcile them. `--preview`
is the only way to be certain, and being certain matters: `primary_gaze` pointed
at the wrong camera means gaze estimated from a view of the wall.

Then say which cameras to use and which one watches your eyes:

```toml
[cameras]
devices = [0, 1]
primary_gaze = 0
```

Nothing else changes. Each camera gets its own thread and its own inference, and
the results are merged by how confident each camera is. The payoff is occlusion:
a pinch hidden behind your palm from the laptop is obvious from a camera at your
side, and either one can carry the gesture.

Hand *shape* is combined across cameras by confidence-weighted vote. Hand
*position* is not averaged — each camera sees you from a different place, so an
average would describe a hand that exists nowhere. One camera leads for position,
and when the lead changes the new leader's coordinates are shifted onto where the
pointer already was, so the handover costs neither a jump nor the motion in that
frame. Losing the motion mattered: a swipe is judged on accumulated travel, and a
handover mid-sweep used to erase it.

Gaze runs on the primary camera only. It is the expensive model, and only a
camera near the screen you are looking at can say anything useful.

More cameras is not automatically better, and the reason is the vote. A camera
that sees the hand nearly edge-on still gets a say in the blended shape, and its
say is wrong in a particular direction: fingers foreshorten, so an open palm can
read as something closed. On one recorded session a phone added as a third view
was worth two extra clicks and cost five swipes — 7 down to 2 — because the palm
that has to stay open through a sweep kept flickering. Judge a camera by replaying
a session with and without it, not by how good its own picture looks.

Three cameras measured at 19ms median per poll on an M4 Max, against a 33ms
budget at 30fps, so time is not what limits how many you add. Two things to know
if a camera might come and go — an iPhone over Continuity does:

List it **last**. Indices are positional, so a device that disappears from the
middle renumbers everything after it, and a config naming cameras by number would
quietly start pointing at different lenses. Lose the trailing index and the rest
keep their meaning.

Expect a wait on start. Cameras are given up to twelve seconds to deliver their
first frame before recording begins, because a Continuity camera has been
measured taking most of five, and frames captured while one is still waking are
indistinguishable afterwards from a camera that saw nothing. Any camera that
never wakes is named, and the session goes ahead without it.

## Tuning

The shipped thresholds are reasoned, not measured. They were chosen against hand
proportions in the literature, and nobody's hands are the literature. Rather than
nudging numbers until things feel right, you can measure yours once:

```bash
mindcontrol record     # ~1 minute: hold each pose when prompted
mindcontrol autotune   # see what your hands imply, change nothing
mindcontrol autotune --apply
```

`record` walks you through the poses and stores the raw landmarks together with
the pose it asked for. That pairing is what makes it useful: the label says which
cluster each sample belongs to, so a boundary can be *found* instead of guessed.

**Use one hand and keep the other out of frame.** A second hand resting in shot
is not idle data — it is a different pose wearing the same label, and it corrupts
every threshold fitted from that prompt. Both `autotune` and `replay` check for
this and tell you when a recording cannot answer the question you asked of it.

`autotune` reads those clusters and puts each threshold in the gap between them —
your pinched distances on one side, your open ones on the other. Percentiles
rather than extremes, so a single bad frame cannot move a threshold. **If two
clusters overlap it declines and tells you**, because a tuner that always emits a
number is indistinguishable from one that emits noise. Without `--apply` it only
reports. With it, `config.toml` is edited in place, comments intact.

`replay` runs a recording back through the gesture engine offline:

```bash
mindcontrol replay
```

It prints what each prompt actually classified as, and what the state machine did
about it — how many clicks your taps produced, whether a held pinch became one
drag or a burst of clicks, whether anything fired while control was off. Because
it reads recorded timestamps rather than the clock, every run is identical, so
this is also how you tell whether a config change helped or hurt.

Use it before `--apply`, not after. A fit can be sound and still be a regression,
because several of these thresholds trade one pose against another: raising
`thumb_extended` until a fist is recognised also makes it harder for a thumb to
count as *out*, which is what the telephone pose needs. Replay each candidate,
then write only the ones that earned it:

```bash
mindcontrol autotune --apply --only thumb_extended,pinch_close
```

When one gesture needs another attempt and the rest of the script already worked,
re-record just that part:

```bash
mindcontrol record --focus pinch   # ~27s
mindcontrol record --focus swipe   # ~16s
mindcontrol record --focus poses
```

Each group carries the prompts its fit *depends on*, not only the failing one. A
threshold is a boundary between two clusters, so recording just the pinch would
leave nothing to separate it from — `--focus pinch` therefore also captures the
open hands that form the other side of that boundary.

Recordings live in `~/.local/state/mindcontrol/sessions/`. They are worth keeping:
they turn "it feels wrong" into a number, and they let the test suite check real
gestures long after the moment you performed them.

### Tuning by hand

Open the overlay (`mindcontrol --debug`) and watch the real numbers from your own
hands in your own light. It shows each hand's skeleton, its classified pose, its
live pinch distance, `x2` when cameras are being merged, and a small screen proxy
showing where gaze thinks you are looking.

Every threshold lives in `config.toml`, and "Reload config" applies edits without
restarting. Distances are in **palm units** — divided by the span from your wrist
to your middle knuckle — so they hold whether you are at the keyboard or across
the room.

The knobs worth reaching for first:

- **Cursor too twitchy** — lower `pointer.filter_fc_min`. **Too laggy** — raise it.
- **Cursor too slow to cross the screen** — raise `pointer.sensitivity` or
  `pointer.gain_max`.
- **Clicks not registering** — raise `gestures.pinch_close` toward your measured
  pinch distance. **Clicks firing on their own** — lower it.
- **Clicks sticking down** — lower `gestures.pinch_open`. Watch your resting
  pinch number on the overlay; it must sit clearly above this value.
- **Taps turning into drags** — raise `gestures.tap_max_ms`.
- **Open palms triggering when your hand is sideways** — raise
  `gestures.palm_facing` toward 0.3. It defaults to 0.0, accepting any
  orientation, because a too-strict setting here would stop you engaging at all.
- **Accidental swipes** — raise `gestures.swipe_min_speed` or
  `swipe_min_travel`.
- **Swipes never firing, though the palm is recognised when you hold it still** —
  raise `gestures.swipe_grace_ms`. A sweeping palm is blurred and rotating, so the
  pose drops out partway through; without a grace window the accumulated travel is
  wiped mid-gesture and no swipe ever completes. 600 ms is enough for most hands.
  Lower it if a fist just after a swipe fails to scroll.
- **Adding a camera made swipes *worse*** — check the pose report from
  `mindcontrol replay` for the prompt that is failing. Handing the lead between
  cameras is free (the new leader's own movement is stitched onto the track, so
  `rebases` should stay near zero), but a camera with a poor view still pulls the
  *shape* average around, and a swipe needs the open palm to survive the sweep.
  A camera that sees your hand edge-on helps a still pinch and hurts a sweep.
- **Pinches doing nothing, or scrolling instead of clicking** — your pinch is
  probably curling the other three fingers, which makes it a fist, and a fist
  scrolls by design. Keep the middle, ring and little fingers out. No threshold
  can separate the two: on a curled pinch they measure identically.
- **Gaze fighting your hand** — lower `gaze.hand_quiet_speed`. **Gaze warping on
  small corrections** — raise `gaze.warp_min_distance`.

Set `pointer.mode` to `hands` to switch gaze off entirely, or `gaze` to lean on
it harder.

## How it fits together

```
cameras (one thread each)
  -> hand landmarks per camera        tracking/hands.py
  -> gaze features on the primary     tracking/gaze.py
  -> merge cameras into one view      fusion.py
  -> classify shape, run the machine  geometry.py, gestures/engine.py
  -> post real input events           control/mouse.py, control/keyboard.py
```

`pipeline.py` runs that loop on a worker thread. `app.py` keeps the main thread
for the menu bar, because a macOS status item needs the Cocoa run loop.
`control/modes.py` arbitrates between your hands and your hardware, watching for
physical input on a private run loop of its own.

Every event the app injects is tagged, and the watcher ignores anything carrying
that tag. Without it the app would see its own cursor motion, conclude a human
had grabbed the mouse, and suspend itself the instant it started working.

## Tests

```bash
uv sync --group dev
uv run pytest
```

Three layers, deliberately separated:

- **Logic** — the state machine and the geometry, driven by stated measurements
  and by synthetic hands built to human proportions. Fast, and independent of
  cameras, lighting, and hands.
- **Machinery** — a scripted session replayed end to end, covering session I/O,
  re-measurement, and every gesture the engine can emit. Deterministic, so it
  runs anywhere.
- **Reality** — the same assertions against your own recording. These *skip*
  until you have run `mindcontrol record`, rather than invent input, because a
  test that fabricates its own data would report success while checking nothing
  about the person using this.

When a reality test fails but the matching machinery test passes, the pipeline is
fine and the thresholds do not suit those hands: run `mindcontrol autotune`.

## Limits worth knowing

Gaze from a webcam is good for regions, not for pixels; expect a few percent of
screen error, which is exactly why the hand does the last inch.

There is no air keyboard. Typing goes through dictation.

Gaze is calibrated for one display and one seating position.

Hand tracking needs your hand reasonably lit and reasonably unoccluded. A second
camera helps more than better thresholds.
