// The only thing in the system that moves the cursor.
//
// Single writer, deliberately. The old arrangement had the gesture loop posting a
// move whenever a frame arrived, which is where "overlaps with itself" came from:
// a gaze warp and a hand delta could be posted in the same millisecond, each
// computed from a cursor position the other had already invalidated, and the
// result was a cursor that fought itself. Here every position comes from one
// thread on one clock, and nothing else is allowed to post motion.
//
// Two details carry over from the Python this replaces, both load-bearing.
//
// *Tagging.* Every event is stamped with the same marker the mode watcher looks
// for, so the app does not mistake its own output for a human reaching for the
// trackpad and suspend itself the instant it starts working.
//
// *Click chaining.* macOS decides what a double click is from the click-state
// field on the event, not from two clicks arriving close together. Real trackpads
// set it, so this does too, which is what makes two quick pinches open a folder.

import CoreGraphics
import Foundation

enum MouseButton {
    case left
    case right

    var down: CGEventType { self == .left ? .leftMouseDown : .rightMouseDown }
    var up: CGEventType { self == .left ? .leftMouseUp : .rightMouseUp }
    var dragged: CGEventType { self == .left ? .leftMouseDragged : .rightMouseDragged }
    var index: CGMouseButton { self == .left ? .left : .right }
}

/// Shared with `control/events.py`. Changing it here without changing it there
/// makes the app suspend itself on its own cursor motion.
let eventMarker: Int64 = 0x4D49_4E44  // "MIND"

final class Cursor {
    private let source: CGEventSource?
    private var doubleClickMs: Double

    private(set) var held: MouseButton?
    private var lastClickAt: Double = 0
    private var lastClickPoint: CGPoint = .zero
    private var clickRun: Int = 0

    private(set) var desktop: CGRect = .zero
    private(set) var mainDisplay: CGRect = .zero

    init(doubleClickMs: Double) {
        self.doubleClickMs = doubleClickMs
        source = CGEventSource(stateID: .hidSystemState)
        source?.userData = eventMarker
        refreshDisplays()
    }

    func apply(_ tuning: Tuning) { doubleClickMs = tuning.doubleClickMs }

    /// Re-read display geometry, for a monitor being plugged in or unplugged.
    func refreshDisplays() {
        mainDisplay = CGDisplayBounds(CGMainDisplayID())

        var count: UInt32 = 0
        CGGetActiveDisplayList(0, nil, &count)
        var ids = [CGDirectDisplayID](repeating: 0, count: Int(count))
        CGGetActiveDisplayList(count, &ids, &count)

        // Cursor coordinates are global across displays, so clamping to the main
        // screen would trap the pointer on a multi-monitor desk. Gaze is the only
        // thing confined to one screen, because that is the only screen it was
        // ever calibrated against.
        let boxes = ids.prefix(Int(count)).map { CGDisplayBounds($0) }
        desktop = boxes.isEmpty ? mainDisplay : boxes.dropFirst().reduce(boxes[0]) { $0.union($1) }
    }

    func clamp(_ point: CGPoint) -> CGPoint {
        CGPoint(
            x: min(max(point.x, desktop.minX), desktop.maxX - 1),
            y: min(max(point.y, desktop.minY), desktop.maxY - 1))
    }

    /// Where the cursor actually is. Read from the system rather than remembered,
    /// so nudging the real mouse does not leave this process's idea of the cursor
    /// somewhere else.
    func location() -> CGPoint {
        CGEvent(source: nil)?.location ?? .zero
    }

    private func post(_ event: CGEvent?) {
        guard let event else { return }
        event.setIntegerValueField(.eventSourceUserData, value: eventMarker)
        event.post(tap: .cghidEventTap)
    }

    /// Move to an absolute point. While a button is down the motion must go out as
    /// a drag, or the application underneath sees the cursor teleport without ever
    /// having been dragged.
    func move(to point: CGPoint) {
        let type = held?.dragged ?? .mouseMoved
        let button = held?.index ?? .left
        post(CGEvent(mouseEventSource: source, mouseType: type, mouseCursorPosition: point,
                     mouseButton: button))
    }

    func click(_ button: MouseButton, at point: CGPoint) {
        let now = CFAbsoluteTimeGetCurrent()
        let near = abs(point.x - lastClickPoint.x) < 6 && abs(point.y - lastClickPoint.y) < 6
        let inTime = (now - lastClickAt) * 1000.0 < doubleClickMs
        clickRun = (near && inTime) ? clickRun + 1 : 1
        lastClickAt = now
        lastClickPoint = point

        for type in [button.down, button.up] {
            let event = CGEvent(
                mouseEventSource: source, mouseType: type, mouseCursorPosition: point,
                mouseButton: button.index)
            event?.setIntegerValueField(.mouseEventClickState, value: Int64(min(clickRun, 3)))
            post(event)
        }
    }

    func press(_ button: MouseButton, at point: CGPoint) {
        guard held == nil else { return }
        post(CGEvent(mouseEventSource: source, mouseType: button.down, mouseCursorPosition: point,
                     mouseButton: button.index))
        held = button
    }

    /// Let go of a held button. Safe to call when nothing is held.
    func release(at point: CGPoint) {
        guard let button = held else { return }
        held = nil
        post(CGEvent(mouseEventSource: source, mouseType: button.up, mouseCursorPosition: point,
                     mouseButton: button.index))
    }

    /// Scroll by a pixel delta, following the hand as if it held the page. Pulling
    /// down should drag the content down, which in wheel terms is positive, and the
    /// camera's y axis grows downward -- hence the negation.
    func scroll(dx: Double, dy: Double) {
        post(
            CGEvent(
                scrollWheelEvent2Source: source, units: .pixel, wheelCount: 2,
                wheel1: Int32(-dy), wheel2: Int32(dx), wheel3: 0))
    }
}
