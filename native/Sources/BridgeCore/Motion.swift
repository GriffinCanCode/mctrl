// The clock the cursor lives on.
//
// The camera produces about thirty opinions a second and the display can show a
// hundred and twenty positions in the same time. Posting one move per camera
// frame is what made the old cursor feel stepped: four refreshes of stillness,
// then a jump. So the gesture engine's deltas no longer move the cursor at all --
// they move a *goal*, and this thread walks the cursor toward that goal once per
// refresh with a critically damped spring. Same information, four times the
// resolution, and no overshoot.
//
// Everything that touches the cursor is funnelled through this one thread,
// including clicks. Button intents arrive on the socket thread and are queued
// rather than acted on, which is what makes ordering total: a click can never be
// posted from a position that a move, arriving on another thread a microsecond
// later, has already invalidated. That race is what "overlapping with itself"
// was.
//
// Queueing costs a click at most one tick of delay -- eight milliseconds, below
// anything anyone can feel -- and buys the guarantee that a press and its release
// bracket exactly the motion between them.

import CoreGraphics
import Foundation

private enum PendingAction {
    case click(MouseButton)
    case press(MouseButton)
    case release
    case scroll(dx: Double, dy: Double)
    case releaseAll
    case refreshDisplays
}

final class MotionCore {
    private let cursor: Cursor
    private let probe: TargetProbe
    private let overlay: OverlayPresenter

    private let lock = NSLock()
    private var tuning: Tuning
    private var goal: CGPoint
    private var pending: [PendingAction] = []
    private var flags: ModeFlags = []

    // Owned solely by the motion thread below this line.
    private var position: CGPoint
    private var velocity = CGVector(dx: 0, dy: 0)
    private var posted: CGPoint
    /// Where the cursor was one tick ago, which is how "at rest" is told apart from
    /// "coasting slowly".
    private var previousPosition: CGPoint
    private var heldTarget: Int?
    /// Probes taken before this describe somewhere the cursor no longer is.
    ///
    /// Adopting a new position is a discontinuity, and everything the probe found
    /// around the old one stops being evidence about what is nearby. Trusting it
    /// anyway let a press resolve to a word measured 86 px from where the cursor
    /// visibly was -- the pull normally closes that gap before a click happens, and
    /// a jump is exactly the case where it has had no chance to.
    private var trustProbesAfter: Double = 0
    /// The target a drag began on. A drag must not re-snap to whatever it passes
    /// over, or dropping a file would fight you the whole way across the screen.
    private var dragAnchorKind: TargetKind?
    /// The word a text drag began on, kept so the release knows which end of the
    /// far word to land on.
    private var dragAnchorRect: CGRect?

    private var thread: Thread?
    private var running = false

    /// Print one line per tick that moves the cursor, for `--trace`. The goal, the
    /// destination and the posted position are three different numbers, and when
    /// the cursor ends up somewhere unexpected the only useful question is which of
    /// the three disagreed.
    var tracing = false
    private var traceTick = 0

    init(cursor: Cursor, probe: TargetProbe, overlay: OverlayPresenter, tuning: Tuning) {
        self.cursor = cursor
        self.probe = probe
        self.overlay = overlay
        self.tuning = tuning
        let start = cursor.location()
        goal = start
        position = start
        posted = start
        previousPosition = start
    }

    // ------------------------------------------------------------------ intents

