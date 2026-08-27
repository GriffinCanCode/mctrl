// What the cursor can be pulled to, and how the nearest one is chosen.
//
// Coordinates throughout are the screen space accessibility and CGEvent share:
// origin at the top left of the primary display, y growing downward. Only the
// overlay converts out of it, because AppKit disagrees about which way is up.
//
// Choosing a target is not the same as finding the closest rectangle. Three
// corrections turn proximity into intent:
//
// *Role* -- a hit test lands on whatever is deepest at the point, which is often
// the group containing the button rather than the button. A button outranks its
// container regardless of which one the point technically fell in.
//
// *Heading* -- where the hand is travelling is better evidence than where it
// currently is. A target ahead of the cursor beats an equidistant one behind it.
//
// *Stickiness* -- the target already held keeps a bonus. Without it, a cursor
// resting on the boundary between two buttons alternates between them every
// probe, and the highlight strobes. This is the pinch detector's hysteresis
// applied to space instead of time.

import ApplicationServices
import CoreGraphics

enum TargetKind {
    case element
    /// A word or glyph run inside a text element, resolved through the text APIs.
    case textRange
}

struct Target {
    var rect: CGRect
    var role: String
    var label: String
    var kind: TargetKind
    /// Stable enough to recognise the same target across probes, which is what
    /// stickiness and the overlay's glide both need.
    var identity: Int

    /// Where the cursor is pulled to from `point`.
    ///
    /// A small target pulls to its centre, which is what makes a checkbox feel
    /// magnetic. A large one pulls only to its nearest edge: yanking the cursor
    /// to the middle of a scroll view would be worse than not helping at all.
    func anchor(from point: CGPoint, smallerThan smallSide: Double) -> CGPoint {
        if rect.width <= smallSide && rect.height <= smallSide {
            return CGPoint(x: rect.midX, y: rect.midY)
        }
        return CGPoint(
            x: min(max(point.x, rect.minX), rect.maxX),
            y: min(max(point.y, rect.minY), rect.maxY))
    }
}

/// One probe's worth of findings, published as a value so the motion core can
/// read it without holding a lock while it integrates.
struct TargetSnapshot {
    var targets: [Target] = []
    var takenAt: Double = 0
}

// ------------------------------------------------------------------ roles

/// Roles worth aiming at, ranked. A higher number wins a tie against a lower one
/// even when the lower one is closer, which is what stops every snap landing on
/// the enclosing group.
// Roles that exist on screen but have no exported constant in
// ApplicationServices, so they are spelled out. Both matter: links are the single
// most common thing anyone aims at in a browser, and a search field is a text
// field that reports a different role. Window buttons are conspicuously absent
// here because a close or minimise button reports role `AXButton` and distinguishes
// itself by *subrole* -- they are already ranked, one line below.
let axLinkRole = "AXLink"
let axSearchFieldRole = "AXSearchField"

/// Ranked by how deliberate an aim at one is. Above `specificityLine` is something
/// to aim at; below it is scenery to look inside.
///
/// Containers are listed rather than left to the unranked default, because the
/// default is decided by size and a small window or a short menu would otherwise
/// pass for a control.
let targetPriority: [String: Int] = [
    // --- controls
    kAXButtonRole: 100,
    kAXMenuItemRole: 100,
    kAXMenuBarItemRole: 100,
    kAXCheckBoxRole: 100,
    kAXRadioButtonRole: 100,
    kAXDockItemRole: 100,
    axLinkRole: 100,
    kAXMenuButtonRole: 95,
    kAXPopUpButtonRole: 95,
    kAXComboBoxRole: 95,
    kAXDisclosureTriangleRole: 95,
    kAXColorWellRole: 90,
    kAXIncrementorRole: 90,
    kAXSliderRole: 85,

    // --- text
    kAXTextFieldRole: 80,
    axSearchFieldRole: 80,
    kAXDateFieldRole: 80,
    kAXTimeFieldRole: 80,
    kAXTextAreaRole: 70,
    kAXHeadingRole: 55,
    kAXStaticTextRole: 50,

    // --- things dragged rather than clicked
    kAXValueIndicatorRole: 70,  // a scrollbar thumb or slider knob
    kAXHandleRole: 65,
    kAXSplitterRole: 60,
    kAXScrollBarRole: 60,
    kAXGrowAreaRole: 60,

    // --- structure that is still worth aiming at
    kAXTabGroupRole: 60,
    kAXCellRole: 65,
    kAXRadioGroupRole: 55,
    kAXRowRole: 45,
    kAXImageRole: 40,

    // --- scenery
    kAXOutlineRole: 30,
    kAXTableRole: 30,
    kAXGridRole: 30,
    kAXBrowserRole: 25,
    kAXListRole: 25,
    kAXPopoverRole: 22,
    kAXSheetRole: 22,
    kAXToolbarRole: 20,
    kAXRulerRole: 20,
    kAXScrollAreaRole: 15,
    kAXColumnRole: 15,
    kAXLayoutAreaRole: 12,
    kAXSplitGroupRole: 12,
    kAXWindowRole: 11,
    kAXMenuRole: 11,
    kAXMenuBarRole: 11,
    kAXGroupRole: 10,
    kAXApplicationRole: 5,
    kAXUnknownRole: 5,
]

/// Ranked at or above this is a target; below it is scenery.
let specificityLine = 40

