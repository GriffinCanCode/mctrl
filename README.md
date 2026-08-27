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

Then build the native helper, which is what makes the cursor smooth and lets it
snap to what you are aiming at:

```bash
mindcontrol bridge      # compiles native/ and reports where it stands
```

Needs the Xcode command line tools (`xcode-select --install`). If you skip this
the app still runs — it posts events straight from Python instead — but you get
one cursor move per camera frame and nothing snaps or highlights. The menu-bar
status line ends in `snapping` or `raw pointer` so you can tell which you have.

### As a Mac app

To get an icon you can keep in the Dock instead of a shell you have to keep open:

```bash
make app        # build/MindControl.app, from its own CPython up
make install    # copy it into /Applications
make dmg        # dist/MindControl-<version>.dmg, to install it somewhere else
```

The first build takes a few minutes because it fetches a whole interpreter and
every pinned wheel. Afterwards use `make update`, which reinstalls just this
project and the helper into the existing bundle, syncs the handful of changed
files into `/Applications` and relaunches — about fifteen seconds. `make help`
lists the rest.

The bundle carries its own CPython and the whole pinned dependency set, so once
it is installed it reads nothing out of the checkout it was built from. The
native helper is built into it and pointed at through `MINDCONTROL_BRIDGE`, and
`config.toml` is copied into `~/.config/mindcontrol/` the first time it runs,
after which that copy is yours to edit.

It is a status-bar app, so opening it puts the glyph in the menu bar and nothing
in the Dock. There is no terminal for it to complain to either, so everything it
would have printed goes to `~/.local/state/mindcontrol/app.log`.

The icon is drawn rather than drawn on: `packaging/icon.py` renders it with
CoreGraphics at build time. It is deliberately full-bleed, because macOS 26
rounds, masks and shadows an app icon itself and reads artwork that arrives with
its own corners as a picture to inset into a plate. Pass `--plate` for the older
look.

## Permissions

Three grants are needed, all for whichever app launches the process — your
terminal, if you start it from a shell, and `MindControl.app` itself if you
installed the bundle, which is the tidier of the two:

**Camera** — macOS asks the first time. If you miss the prompt, System Settings
> Privacy & Security > Camera.

**Accessibility** — required to move the cursor and to notice when you touch the
real mouse. System Settings > Privacy & Security > Accessibility. Without it the
app runs and tracks your hands perfectly while silently failing to move
anything, so it says so loudly at startup.

**Menu Bar** — macOS 26 hosts every third-party status item through Control
Center. System Settings > Menu Bar > Allow in the Menu Bar. Without it the
process is running and there is nothing to click. If MindControl is not in that
list, the bundle never registered; rebuild with `make app`.

The native helper needs the same grant **in its own right**, because macOS
attaches the permission to a binary rather than to a project. That is a feature
here: granted to `mindcontrol-bridge` it survives rebuilding your virtualenv,
which a grant made to `.venv/bin/python` does not. The helper also uses it for a
second purpose — asking the window server what is on screen, which is how it
knows what to highlight. Without it, motion and clicks still work and snapping
silently does not, so it says so on startup too.

`make permissions` opens those panes. Installed as a bundle there is only one
entry to enable, `MindControl`: the helper is spawned by the app and lives
inside it, so macOS holds the app responsible for what it asks for and the grant
covers both. Running from a checkout is where the helper needs its own entry,
because there the responsible process is your terminal.

### Keeping the grants across updates

An ad-hoc signature — the default, because it needs nothing set up — pins the
bundle's designated requirement to a hash of its own contents:

```
$ codesign -d -r- /Applications/MindControl.app
designated => cdhash H"e3bbe92e…"
```

Every update changes that hash, so macOS sees a different application, asks for
the camera again and quietly stops honouring the Accessibility entry — while
still showing its switch as on, because the entry belongs to the copy you built
last time. That failure is worth recognising: the app runs, tracks your hands,
and moves nothing, which looks like a bug in the tracking and is a signature.

Two copies of the bundle do the same thing to each other. `build/MindControl.app`
and `/Applications/MindControl.app` are two applications with one identifier, so
grant the one you run, which `make run` and `make update` take to be the
installed one.

Signing with a certificate pins the requirement to the certificate instead, which
does not change. Any code-signing certificate on your keychain is found and used
without being asked for:

```
$ packaging/identity.sh
Developer ID Application: …
```

