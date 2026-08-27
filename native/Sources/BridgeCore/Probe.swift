// Finding out what is on the screen, without ever making the cursor wait.
//
// Every accessibility query is a synchronous round trip into the target
// application's main thread, and that is the entire reason this file exists as a
// separate thread rather than a function the motion core calls. Measured on this
// machine:
//
//   one attribute read                     382 us
//   whole-window tree walk, 2419 nodes    4251 ms   -- 0.24 Hz, unusable
//   single-point hit test                 0.43 ms median, 3.46 ms at p95
//   four attributes batched vs separate   0.14 ms vs 0.31 ms
//
// So walking a window to build a list of targets is out, in any language: it is
// four seconds of work and the layout has changed by the time it finishes. The
// primitive that does work is the single-point hit test, which answers "what is
// here" rather than "what exists". Nearness is then reconstructed by asking about
// a handful of points instead of enumerating everything.
//
// The p95 is the reason for the thread. A cursor that stalled for 3.5 ms would
// drop frames visibly, and an application wedged on its own main thread would
// freeze the pointer outright -- so the probe absorbs that latency out of band,
// with a messaging timeout as a backstop, and publishes results the motion core
// reads whenever it happens to look.

import ApplicationServices
import CoreGraphics
import Foundation

// ------------------------------------------------------------ AX plumbing

func axCopy(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyAttributeValue(element, attribute as CFString, &value) == .success
        ? value : nil
}

/// Several attributes in one round trip. Worth the awkwardness: measured at
/// 0.14 ms against 0.31 ms for the same four fetched separately, and the probe
/// does this for every candidate it considers.
func axCopyMany(_ element: AXUIElement, _ attributes: [String]) -> [CFTypeRef?] {
    var raw: CFArray?
    let status = AXUIElementCopyMultipleAttributeValues(
        element, attributes as CFArray, AXCopyMultipleAttributeOptions(), &raw)
    guard status == .success, let values = raw as? [CFTypeRef], values.count == attributes.count
    else {
        // The batched call fails as a unit, so fall back rather than lose the
        // attributes that would have succeeded.
        return attributes.map { axCopy(element, $0) }
    }
    // Unavailable attributes come back as a null placeholder rather than being
    // omitted, which keeps the array positionally aligned with the request.
    return values.map { CFGetTypeID($0) == CFNullGetTypeID() ? nil : $0 }
}

func axPoint(_ value: CFTypeRef?) -> CGPoint? {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    var point = CGPoint.zero
    return AXValueGetValue(value as! AXValue, .cgPoint, &point) ? point : nil
}

func axSize(_ value: CFTypeRef?) -> CGSize? {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    var size = CGSize.zero
    return AXValueGetValue(value as! AXValue, .cgSize, &size) ? size : nil
}

func axRect(_ value: CFTypeRef?) -> CGRect? {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    var rect = CGRect.zero
    return AXValueGetValue(value as! AXValue, .cgRect, &rect) ? rect : nil
}

func axRange(_ value: CFTypeRef?) -> CFRange? {
    guard let value, CFGetTypeID(value) == AXValueGetTypeID() else { return nil }
    var range = CFRange()
    return AXValueGetValue(value as! AXValue, .cfRange, &range) ? range : nil
}

func axParameterized(
    _ element: AXUIElement, _ attribute: String, _ parameter: CFTypeRef
) -> CFTypeRef? {
    var value: CFTypeRef?
    return AXUIElementCopyParameterizedAttributeValue(
        element, attribute as CFString, parameter, &value) == .success ? value : nil
}

// --------------------------------------------------------- the text seam

/// Locating a word inside a text element, so a click can land on the word meant
/// rather than the paragraph containing it.
protocol TextLocator {
    /// Box around the word under `point`, or nil if this locator cannot tell.
    func wordBox(in element: AXUIElement, at point: CGPoint) -> CGRect?
}

/// Words by asking the application, which is exact when it answers.
///
/// Three calls, not one per character: the position maps straight to a character
/// index, a short string window around that index gives the word's extent
/// locally, and one bounds query turns the extent into a rectangle. Measured at
/// 1.38 ms for the bounds call, which is why the result is cached rather than
/// recomputed per frame.
struct AccessibilityTextLocator: TextLocator {
    /// Characters either side of the hit to fetch when looking for word edges.
    private let window = 48