    func handle(_ frame: Frame) {
        switch frame.intent {
        case .moveBy:
            lock.lock()
            goal = cursor.clamp(CGPoint(x: goal.x + frame.a, y: goal.y + frame.b))
            lock.unlock()

        case .warpToFraction:
            // Gaze is a fraction of the calibrated display, not of the desktop:
            // calibration can only teach where you look on the screen the camera
            // watched, and a fraction of a three-monitor desk would put the cursor
            // on a display it was never calibrated for.
            let box = cursor.mainDisplay
            lock.lock()
            goal = cursor.clamp(
                CGPoint(x: box.minX + frame.a * box.width, y: box.minY + frame.b * box.height))
            lock.unlock()

        case .click: enqueue(.click(frame.mouseButton))
        case .press: enqueue(.press(frame.mouseButton))
        case .release: enqueue(.release)
        case .scroll: enqueue(.scroll(dx: frame.a, dy: frame.b))
        case .releaseAll: enqueue(.releaseAll)

        case .setMode:
            lock.lock()
            let was = flags
            flags = frame.modeFlags
            // Coming back from suspended or disengaged, the cursor may have been
            // moved by hand in the meantime. Adopt wherever it actually is rather
            // than resuming from a stale goal and flinging across the screen.
            if !was.contains(.engaged) && frame.modeFlags.contains(.engaged) { resyncLocked() }
            lock.unlock()

        case .reloadConfig, .shutdown:
            break  // Handled by the owner, which has the paths and the run loop.
        }
    }

    private func enqueue(_ action: PendingAction) {
        lock.lock()
        pending.append(action)
        lock.unlock()
    }

    func apply(_ tuning: Tuning) {
        lock.lock()
        self.tuning = tuning
        lock.unlock()
    }

    func displaysChanged() { enqueue(.refreshDisplays) }

    /// Adopt the real cursor position. Caller holds the lock.
    private func resyncLocked() {
        let actual = cursor.location()
        goal = actual
        pendingResync = actual
    }

    /// Set under the lock by the socket thread, consumed by the motion thread.
    private var pendingResync: CGPoint?

    // -------------------------------------------------------------------- loop

    func start() {
        running = true
        let thread = Thread { [weak self] in self?.loop() }
        thread.name = "bridge-motion"
        // The highest band available. This thread does a few hundred floating
        // point operations and one event post per refresh; if it is late, the
        // cursor visibly stutters, and nothing else here is more urgent.
        thread.qualityOfService = .userInteractive
        thread.threadPriority = 1.0
        self.thread = thread
        thread.start()
    }

    func stop() {
        running = false
        thread = nil
    }

    /// Refresh rate of the display, so the cursor is integrated at exactly the
    /// rate the screen can show. Some displays report zero, in which case sixty is
    /// the safe assumption.
    private func refreshHz() -> Double {
        let reported = CGDisplayCopyDisplayMode(CGMainDisplayID())?.refreshRate ?? 0
        return reported > 1 ? min(max(reported, 60), 240) : 60
    }

    private func loop() {
        var hz = refreshHz()
        var interval = 1.0 / hz
        var nextTick = CFAbsoluteTimeGetCurrent()
        var sinceRateCheck = 0

        while running {
            let now = CFAbsoluteTimeGetCurrent()
            // Re-read the refresh rate now and then: plugging in a monitor or
            // ProMotion changing gear both alter it.
            sinceRateCheck += 1
            if sinceRateCheck >= 600 {
                sinceRateCheck = 0
                hz = refreshHz()
                interval = 1.0 / hz
            }

            tick(dt: interval)

            nextTick += interval
            // A late tick must not accumulate debt, or the loop spins trying to
            // catch up on time that has already passed.
            if nextTick < now { nextTick = now + interval }
            let wait = nextTick - CFAbsoluteTimeGetCurrent()
            if wait > 0 { usleep(UInt32(wait * 1_000_000)) }
        }
    }