With none, builds are ad-hoc and the grants are given again each time.
`SIGN_IDENTITY` overrides the choice, and `SIGN_IDENTITY=-` forces ad-hoc. If you
have no certificate, Keychain Access > Certificate Assistant > Create a
Certificate… makes one: identity type **Self Signed Root**, certificate type
**Code Signing**. Grant the three permissions once to a build signed that way and
later `make update`s keep them.

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
mindcontrol bridge               # build the native helper
```

When snapping picks the wrong thing, ask the helper what it can see under the
cursor:

```bash
native/.build/release/mindcontrol-bridge --inspect
```

It prints every candidate with its role, its distance, and its size, and marks
the one it would choose with `->`. This is how every selection bug so far was
found rather than reasoned about: a container directly under the cursor scoring
zero and so beating the button inside it, word lookup asking for a fixed 96
characters of context in documents shorter than that, a panel-sized `AXGroup`
winning over empty space, and the whole Dock going dark because `AXDockItem` was a
role the ranking had never been shown. It takes no single-instance claim, so it
works while the real helper is running.

When the cursor ends up somewhere you did not ask for, the goal, the destination
and the posted position are three different numbers, and the only useful question
is which of them disagreed:

```bash
native/.build/release/mindcontrol-bridge --trace   # one line per tick, on stderr
```

Every button also prints where it actually landed and what it was aimed at, which
the per-tick lines cannot show: a click is precisely the moment the cursor stops
being where the tick said it was. Reading a press resolve to a word 130 px away
is how the stale-position bug above was found, after two rounds of guessing at it.

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
Python -- what a hand meant
  cameras (one thread each)
  -> hand landmarks per camera        tracking/hands.py
  -> gaze features on the primary     tracking/gaze.py
  -> merge cameras into one view      fusion.py
  -> classify shape, run the machine  geometry.py, gestures/engine.py
  -> send intents over a socket       control/bridge.py

Swift -- what the cursor does about it        native/Sources/BridgeCore/
  <- 48-byte datagrams                Protocol.swift, Transport.swift
  -> integrate motion at display rate Motion.swift
  -> ask what is on screen            Probe.swift, Targets.swift
  -> post real input events           Cursor.swift
  -> draw the highlight               Overlay.swift
  -> be the only helper running       Solitary.swift
  -> assemble the above               Run.swift
```

`BridgeCore` is a library and `native/Sources/Bridge/main.swift` is a two-line
executable over it, so target selection and the wire format can be tested without
a screen, an Accessibility grant, or another application to interrogate:
`swift test --package-path native`.

`pipeline.py` runs the camera loop on a worker thread. `app.py` keeps the main
thread for the menu bar, because a macOS status item needs the Cocoa run loop.
`control/modes.py` arbitrates between your hands and your hardware, watching for
physical input on a private run loop of its own.

Every event the app injects is tagged, and the watcher ignores anything carrying
that tag. Without it the app would see its own cursor motion, conclude a human
had grabbed the mouse, and suspend itself the instant it started working. Both
sides stamp the same tag, so `control/events.py` and `Cursor.swift` have to agree
about it.

### Why the cursor moved to Swift

Not for speed in the abstract. Three things could not be done from Python, and
each was measured before it was moved.

**Interpolation.** The camera has an opinion thirty times a second; the display
can show a new position a hundred and twenty times. Posting one move per camera
frame is four refreshes of stillness and then a jump, which is what "not smooth"
was. The helper accumulates deltas into a goal and walks the cursor there with a
critically damped spring, on a thread that asks for the user-interactive band and
is never behind the GIL while MediaPipe is running inference. Counted with an
event tap: **4.9 events per intent at 114 Hz**, 8.4 ms apart, largest step 7 px,
against 1.0 event per intent 33 ms apart and 20 px each.

**One writer.** Cursor motion used to be posted from wherever a frame arrived, so
a gaze warp and a hand delta could each be computed from a position the other had
already invalidated. That is what "overlapping with itself" was. Now one thread
owns the cursor and *clicks are queued to it too*, which makes the ordering total:
a press and its release always bracket exactly the motion between them. It costs a
click one tick — eight milliseconds — and no measured reversal survives it.

One thread is only one writer if there is also one process, and that does not come
for free. Binding the socket unlinks whatever was there, so a second helper takes
every frame and looks perfectly healthy while the first sits on its last goal with
a live motion thread, a live probe, and a second highlight window — started two
deliberately and got exactly that. So the claim is held as an `flock`, which the
kernel drops however the holder dies, and the newcomer evicts the incumbent rather
than refusing to start: an orphan nobody can see must not be able to block every
future launch. A helper also exits on its own once `getppid()` is 1, because a
parent that crashes never gets to clean up. `--inspect` deliberately takes no
claim, since diagnosing a *running* cursor is most of what it is for.

One writer is still not the *only* writer, because the rest of the system has
never agreed to that. Another application warping the pointer, a dialog taking
focus, Mission Control — after any of them the helper's idea of where the cursor
is describes somewhere it no longer is, and every target and pull computed from it
is wrong in a way nothing on screen reveals until a click lands in the wrong
place. Measured: two identical drags in a row, the second pressing on a word 130
px from where the cursor had been put, because the probe was still answering about
the first. So the real position is compared against the posted one every refresh,
and any disagreement is adopted as truth — the goal, the velocity, the held
target and every probe taken before that moment all discarded together.

Only while the cursor is at rest, though, and that restriction is the whole
reason it is safe: mid-flight the window server is legitimately a frame behind
what was just posted, and at the top speed allowed here a frame is two hundred
pixels, indistinguishable from a real jump. At rest there is nothing to mistake.
Worth knowing what "at rest" has to mean — the cursor stopped moving, *not* the
cursor reached its goal, because with a target in range the spring settles at the
goal plus the pull. Comparing against the goal held the check off permanently at
a 7 px steady-state offset, which is exactly how the bug above survived its first
fix.

