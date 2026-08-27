// The highlight.
//
// One layer, retargeted -- never a second layer drawn on top of the first. That
// is the whole reason the highlight cannot overlap itself: there is exactly one
// of it, and pointing at something new moves it rather than adding to it.
//
// The glide is the compositor's work, not ours. Setting a new frame inside a
// transaction hands Core Animation the interpolation, so the highlight travels at
// display rate even though new targets only arrive as fast as the probe can find
// them -- roughly every sixteen milliseconds, and stuttering when an application
// is slow to answer. Animating it ourselves would put that stutter on screen.
//
// The window is click-through, which is not a nicety: an overlay that accepted
// mouse events would swallow the very clicks this project exists to deliver.

import AppKit
import CoreGraphics

/// Convert from the screen space accessibility and CGEvent use -- origin at the
/// top left of the primary display, y downward -- into AppKit's, which puts the
/// origin at the bottom left and counts upward. Everything else in this process
/// works in the former; only the window server insists on the latter.
private func toAppKit(_ rect: CGRect) -> CGRect {
    // The primary screen is the one AppKit places at the origin, and its top edge
    // is where the two coordinate systems meet.
    let flipAxis = (NSScreen.screens.first { $0.frame.origin == .zero } ?? NSScreen.main)?
        .frame.maxY ?? rect.maxY
    return CGRect(
        x: rect.minX, y: flipAxis - rect.maxY, width: rect.width, height: rect.height)
}

final class OverlayPresenter {
    private var window: NSPanel?
    private var highlight: CALayer?

    // Touched only by the motion thread, to decide whether a hop to main is worth
    // making. At a hundred and twenty ticks a second, almost all of them are not.
    private var lastIdentity: Int?
    private var lastRect: CGRect = .zero
    private var visible = false

    /// Build the window. Main thread only, and before any target can arrive.
    func attach() {
        let frame = NSScreen.screens.reduce(CGRect.zero) { $0.union($1.frame) }
        let panel = NSPanel(
            contentRect: frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false)

        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = false
        // Above ordinary windows, and never the reason a click goes missing.
        panel.level = .screenSaver
        panel.ignoresMouseEvents = true
        panel.collectionBehavior = [
            .canJoinAllSpaces, .stationary, .fullScreenAuxiliary, .ignoresCycle,
        ]
        // A panel that could become key would steal focus from whatever is being
        // pointed at, which would change what the click then does.
        panel.becomesKeyOnlyIfNeeded = true
        panel.isFloatingPanel = true

        let content = NSView(frame: CGRect(origin: .zero, size: frame.size))
        content.wantsLayer = true
        panel.contentView = content

        let layer = CALayer()
        layer.opacity = 0
        layer.borderColor = CGColor(red: 0.36, green: 0.72, blue: 1.0, alpha: 0.95)
        layer.backgroundColor = CGColor(red: 0.36, green: 0.72, blue: 1.0, alpha: 0.14)
        content.layer?.addSublayer(layer)

        panel.orderFrontRegardless()
        window = panel
        highlight = layer
    }

    func detach() {
        window?.orderOut(nil)
        window = nil
        highlight = nil
    }

    /// Called every tick from the motion thread. Cheap when nothing has changed.
    func show(_ target: Target?, enabled: Bool, tuning: Tuning) {
        guard enabled, let target else {
            if visible {
                visible = false
                lastIdentity = nil
                DispatchQueue.main.async { [weak self] in self?.hide(tuning) }
            }
            return
        }

        // A target that has not moved needs no work. Rectangles jitter by a
        // fraction of a pixel as applications relayout, so a small movement does
        // not count as one.
        let same = target.identity == lastIdentity
        if same, abs(target.rect.minX - lastRect.minX) < 0.5,
            abs(target.rect.minY - lastRect.minY) < 0.5,
            abs(target.rect.width - lastRect.width) < 0.5,
            abs(target.rect.height - lastRect.height) < 0.5
        {
            return
        }

        let appearing = !visible
        lastIdentity = target.identity
        lastRect = target.rect
        visible = true

        let frame = toAppKit(target.rect)
        DispatchQueue.main.async { [weak self] in
            self?.place(frame, appearing: appearing, tuning: tuning)
        }
    }

    private func place(_ frame: CGRect, appearing: Bool, tuning: Tuning) {
        guard let highlight else { return }
        CATransaction.begin()
        // Appearing somewhere new should not be a flight across the screen from
        // wherever the highlight last was; it should simply be there, and fade in.
        CATransaction.setAnimationDuration(appearing ? 0 : tuning.overlayGlideS)
        CATransaction.setAnimationTimingFunction(CAMediaTimingFunction(name: .easeOut))
        highlight.borderWidth = tuning.overlayBorderWidth
        highlight.cornerRadius = tuning.overlayCornerRadius
        highlight.borderColor = tuning.borderCGColor
        highlight.backgroundColor = tuning.fillCGColor
        highlight.frame = frame
        CATransaction.commit()

        if appearing {
            CATransaction.begin()
            CATransaction.setAnimationDuration(tuning.overlayGlideS)
            highlight.opacity = 1
            CATransaction.commit()
        }
    }

    private func hide(_ tuning: Tuning) {
        guard let highlight else { return }
        CATransaction.begin()
        CATransaction.setAnimationDuration(tuning.overlayGlideS)
        highlight.opacity = 0
        CATransaction.commit()
    }
}