    private func tick(dt: Double) {
        lock.lock()
        let tuning = self.tuning
        var goal = self.goal
        let flags = self.flags
        let actions = pending
        pending.removeAll(keepingCapacity: true)
        let resync = pendingResync
        pendingResync = nil
        lock.unlock()

        let wasAt = position
        defer { previousPosition = wasAt }

        if let resync {
            adopt(resync)
            goal = resync
        } else if let elsewhere = movedByAnythingElse() {
            adopt(elsewhere)
            goal = elsewhere
            lock.lock()
            self.goal = elsewhere
            lock.unlock()
        }

        let engaged = flags.contains(.engaged)
        // Snapping applies while aiming and not while sweeping: a scroll or a
        // swipe is not pointing at anything, and a magnetic pull during one fights
        // the hand rather than helping it.
        let aiming = engaged && flags.contains(.pointing) && !flags.contains(.sweeping)

        let target = pickTarget(at: position, aiming: aiming, tuning: tuning)
        heldTarget = target?.identity

        var destination = goal
        if let target, dragAllowsSnap(to: target) {
            let pull = pullVector(from: goal, to: target, tuning: tuning)
            destination = CGPoint(x: goal.x + pull.dx, y: goal.y + pull.dy)
        }

        integrate(toward: destination, dt: dt, tuning: tuning)

        if engaged, cursor.held != nil || position.distance(to: posted) >= tuning.minimumStepPixels {
            posted = position
            cursor.move(to: position)
        }

        perform(actions, target: target, tuning: tuning)

        probe.aim(at: position, velocity: velocity, wantsSnap: aiming)
        overlay.show(target, enabled: tuning.overlayEnabled && aiming, tuning: tuning)

        if tracing { trace(goal: goal, destination: destination, target: target, dt: dt) }
    }

    private func trace(goal: CGPoint, destination: CGPoint, target: Target?, dt: Double) {
        traceTick += 1
        FileHandle.standardError.write(
            Data(
                String(
                    format:
                        "[trace] %5d dt %.4f  goal %7.1f,%-7.1f dest %7.1f,%-7.1f "
                        + "pos %7.1f,%-7.1f v %7.0f,%-7.0f  %@\n",
                    traceTick, dt, goal.x, goal.y, destination.x, destination.y, position.x,
                    position.y, velocity.dx, velocity.dy,
                    target.map { "\($0.role) \($0.kind == .textRange ? "word" : "elem")" }
                        as NSString? ?? "-" as NSString
                ).utf8))
    }

    /// Where a button landed and what it was aimed at. The one thing the per-tick
    /// trace cannot show, because a click is exactly the moment the cursor stops
    /// being where the tick said it was.
    private func traceButton(_ what: String, at point: CGPoint, target: Target?) {
        let aim =
            target.map { "\($0.role) \($0.kind == .textRange ? "word" : "elem") \($0.rect)" }
            ?? "no target"
        FileHandle.standardError.write(
            Data("[trace] \(what) at \(point.x),\(point.y)  <- \(aim)\n".utf8))
    }

    /// Take a position as the truth, discarding everything derived from the old one.
    private func adopt(_ point: CGPoint) {
        position = point
        posted = point
        velocity = CGVector(dx: 0, dy: 0)
        heldTarget = nil
        trustProbesAfter = CFAbsoluteTimeGetCurrent()
    }

    /// The cursor's real position, when something other than this thread put it
    /// there.
    ///
    /// The helper assumes it owns the cursor, and when that stops being true --
    /// another application warping the pointer, a dialog taking focus, Mission
    /// Control, a script -- every position, target and pull downstream describes
    /// somewhere the cursor is not.
    ///
    /// Only asked while the spring is at rest, and that restriction is the whole
    /// reason this is safe. Mid-flight, the window server is legitimately a frame or
    /// so behind what was just posted, and at the top speed allowed here that lag is
    /// two hundred pixels -- indistinguishable from a real jump, so checking then
    /// would have the helper fighting its own motion. At rest there is no lag to
    /// mistake, and noticing within one refresh is what lets the probe re-aim
    /// *before* the next click instead of after it.
    private func movedByAnythingElse() -> CGPoint? {
        // At rest means the cursor stopped moving, which is not the same as having
        // reached the goal: with a target in range the spring settles at the goal
        // *plus* the snap pull, so a resting cursor sits a few pixels off its goal
        // for as long as the pull lasts. Comparing against the goal instead was
        // measured holding this check off permanently at a 7 px steady-state offset.
        guard velocity.magnitude < 4, position.distance(to: previousPosition) < 0.5 else {
            return nil
        }
        let actual = cursor.location()
        // Generous: this is for noticing jumps, not for tracking every pixel. The
        // trackpad is not the case being caught -- touching it suspends the app
        // outright -- so anything real here has moved the cursor properly.
        return actual.distance(to: posted) > 6 ? actual : nil
    }

