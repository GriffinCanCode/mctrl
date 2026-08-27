// Does the highlight land on the thing you meant?
//
// Pure functions over value types, so these run without a screen, an
// accessibility grant, or another application to interrogate. Two of them are
// regressions for bugs that shipped and were caught by `--inspect` against a real
// window rather than by reasoning.

import ApplicationServices
import CoreGraphics
import Testing

@testable import BridgeCore

private let still = CGVector(dx: 0, dy: 0)

private func target(
    _ role: String, _ rect: CGRect, kind: TargetKind = .element, label: String = ""
) -> Target {
    // Identity only has to be unique within a test, and the rect is what varies.
    var hasher = Hasher()
    hasher.combine(role)
    hasher.combine(label)
    hasher.combine(rect.minX)
    hasher.combine(rect.minY)
    hasher.combine(kind == .textRange)
    return Target(rect: rect, role: role, label: label, kind: kind, identity: hasher.finalize())
}

@Suite("distance to a rectangle")
struct DistanceTests {
    @Test("a point inside is at no distance")
    func inside() {
        let rect = CGRect(x: 100, y: 100, width: 50, height: 20)
        #expect(distance(from: CGPoint(x: 120, y: 110), to: rect) == 0)
        #expect(distance(from: CGPoint(x: 100, y: 100), to: rect) == 0)
    }

    @Test("a point outside measures to the nearest edge, not the centre")
    func outside() {
        let rect = CGRect(x: 100, y: 100, width: 50, height: 20)
        // Directly left of the rect: 20 px from its left edge, though the centre is
        // 45 px away.
        #expect(distance(from: CGPoint(x: 80, y: 110), to: rect) == 20)
        // Diagonally off the corner: 3-4-5.
        #expect(distance(from: CGPoint(x: 96, y: 97), to: rect) == 5)
    }
}

@Suite("choosing a target")
struct SelectionTests {
    let tuning = Tuning()

    @Test("a specific control beats the container it sits inside")
    func specificBeatsContainer() {
        // The bug this holds still: cost used to be the raw distance scaled by
        // role priority, so anything the cursor was *inside* scored zero and won
        // regardless of how vague it was. Against a real window that meant the
        // highlight landed on a 141x35 AXGroup instead of the AXStaticText 8 px
        // away inside it -- the enclosing panel, never the button.
        let at = CGPoint(x: 255, y: 97)
        let candidates = [
            target(kAXGroupRole, CGRect(x: 248, y: 64, width: 141, height: 35)),
            target(kAXGroupRole, CGRect(x: 0, y: 29, width: 1496, height: 880)),
            target(kAXStaticTextRole, CGRect(x: 248, y: 74, width: 13, height: 15)),
        ]
        let chosen = chooseTarget(
            from: candidates, at: at, velocity: still, holding: nil, tuning: tuning)
        #expect(chosen?.role == kAXStaticTextRole)
    }

    @Test("the nearest of several equally specific controls wins")
    func nearestAmongEquals() {
        // Sampled from a real menu bar: the item under the cursor, and its
        // neighbours 12 px and 30 px away.
        let at = CGPoint(x: 120, y: 12)
        let candidates = [
            target(kAXMenuBarItemRole, CGRect(x: 108, y: 0, width: 42, height: 29), label: "File"),
            target(kAXMenuBarItemRole, CGRect(x: 44, y: 0, width: 64, height: 29), label: "Cursor"),
            target(kAXGroupRole, CGRect(x: 0, y: 29, width: 1496, height: 880)),
            target(kAXMenuBarItemRole, CGRect(x: 150, y: 0, width: 44, height: 29), label: "Edit"),
        ]
        let chosen = chooseTarget(
            from: candidates, at: at, velocity: still, holding: nil, tuning: tuning)
        #expect(chosen?.label == "File")
    }

    @Test("a container alone is no target at all")
    func sceneryIsRefused() {
        // Sampled from a Finder window with the cursor in the empty space between
        // two icons: the hit landed on a 614x756 AXGroup, and the icons were 87 and
        // 95 px off. The group won, and drew a highlight over most of the window
        // while pulling nowhere -- its anchor clamps to the cursor. Nothing is the
        // right answer here; the cursor is over empty space and should stay free.
        let at = CGPoint(x: 647, y: 594)
        let group = target(kAXGroupRole, CGRect(x: 304, y: 131, width: 614, height: 756))
        #expect(
            chooseTarget(from: [group], at: at, velocity: still, holding: nil, tuning: tuning)
                == nil)