    func wordBox(in element: AXUIElement, at point: CGPoint) -> CGRect? {
        var hit = point
        guard let pointValue = AXValueCreate(.cgPoint, &hit),
            let indexRange = axRange(
                axParameterized(element, kAXRangeForPositionParameterizedAttribute, pointValue))
        else { return nil }

        // The string window has to be clamped to the text, not merely centred on
        // the hit. A range that overruns the end is rejected outright rather than
        // truncated -- AppKit answers -25201 and nothing comes back -- so asking
        // for a fixed 96 characters silently failed on every short document, which
        // is most of them.
        guard let length = axCopy(element, kAXNumberOfCharactersAttribute) as? Int, length > 0
        else { return nil }

        let index = indexRange.location
        let start = max(0, min(index - window, length - 1))
        var probe = CFRange(location: start, length: min(window * 2, length - start))
        guard probe.length > 0, let probeValue = AXValueCreate(.cfRange, &probe),
            let text = axParameterized(
                element, kAXStringForRangeParameterizedAttribute, probeValue) as? String,
            !text.isEmpty
        else { return nil }

        let characters = Array(text)
        let local = index - start
        guard local >= 0, local < characters.count else { return nil }

        let isWord: (Character) -> Bool = { $0.isLetter || $0.isNumber || $0 == "_" || $0 == "-" }
        guard isWord(characters[local]) else { return nil }

        var from = local
        while from > 0 && isWord(characters[from - 1]) { from -= 1 }
        var to = local
        while to + 1 < characters.count && isWord(characters[to + 1]) { to += 1 }

        var word = CFRange(location: start + from, length: to - from + 1)
        guard let wordValue = AXValueCreate(.cfRange, &word),
            let box = axRect(
                axParameterized(element, kAXBoundsForRangeParameterizedAttribute, wordValue)),
            box.width > 0, box.height > 0
        else { return nil }

        // The box has to be on the line that was asked about. There is no error
        // for "no glyph here": a position past the end of the text answers with
        // index zero, indistinguishable from a genuine hit on the first character.
        // Unchecked, that offered the document's opening word to a probe point in
        // the blank space below the last line -- measured at 92 px away, well
        // inside the snap radius, so the cursor would have been tugged to the top
        // of the document from anywhere in the margin. Comparing the answer's own
        // bounds against the question is the check AX does not provide.
        guard point.y >= box.minY - 2, point.y <= box.maxY + 2 else { return nil }
        return box
    }
}

/// Words by reading the pixels, for the applications that will not say.
///
/// Chromium and Electron windows -- which is most editors, including this
/// project's own -- answer `kAXNumberOfCharacters` with nothing at all, so no
/// amount of asking will locate a word inside one. The measured way around it is
/// to recognise glyph boxes in the pixels near the cursor: Vision's fast
/// recogniser costs 4 ms on a 240x120 patch and 27 ms on 960x480, both
/// affordable off the interaction path.
///
/// The catch is capture, not recognition. `SCScreenshotManager.captureImage`
/// measured 53 ms per call regardless of region size -- fixed setup cost, not
/// pixels -- so a one-shot grab per probe is far too expensive. Doing this
/// properly means holding a persistent `SCStream` on the focused window and
/// recognising from its most recent frame, which also means asking for Screen
/// Recording permission on top of Accessibility.
///
/// Deliberately unimplemented for now. The seam is here so that turning it on is
/// a matter of filling in one method, and every caller already copes with a
/// locator that declines to answer.
struct PixelTextLocator: TextLocator {
    func wordBox(in element: AXUIElement, at point: CGPoint) -> CGRect? { nil }
}

// ------------------------------------------------------------------ probe

/// Where the cursor is and where it is going, handed to the probe each tick.
struct Aim {
    var position: CGPoint = .zero
    var velocity: CGVector = CGVector(dx: 0, dy: 0)
    var wantsSnap: Bool = false
}

final class TargetProbe {
    private let tuningLock = NSLock()
    private var tuning: Tuning

    private let aimLock = NSLock()
    private var aim = Aim()

    private let snapshotLock = NSLock()
    private var current = TargetSnapshot()

    private let system: AXUIElement
    private let locators: [TextLocator]
    private var thread: Thread?
    private var running = false
    private var warnedUntrusted = false

    /// Word boxes are stable for as long as the text does not reflow, and cost a
    /// millisecond and a half to find, so they are kept per element between probes.
    private var wordCache: [Int: (rect: CGRect, at: Double)] = [:]

    init(tuning: Tuning) {
        self.tuning = tuning
        system = AXUIElementCreateSystemWide()
        AXUIElementSetMessagingTimeout(system, Float(tuning.probeTimeoutS))
        locators = [AccessibilityTextLocator(), PixelTextLocator()]
    }