    /// A critically damped spring: fastest approach that never overshoots. An
    /// overshooting cursor reads as sloppy in a way that a slightly slow one does
    /// not, which is why this is not tuned for speed alone.
    private func integrate(toward destination: CGPoint, dt: Double, tuning: Tuning) {
        let omega = 2.0 / max(tuning.motionTimeConstant, 0.008)
        let ax = omega * omega * (destination.x - position.x) - 2 * omega * velocity.dx
        let ay = omega * omega * (destination.y - position.y) - 2 * omega * velocity.dy
        velocity.dx += ax * dt
        velocity.dy += ay * dt

        let speed = (velocity.dx * velocity.dx + velocity.dy * velocity.dy).squareRoot()
        if speed > tuning.maximumSpeed {
            let scale = tuning.maximumSpeed / speed
            velocity.dx *= scale
            velocity.dy *= scale
        }
        position = cursor.clamp(
            CGPoint(x: position.x + velocity.dx * dt, y: position.y + velocity.dy * dt))
    }

    private func pickTarget(at point: CGPoint, aiming: Bool, tuning: Tuning) -> Target? {
        guard aiming || cursor.held != nil else { return nil }
        let snapshot = probe.snapshot
        guard !snapshot.targets.isEmpty, snapshot.takenAt >= trustProbesAfter,
            (CFAbsoluteTimeGetCurrent() - snapshot.takenAt) * 1000 < tuning.targetLifetimeMs
        else { return nil }
        return chooseTarget(
            from: snapshot.targets, at: point, velocity: velocity, holding: heldTarget,
            tuning: tuning)
    }

    /// Whether a drag in progress may still be snapped.
    ///
    /// A drag that began on a word keeps snapping to words, which is what makes
    /// selecting "from here to there" land on whole words instead of splitting
    /// them. A drag that began on anything else stops snapping entirely: dragging
    /// a file is a continuous act, and re-aiming it at every control it passes
    /// over would be unusable.
    private func dragAllowsSnap(to target: Target) -> Bool {
        guard let kind = dragAnchorKind else { return true }
        return kind == .textRange && target.kind == .textRange
    }

    /// Offset added to the goal to lean the cursor toward a target.
    ///
    /// A force rather than a jump: a teleport onto every passing button would be
    /// unusable, and the point is to bias aim, not to seize it. The pull fades to
    /// nothing at the edge of the snap radius so crossing that boundary is not
    /// felt, and a large target's anchor is simply the nearest point on it, so the
    /// pull vanishes once the cursor is inside and the hand regains full freedom.
    ///
    /// The steady-state offset is a fraction of the gap, not all of it, so the
    /// cursor stays visibly under the user's control. Exactness comes at click
    /// time instead: `resolve` puts the button event on the target itself.
    private func pullVector(from point: CGPoint, to target: Target, tuning: Tuning) -> CGVector {
        let anchor = target.anchor(from: point, smallerThan: tuning.smallTargetPixels)
        let dx = anchor.x - point.x
        let dy = anchor.y - point.y
        let gap = (dx * dx + dy * dy).squareRoot()
        guard gap > 0.5, tuning.snapRadius > 1 else { return CGVector(dx: 0, dy: 0) }
        let closeness = max(0, 1 - gap / tuning.snapRadius)
        let magnitude = gap * tuning.snapStrength * closeness
        return CGVector(dx: dx / gap * magnitude, dy: dy / gap * magnitude)
    }