/// Roles that can be subdivided into words, for aiming inside prose rather than
/// at the paragraph containing it.
let textBearingRoles: Set<String> = [
    kAXTextAreaRole, kAXTextFieldRole, kAXStaticTextRole, axSearchFieldRole,
]

/// Unranked roles score low, but not zero: low enough that anything named above
/// beats them, high enough to still be picked over nothing at all.
func priority(of role: String) -> Int { targetPriority[role] ?? 15 }

/// Whether a role is something to aim at, or merely something to look inside.
///
/// Used by the probe to decide whether to descend past a vague hit. Selection asks
/// `isAimable` instead, which is the same question plus a fallback for roles the
/// table has never been shown.
func isSpecific(role: String) -> Bool { priority(of: role) >= specificityLine }

/// Whether a candidate deserves the highlight at all.
///
/// Everything the table ranks below the specificity line -- groups, toolbars,
/// tables, outlines -- is scenery, and highlighting scenery is worse than
/// highlighting nothing: a panel-sized outline obscures what is behind it and
/// pulls nowhere, because a large target's anchor clamps to wherever the cursor
/// already was.
///
/// An unfamiliar role is judged by shape instead of being refused outright. No
/// table can name every role that ships -- web content and Electron invent them
/// freely -- and refusing all of them silently killed the entire Dock the first
/// time this was tried. So something small enough to be a control is taken at face
/// value, and something the size of a panel is not. Getting this wrong in the
/// refusing direction is invisible from outside the process, which is what
/// `--inspect` is for.
func isAimable(_ target: Target, controlSide: Double) -> Bool {
    if targetPriority[target.role] != nil { return isSpecific(role: target.role) }
    return target.rect.width <= controlSide * 2 && target.rect.height <= controlSide * 2
}

// --------------------------------------------------------------- selection

/// Distance from a point to a rectangle: zero inside, Euclidean to the nearest
/// edge outside.
func distance(from point: CGPoint, to rect: CGRect) -> Double {
    let dx = max(rect.minX - point.x, 0, point.x - rect.maxX)
    let dy = max(rect.minY - point.y, 0, point.y - rect.maxY)
    return (dx * dx + dy * dy).squareRoot()
}

/// Pick the target the user most likely means, or nothing if none is close enough.
///
/// Cost, not score: lower wins. Everything below either scales the raw distance
/// down (making a target more attractive) or leaves it alone.
func chooseTarget(
    from targets: [Target],
    at point: CGPoint,
    velocity: CGVector,
    holding current: Int?,
    tuning: Tuning
) -> Target? {
    guard !targets.isEmpty else { return nil }

    let speed = (velocity.dx * velocity.dx + velocity.dy * velocity.dy).squareRoot()
    var best: Target?
    var bestCost = Double.infinity

    for target in targets {
        // Scenery is not a target. The probe hands over whatever the hit test
        // landed on, which is an enclosing group about two thirds of the time, and
        // an accepted container draws an outline the size of the panel while
        // pulling nowhere at all.
        guard isAimable(target, controlSide: tuning.smallTargetPixels) else { continue }

        // Prose is aimed *inside*, not at. A wrapped text area is as tall as the
        // document view, so its outline covers the text it is supposed to be
        // pointing at, and it cannot pull -- a large target's anchor clamps to
        // wherever the cursor already was. Height rather than area is the honest
        // discriminator: a single-line field stays short however wide it gets, and
        // clicking an empty search box to focus it is a real aim worth keeping.
        if target.kind == .element, textBearingRoles.contains(target.role),
            target.rect.height > tuning.smallTargetPixels
        {
            continue
        }

        let gap = distance(from: point, to: target.rect)
        guard gap <= tuning.snapRadius else { continue }

        // The floor is what makes role matter at all. Scaling a raw distance by
        // priority leaves anything the cursor is directly inside at zero cost, and
        // zero divided by any priority is still zero -- so the vaguest hit
        // underfoot would beat a button a few pixels away, which is backwards.
        // A table row the cursor rests on should lose to the button inside it.
        // Expressed as a fraction of the radius so that widening the radius does
        // not quietly change which of two candidates wins.
        var cost = gap + tuning.snapRadius * 0.125

        // Favour what the hand is heading toward. Only meaningful once the cursor
        // is actually moving; a stationary cursor has no heading to speak of.
        if speed > 40 {
            let anchor = target.anchor(from: point, smallerThan: tuning.smallTargetPixels)
            let toward = CGVector(dx: anchor.x - point.x, dy: anchor.y - point.y)
            let reach = (toward.dx * toward.dx + toward.dy * toward.dy).squareRoot()
            if reach > 1 {
                let alignment =
                    (toward.dx * velocity.dx + toward.dy * velocity.dy) / (reach * speed)
                cost *= 1 - tuning.snapHeadingWeight * max(0, alignment)
            }
        }

        // A specific control beats the container it sits inside, and a word beats
        // the paragraph around it.
        cost /= Double(priority(of: target.role)) / 50.0
        if target.kind == .textRange { cost /= 1.5 }

        // Among equals, the smaller target is the more deliberate choice.
        let area = max(target.rect.width * target.rect.height, 1)
        cost *= 1 + min(area / 4_000_000, 0.5)

        if let current, target.identity == current { cost /= tuning.snapStickiness }

        if cost < bestCost {
            bestCost = cost
            best = target
        }
    }
    return best
}