    var snapshot: TargetSnapshot {
        snapshotLock.lock()
        defer { snapshotLock.unlock() }
        return current
    }

    func apply(_ tuning: Tuning) {
        tuningLock.lock()
        self.tuning = tuning
        tuningLock.unlock()
        AXUIElementSetMessagingTimeout(system, Float(tuning.probeTimeoutS))
    }

    private var settings: Tuning {
        tuningLock.lock()
        defer { tuningLock.unlock() }
        return tuning
    }

    /// Called from the motion thread every tick. Must stay trivial.
    func aim(at position: CGPoint, velocity: CGVector, wantsSnap: Bool) {
        aimLock.lock()
        aim = Aim(position: position, velocity: velocity, wantsSnap: wantsSnap)
        aimLock.unlock()
    }

    func start() {
        running = true
        let thread = Thread { [weak self] in self?.loop() }
        thread.name = "bridge-probe"
        // Below the motion thread on purpose. If the machine is saturated the
        // cursor must keep moving even if the highlight goes stale.
        thread.qualityOfService = .userInitiated
        self.thread = thread
        thread.start()
    }

    func stop() {
        running = false
        thread = nil
    }

    private func loop() {
        while running {
            let started = CFAbsoluteTimeGetCurrent()
            let settings = self.settings

            aimLock.lock()
            let aim = self.aim
            aimLock.unlock()

            if aim.wantsSnap && settings.snapEnabled {
                if AXIsProcessTrusted() {
                    let targets = gather(around: aim, tuning: settings)
                    publish(TargetSnapshot(targets: targets, takenAt: CFAbsoluteTimeGetCurrent()))
                } else if !warnedUntrusted {
                    warnedUntrusted = true
                    FileHandle.standardError.write(
                        Data(
                            """
                            [bridge] no Accessibility permission, so nothing can be \
                            highlighted or snapped to. Motion still works. Grant it in \
                            System Settings > Privacy & Security > Accessibility.\n
                            """.utf8))
                }
            } else if !snapshot.targets.isEmpty {
                publish(TargetSnapshot())
            }

            let spent = (CFAbsoluteTimeGetCurrent() - started) * 1000.0
            let remaining = settings.probeIntervalMs - spent
            if remaining > 0 { usleep(UInt32(remaining * 1000)) }
        }
    }

    private func publish(_ snapshot: TargetSnapshot) {
        snapshotLock.lock()
        current = snapshot
        snapshotLock.unlock()
    }

    /// One synchronous probe, for `--inspect`. Not on any hot path: this is how a
    /// user finds out why a particular button will not light up, which is otherwise
    /// invisible from outside the process.
    func inspect(at point: CGPoint) -> [Target] {
        gather(
            around: Aim(position: point, velocity: CGVector(dx: 0, dy: 0), wantsSnap: true),
            tuning: settings)
    }

    // ------------------------------------------------------------- gathering

    /// Hit test a small constellation of points and turn the results into targets.
    ///
    /// Nearness has to be reconstructed rather than queried: the hit test only
    /// reports what is directly under a point. So the cursor's own position is
    /// asked about, then the point it is heading for, then a ring around that --
    /// which is what lets a target the cursor is merely *near* still light up.
    private func gather(around aim: Aim, tuning: Tuning) -> [Target] {
        let ahead = CGPoint(
            x: aim.position.x + aim.velocity.dx * tuning.probeLookaheadS,
            y: aim.position.y + aim.velocity.dy * tuning.probeLookaheadS)

        var points = [aim.position, ahead]
        let ring = tuning.snapRadius * 0.6
        for step in 0..<6 {
            let angle = Double(step) / 6.0 * 2.0 * .pi
            points.append(CGPoint(x: ahead.x + cos(angle) * ring, y: ahead.y + sin(angle) * ring))
        }

        var found: [Int: Target] = [:]
        for point in points {
            guard let element = hitTest(point) else { continue }
            for target in resolve(element, at: point, tuning: tuning) {
                // First writer wins: points are ordered by relevance, the cursor's
                // own position first, so the earliest resolution of a given element
                // is the best-aimed one.
                if found[target.identity] == nil { found[target.identity] = target }
            }
        }
        return Array(found.values)
    }

    private func hitTest(_ point: CGPoint) -> AXUIElement? {
        var element: AXUIElement?
        let status = AXUIElementCopyElementAtPosition(
            system, Float(point.x), Float(point.y), &element)
        return status == .success ? element : nil
    }