    /// Where a button event should actually land.
    ///
    /// The highlight promises a target; the click has to honour it exactly, or the
    /// two disagree and the feature is worse than nothing. So a click resolves to
    /// the target's centre -- which for a word means the middle of the word, where
    /// a caret lands inside it -- and the cursor is moved there first so that what
    /// happened is visible.
    private func resolve(_ target: Target?, tuning: Tuning) -> CGPoint {
        guard let target else { return position }
        let anchor = target.kind == .textRange || target.rect.width <= tuning.smallTargetPixels
            && target.rect.height <= tuning.smallTargetPixels
            ? CGPoint(x: target.rect.midX, y: target.rect.midY)
            : target.anchor(from: position, smallerThan: tuning.smallTargetPixels)
        return cursor.clamp(anchor)
    }

    /// Where a press should land.
    ///
    /// On a word this is the word's leading edge rather than its middle. A click
    /// wants the middle -- that is where a caret belongs inside a word -- but a
    /// press is the start of a drag, and a selection that begins halfway through its
    /// first word is not the range anyone meant. Measured before this: dragging from
    /// "quick" to "lazy" selected "ick brown fox jumps over the la".
    private func resolvePress(_ target: Target?, tuning: Tuning) -> CGPoint {
        guard let target, target.kind == .textRange else { return resolve(target, tuning: tuning) }
        return cursor.clamp(CGPoint(x: target.rect.minX, y: target.rect.midY))
    }

    /// Where a text drag should end, so the selection covers whole words.
    ///
    /// The far edge of the word under the release, measured against the word the
    /// drag began on: selecting rightwards should take the last word entirely,
    /// leftwards the first. Anything that is not a word-to-word drag ends exactly
    /// where the cursor is, because a file being dropped means the position and
    /// nothing else.
    private func resolveDragEnd(_ target: Target?) -> CGPoint {
        guard let anchor = dragAnchorRect, let target, target.kind == .textRange else {
            return position
        }
        let rightwards = target.rect.midX >= anchor.midX
        return cursor.clamp(
            CGPoint(x: rightwards ? target.rect.maxX : target.rect.minX, y: target.rect.midY))
    }

    private func letGo(at point: CGPoint) {
        if point != position { settle(at: point) }
        cursor.release(at: point)
        dragAnchorKind = nil
        dragAnchorRect = nil
    }

    private func perform(_ actions: [PendingAction], target: Target?, tuning: Tuning) {
        for action in actions {
            switch action {
            case .click(let button):
                let point = resolve(target, tuning: tuning)
                if tracing { traceButton("click", at: point, target: target) }
                settle(at: point)
                cursor.click(button, at: point)

            case .press(let button):
                let point = resolvePress(target, tuning: tuning)
                if tracing { traceButton("press", at: point, target: target) }
                settle(at: point)
                dragAnchorKind = target?.kind
                dragAnchorRect = target?.kind == .textRange ? target?.rect : nil
                cursor.press(button, at: point)

            case .release:
                let point = resolveDragEnd(target)
                if tracing { traceButton("release", at: point, target: target) }
                letGo(at: point)

            case .releaseAll:
                // No word rounding here: this is a suspend or a disengage, and the
                // only thing that matters is that nothing is left held down.
                letGo(at: position)

            case .scroll(let dx, let dy):
                cursor.scroll(dx: dx, dy: dy)

            case .refreshDisplays:
                cursor.refreshDisplays()
            }
        }
    }

    /// Put the cursor exactly where the event is about to be posted, and stop it
    /// there. Leaving velocity behind would let the spring drift the cursor off the
    /// target between a press and its release, turning a click into a tiny drag.
    private func settle(at point: CGPoint) {
        position = point
        posted = point
        velocity = CGVector(dx: 0, dy: 0)
        lock.lock()
        goal = point
        lock.unlock()
        cursor.move(to: point)
    }
}

extension CGPoint {
    func distance(to other: CGPoint) -> Double {
        let dx = x - other.x
        let dy = y - other.y
        return (dx * dx + dy * dy).squareRoot()
    }
}

extension CGVector {
    var magnitude: Double { (dx * dx + dy * dy).squareRoot() }
}