        // Whereas an icon within reach is a target, and beats the same group.
        let icon = target(kAXImageRole, CGRect(x: 600, y: 560, width: 64, height: 64))
        let chosen = chooseTarget(
            from: [group, icon], at: at, velocity: still, holding: nil, tuning: tuning)
        #expect(chosen?.role == kAXImageRole)
    }

    @Test("an unfamiliar role is judged by its shape, not refused outright")
    func unknownRolesAreJudgedBySize() {
        // Refusing every unranked role was the first attempt, and it silently
        // killed the entire Dock: an icon there is an AXDockItem, a role with no
        // constant to import. Panel-sized is still scenery, though.
        let at = CGPoint(x: 50, y: 50)
        let page = target("AXWebArea", CGRect(x: 0, y: 0, width: 1400, height: 900))
        #expect(
            chooseTarget(from: [page], at: at, velocity: still, holding: nil, tuning: tuning) == nil)

        let control = target("AXSomethingNew", CGRect(x: 40, y: 40, width: 40, height: 52))
        #expect(
            chooseTarget(from: [control], at: at, velocity: still, holding: nil, tuning: tuning)
                != nil)
    }

    @Test("a Dock icon is a target, and the nearer of two wins")
    func dockItemsAreTargets() {
        // Sampled from the Dock with the cursor between two icons, 10 and 14 px
        // away. Both were refused outright until AXDockItem was ranked.
        let at = CGPoint(x: 720, y: 967)
        let near = target(
            kAXDockItemRole, CGRect(x: 690, y: 905, width: 40, height: 52), label: "Activity Monitor"
        )
        let far = target(
            kAXDockItemRole, CGRect(x: 730, y: 905, width: 40, height: 52), label: "Photos")
        let chosen = chooseTarget(
            from: [near, far], at: at, velocity: still, holding: nil, tuning: tuning)
        #expect(chosen?.label == "Activity Monitor")
    }

    @Test("a small container is still scenery, despite being control-sized")
    func namedContainersBeatTheShapeFallback() {
        // The shape fallback judges an unranked role by size, so every container
        // small enough to pass for a control has to be named. A short context menu
        // or a compact window is exactly that shape.
        let at = CGPoint(x: 50, y: 50)
        for role in [kAXMenuRole, kAXWindowRole, kAXScrollAreaRole, kAXListRole] {
            let small = target(role, CGRect(x: 30, y: 30, width: 60, height: 60))
            #expect(
                chooseTarget(from: [small], at: at, velocity: still, holding: nil, tuning: tuning)
                    == nil, "\(role)")
        }
    }

    @Test("a scrollbar thumb is aimable, since it is dragged rather than clicked")
    func dragHandlesAreTargets() {
        let thumb = target(kAXValueIndicatorRole, CGRect(x: 1480, y: 300, width: 12, height: 90))
        let bar = target(kAXScrollBarRole, CGRect(x: 1480, y: 60, width: 12, height: 800))
        let chosen = chooseTarget(
            from: [bar, thumb], at: CGPoint(x: 1486, y: 340), velocity: still, holding: nil,
            tuning: tuning)
        #expect(chosen?.role == kAXValueIndicatorRole)
    }

    @Test("nothing beyond the snap radius is offered")
    func radiusIsRespected() {
        let at = CGPoint(x: 0, y: 0)
        let far = target(
            kAXButtonRole,
            CGRect(x: tuning.snapRadius + 40, y: 0, width: 20, height: 20))
        #expect(
            chooseTarget(from: [far], at: at, velocity: still, holding: nil, tuning: tuning) == nil)
    }

    @Test("an empty candidate list yields nothing rather than crashing")
    func emptyIsSafe() {
        #expect(
            chooseTarget(from: [], at: .zero, velocity: still, holding: nil, tuning: tuning) == nil)
    }

    @Test("the target already held is kept when a rival is only marginally closer")
    func stickinessPreventsFlicker() {
        // Two buttons with a gap, and the cursor hovering in it slightly nearer the
        // right one. This is where the highlight strobes without hysteresis: a
        // couple of pixels of hand tremor flips the winner every probe.
        let left = target(kAXButtonRole, CGRect(x: 0, y: 0, width: 50, height: 20))
        let right = target(kAXButtonRole, CGRect(x: 60, y: 0, width: 50, height: 20))
        let at = CGPoint(x: 56, y: 10)  // 6 px from left, 4 px from right

        let cold = chooseTarget(
            from: [left, right], at: at, velocity: still, holding: nil, tuning: tuning)
        #expect(cold?.identity == right.identity, "with nothing held, the nearer one wins")

        let warm = chooseTarget(
            from: [left, right], at: at, velocity: still, holding: left.identity, tuning: tuning)
        #expect(warm?.identity == left.identity, "a 2 px advantage should not take it away")
    }

    @Test("stickiness yields to a target the cursor is actually inside")
    func stickinessIsNotAJail() {
        // Being inside a target is not a marginal preference, and no amount of
        // hysteresis should hold the highlight on a neighbour you have left.
        let left = target(kAXButtonRole, CGRect(x: 0, y: 0, width: 50, height: 20))
        let right = target(kAXButtonRole, CGRect(x: 60, y: 0, width: 50, height: 20))
        for at in [CGPoint(x: 62, y: 10), CGPoint(x: 100, y: 10)] {
            let chosen = chooseTarget(
                from: [left, right], at: at, velocity: still, holding: left.identity,
                tuning: tuning)
            #expect(chosen?.identity == right.identity, "at \(at.x)")
        }
    }

    @Test("a target in the direction of travel beats an equidistant one behind")
    func headingBreaksTheTie() {
        let ahead = target(kAXButtonRole, CGRect(x: 140, y: 95, width: 20, height: 10))
        let behind = target(kAXButtonRole, CGRect(x: 40, y: 95, width: 20, height: 10))
        let at = CGPoint(x: 100, y: 100)
        // Moving right, fast enough for heading to be considered at all.
        let moving = CGVector(dx: 600, dy: 0)
        let chosen = chooseTarget(
            from: [ahead, behind], at: at, velocity: moving, holding: nil, tuning: tuning)
        #expect(chosen?.identity == ahead.identity)
    }

    @Test("a word beats the paragraph it sits in")
    func wordBeatsParagraph() {
        // Sampled from TextEdit: the text area under the cursor, and the word box
        // the text APIs resolved inside it.
        let at = CGPoint(x: 297, y: 197)
        let area = target(kAXTextAreaRole, CGRect(x: 237, y: 187, width: 656, height: 384))
        let word = target(
            kAXTextAreaRole, CGRect(x: 273, y: 187, width: 33, height: 13), kind: .textRange)
        let chosen = chooseTarget(
            from: [area, word], at: at, velocity: still, holding: nil, tuning: tuning)
        #expect(chosen?.kind == .textRange)
    }

    @Test("a document-sized text area is not itself a target, but a field is")
    func proseIsAimedInside() {
        // The same 656x384 TextEdit area, this time with no word resolved -- the
        // cursor is in the margin past the end of the text. It used to win by
        // default and outline the entire document view.
        let area = target(kAXTextAreaRole, CGRect(x: 237, y: 187, width: 656, height: 384))
        #expect(
            chooseTarget(
                from: [area], at: CGPoint(x: 297, y: 400), velocity: still, holding: nil,
                tuning: tuning) == nil)

        // A search box is short however wide it is, and clicking an empty one to
        // focus it is a real aim.
        let field = target(axSearchFieldRole, CGRect(x: 237, y: 187, width: 320, height: 24))
        #expect(
            chooseTarget(
                from: [field], at: CGPoint(x: 297, y: 199), velocity: still, holding: nil,
                tuning: tuning)?.role == axSearchFieldRole)
    }
}

@Suite("where a target pulls the cursor")
struct AnchorTests {
    @Test("a small target pulls to its centre, so it feels magnetic")
    func smallPullsToCentre() {
        let button = target(kAXButtonRole, CGRect(x: 100, y: 100, width: 24, height: 24))
        let anchor = button.anchor(from: CGPoint(x: 90, y: 90), smallerThan: 72)
        #expect(anchor == CGPoint(x: 112, y: 112))
    }

    @Test("a large target pulls only to its nearest edge, leaving the inside free")
    func largePullsToEdge() {
        let panel = target(kAXGroupRole, CGRect(x: 100, y: 100, width: 600, height: 400))
        // Outside, to the left: pulled onto the edge, not to the middle of a panel.
        #expect(panel.anchor(from: CGPoint(x: 40, y: 200), smallerThan: 72) == CGPoint(x: 100, y: 200))
        // Already inside: the anchor is where the cursor is, so the pull is zero
        // and the hand has full freedom within a large target.
        let inside = CGPoint(x: 300, y: 250)
        #expect(panel.anchor(from: inside, smallerThan: 72) == inside)
    }
}