**Knowing what is on screen.** Snapping needs to ask, and asking is synchronous
IPC into another application's main thread:

| | pyobjc | native Swift |
|---|---|---|
| one attribute read | 1140 µs | 382 µs |
| whole-window tree walk (2419 nodes) | 2490 ms | 4251 ms |
| single-point hit test | — | 0.43 ms (p95 3.46 ms) |
| four attributes, batched vs separate | — | 0.14 vs 0.31 ms |

The middle row is the important one: building a target list by walking a window is
hopeless in *either* language — four seconds, by which time the layout has changed.
So nothing walks the tree. The primitive that works is the single-point hit test,
which answers "what is here" rather than "what exists", and nearness is
reconstructed by asking about the cursor, the point it is heading for, and a ring
around that. Swift is only 1.2× faster per node, because the cost is the IPC — but
it is 3× faster per call, and the p95 is why the probe runs on a thread nobody
waits on, with a messaging timeout so an application wedged on its own main thread
costs one skipped probe rather than a frozen cursor.

### What snapping actually does

Choosing a target is not finding the closest rectangle. Four corrections turn
proximity into intent, and they are tunable in `[native]`:

- **Role.** A hit test lands on whatever is deepest at the point, which is usually
  the group containing the button — 18 of 25 sampled screen points returned an
  `AXGroup`. A button outranks its container even when the container is nearer.
- **Scenery is refused outright.** Outranking a container is not enough when it is
  the only candidate: a Finder window with the cursor in the gap between two icons
  offered a 614×756 `AXGroup` at zero distance, which won, drew a highlight over
  most of the window, and pulled nowhere at all — a large target's anchor clamps to
  where the cursor already was. Nothing is the right answer over empty space.
  Containers are therefore named and excluded rather than merely outranked. A role
  the table has never seen is judged by its shape instead of refused, because
  refusing all of them silently killed the entire Dock, where an icon is an
  `AXDockItem`.
- **Heading.** Where the hand is travelling is better evidence than where it
  currently is, so a target ahead of the cursor beats an equidistant one behind.
- **Stickiness.** The target already held keeps a bonus, or a cursor resting on the
  boundary between two buttons alternates every probe and the highlight strobes.
  This is the pinch detector's hysteresis applied to space instead of time.

The pull is a force, not a jump: it fades to nothing at the edge of the snap
radius so crossing that boundary is not felt, and a large target's anchor is the
nearest point on it, so the pull vanishes once you are inside and the hand has
full freedom again. Exactness comes at click time instead — a click resolves to
the highlighted target itself, so the highlight never promises something the click
does not honour. A drag freezes its target, except when it began on a word, in
which case it keeps snapping to words.

That last exception is what makes selecting a range land on whole ones, and it
needs the two ends resolved differently. A click on a word wants its middle, which
is where a caret belongs inside a word — but a press is the start of a selection,
so it takes the word's *leading* edge, and the release takes the far edge of the
word it ends on, measured against the word it began on so that dragging leftwards
takes the first word entirely rather than the last. Without it, dragging from
"quick" to "lazy" in TextEdit selected `ick brown fox jumps over the la`; with it,
the same drag selects whole words, and a drag that stays inside one word selects
that word.

The highlight is a single Core Animation layer, retargeted rather than redrawn.
That is why it cannot overlap itself, and why it glides at display rate even when
the probe underneath it is stuttering.

### Text, and where it stops working

Aiming at a word uses three calls: the point maps to a character index, a clamped
window of text around that index gives the word's extent, and one bounds query
turns the extent into a rectangle. Verified in TextEdit: a 23×14 box on the word
directly under the cursor, and neighbouring words picked up from the probe ring
when the cursor is on a space between them.

Two things had to be true for that to be trustworthy. Prose is aimed *inside*, not
at, so a text element is only a target in its own right if it is short enough to be
a field — otherwise a 656×384 document view wins by default and outlines the very
text it is supposed to be pointing at. And there is no error for "no glyph here":
a position past the end of the text answers with index zero, indistinguishable from
a real hit on the first character, which offered the document's opening word to a
point measured 92 px away in the bottom margin. Since the bounds of the answer are
already being fetched, they are checked against the question — if the box is not on
the line that was asked about, the answer is discarded. Clicking blank space still
places the caret there; it simply no longer lights up a word to promise otherwise.

Chromium and Electron windows answer the character count with nothing, so no
amount of asking will find a word inside one — including in this project's own
editor. Whole-element snapping still works there; word snapping does not.
`PixelTextLocator` in `Probe.swift` is the seam for the way around it, reading
glyph boxes from the pixels instead. It is deliberately unimplemented: Vision's
fast recogniser is affordable at 4 ms on a 240×120 patch, but one-shot capture
measured 53 ms regardless of region size, so doing it properly means holding a
persistent `SCStream` on the focused window and asking for Screen Recording
permission on top of Accessibility. Every caller already copes with a locator that
declines to answer.

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