    /// Role, frame and label in one round trip, or nil if the element has no
    /// usable geometry.
    private func describe(_ element: AXUIElement) -> (role: String, rect: CGRect, label: String)? {
        let values = axCopyMany(
            element,
            [kAXRoleAttribute, kAXPositionAttribute, kAXSizeAttribute, kAXTitleAttribute])
        guard let role = values[0] as? String,
            let origin = axPoint(values[1]), let size = axSize(values[2]),
            size.width > 0, size.height > 0
        else { return nil }
        return (role, CGRect(origin: origin, size: size), (values[3] as? String) ?? "")
    }

    /// Turn one hit into the candidates it implies: the element itself, a more
    /// specific child if the hit was vague, and a word for anything text-bearing.
    private func resolve(_ element: AXUIElement, at point: CGPoint, tuning: Tuning) -> [Target] {
        guard let hit = describe(element) else { return [] }

        // Kept alongside the targets because resolving a word needs the handle the
        // rectangle came from, and the hit is not always the text-bearing one.
        var candidates: [(element: AXUIElement, role: String, rect: CGRect, label: String)] = [
            (element, hit.role, hit.rect, hit.label)
        ]

        // A hit on a container is usually not what was meant -- 18 of 25 sampled
        // screen points landed on an AXGroup. Descend one level and keep any child
        // that is both near the point and more specific than its parent. One level
        // only: each is a round trip, and controls are rarely buried deeper than
        // that below the element the hit test already found.
        if !isSpecific(role: hit.role),
            let children = axCopy(element, kAXChildrenAttribute) as? [AXUIElement],
            children.count <= 64
        {
            for child in children {
                guard let described = describe(child),
                    distance(from: point, to: described.rect) <= tuning.snapRadius,
                    priority(of: described.role) > priority(of: hit.role)
                else { continue }
                candidates.append((child, described.role, described.rect, described.label))
            }
        }

        var targets = candidates.map {
            Target(
                rect: $0.rect, role: $0.role, label: $0.label, kind: .element,
                identity: identity(role: $0.role, label: $0.label, rect: $0.rect))
        }

        // Every text-bearing candidate, not only the hit. A hit test over a label
        // inside a group lands on the group, so asking only the hit would mean
        // words were never found in exactly the layouts where they matter most.
        if tuning.textSnapEnabled {
            for candidate in candidates where textBearingRoles.contains(candidate.role) {
                guard
                    let box = word(
                        in: candidate.element, at: point, rect: candidate.rect, tuning: tuning)
                else { continue }
                targets.append(
                    Target(
                        rect: box, role: candidate.role, label: candidate.label, kind: .textRange,
                        identity: identity(
                            role: "word:" + candidate.role, label: candidate.label, rect: box)))
            }
        }
        return targets
    }

    /// The word under a point, from whichever locator can answer, cached because
    /// the bounds query is the most expensive call the probe makes.
    private func word(
        in element: AXUIElement, at point: CGPoint, rect: CGRect, tuning: Tuning
    ) -> CGRect? {
        // Keyed by element and rounded point: moving within a word reuses the box,
        // moving to the next word asks again.
        let key = identity(
            role: "w", label: "",
            rect: CGRect(x: (point.x / 8).rounded(), y: (point.y / 8).rounded(), width: rect.width,
                         height: rect.height))
        let now = CFAbsoluteTimeGetCurrent()
        if let cached = wordCache[key], (now - cached.at) * 1000 < tuning.targetLifetimeMs {
            return cached.rect
        }
        for locator in locators {
            if let box = locator.wordBox(in: element, at: point) {
                if wordCache.count > 512 { wordCache.removeAll(keepingCapacity: true) }
                wordCache[key] = (box, now)
                return box
            }
        }
        return nil
    }

    /// Recognise the same target across probes. Rounded so that a rectangle
    /// jittering by a pixel -- which happens as applications relayout -- still
    /// counts as the target the cursor is already holding.
    private func identity(role: String, label: String, rect: CGRect) -> Int {
        var hasher = Hasher()
        hasher.combine(role)
        hasher.combine(label)
        hasher.combine(Int(rect.minX.rounded() / 2))
        hasher.combine(Int(rect.minY.rounded() / 2))
        hasher.combine(Int(rect.width.rounded() / 2))
        hasher.combine(Int(rect.height.rounded() / 2))
        return hasher.finalize()
    }
}
