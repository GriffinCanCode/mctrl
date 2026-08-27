// Assembling the helper.
//
// A separate process rather than a library loaded into Python, for three reasons
// that all showed up in measurement or in practice.
//
// *Permissions survive.* macOS attaches Accessibility permission to a specific
// binary. Granted to a virtualenv's interpreter it is lost whenever that
// virtualenv is rebuilt; granted to this helper it stays granted.
//
// *A wedged application cannot freeze the cursor.* Accessibility calls are
// synchronous IPC into other processes' main threads, and applications do hang.
// Here that costs a skipped probe on a thread nobody is waiting for.
//
// *Scheduling is honest.* The motion thread asks for the user-interactive band and
// gets it, instead of contending with MediaPipe inference and the GIL in a process
// whose main thread belongs to a menu bar.
//
// Python keeps everything it is good at -- cameras, landmarks, the gesture state
// machine, and the thresholds in config.toml that were fitted to real recordings.
// This side owns everything that touches the machine.
//
// The components are file-scoped rather than passed around because there is
// exactly one of each in a process, and the signal handlers need to reach them
// from a C function pointer that cannot capture anything.

import AppKit
import ApplicationServices
import Foundation

private var tuning = Tuning()
private var tuningPath: String?
private var cursor: Cursor!
private var overlay: OverlayPresenter!
private var probe: TargetProbe!
private var motion: MotionCore!
private var transport: Transport?
private var solitary: Solitary?
private var orphanWatch: DispatchSourceTimer?

private func note(_ message: String) {
    FileHandle.standardError.write(Data("[bridge] \(message)\n".utf8))
}

private func argument(_ name: String) -> String? {
    let arguments = CommandLine.arguments
    guard let index = arguments.firstIndex(of: name), index + 1 < arguments.count else {
        return nil
    }
    return arguments[index + 1]
}

/// Reload is a menu bar action on the Python side, so it arrives as a frame rather
/// than a signal, and has to reach every component that holds tuning.
private func reload() {
    tuning = Tuning.load(tuningPath)
    cursor.apply(tuning)
    probe.apply(tuning)
    motion.apply(tuning)
    note("tuning reloaded")
}

/// Stop touching the machine and go. Safe at any point in start-up, because the
/// signal handlers are installed before anything they tear down exists.
private func shutDown() {
    orphanWatch?.cancel()
    transport?.stop()
    motion?.stop()
    probe?.stop()
    overlay?.detach()
    // Released last, so no newcomer can start driving the cursor until this one has
    // genuinely stopped touching it.
    solitary?.release()
    guard let app = NSApp else {
        // Signalled before the run loop existed. `terminate` would have nothing to
        // deliver to, and returning would leave the process alive and holding the
        // claim -- which for an eviction means the newcomer waits out its grace
        // period and refuses to start.
        exit(0)
    }
    app.terminate(nil)
}

/// Report what is under the cursor and exit. The only way, from outside, to see
/// why something will or will not light up.
private func inspectAndExit() -> Never {
    guard AXIsProcessTrusted() else {
        note("no Accessibility permission, so nothing can be inspected")
        exit(2)
    }
    let at = cursor.location()
    let found = probe.inspect(at: at)
    let chosen = chooseTarget(
        from: found, at: at, velocity: CGVector(dx: 0, dy: 0), holding: nil, tuning: tuning)
    print(String(format: "cursor at %.0f,%.0f -- %d candidate(s)", at.x, at.y, found.count))
    let ordered = found.sorted {
        distance(from: at, to: $0.rect) < distance(from: at, to: $1.rect)
    }
    for target in ordered {
        print(
            String(
                format: "%@ %-16@ %-5@ gap %5.1f px  %4.0fx%-4.0f at %5.0f,%-5.0f  %@",
                target.identity == chosen?.identity ? "->" : "  ",
                target.role as NSString,
                (target.kind == .textRange ? "word" : "elem") as NSString,
                distance(from: at, to: target.rect), target.rect.width, target.rect.height,
                target.rect.minX, target.rect.minY, target.label as NSString))
    }
    if chosen == nil { print("nothing within the \(Int(tuning.snapRadius)) px snap radius") }
    exit(0)
}

private final class BridgeDelegate: NSObject, NSApplicationDelegate {
    let socketPath: String

    init(socketPath: String) {
        self.socketPath = socketPath
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        overlay.attach()
        probe.start()
        motion.start()
        note("listening on \(socketPath)")
        // Echo the knobs that decide the feel. A tuning file that failed to decode
        // falls back to defaults silently, and the only way to notice from outside
        // is to see the values it is actually running with.
        note(
            String(
                format: "feel: motion %.3fs, snap %.0fpx at %.2f, probe %.0fms, overlay %@",
                tuning.motionTimeConstant, tuning.snapRadius, tuning.snapStrength,
                tuning.probeIntervalMs, tuning.overlayEnabled ? "on" : "off"))

        // A monitor arriving or leaving changes both the clamp bounds and the
        // overlay's coverage, and neither is worth re-deriving every frame.
        NotificationCenter.default.addObserver(
            forName: NSApplication.didChangeScreenParametersNotification,
            object: nil, queue: .main
        ) { _ in
            motion.displaysChanged()
            overlay.detach()
            overlay.attach()
        }
    }

    func applicationWillTerminate(_ notification: Notification) {
        transport?.stop()
        motion.stop()
        probe.stop()
    }
}

public func runBridge() -> Never {
    // First, before anything that could take time. Eviction depends on an incumbent
    // answering SIGTERM, so a helper that has not yet installed these is one that
    // gets killed outright by a newcomer instead of standing down -- a race whose
    // width is however long start-up happens to take.
    signal(SIGTERM) { _ in DispatchQueue.main.async { shutDown() } }
    signal(SIGINT) { _ in DispatchQueue.main.async { shutDown() } }
    signal(SIGPIPE, SIG_IGN)

    let home = FileManager.default.homeDirectoryForCurrentUser.path
    let socketPath = argument("--socket") ?? "\(home)/.local/state/mindcontrol/bridge.sock"
    tuningPath = argument("--tuning")
    tuning = Tuning.load(tuningPath)

    cursor = Cursor(doubleClickMs: tuning.doubleClickMs)
    overlay = OverlayPresenter()
    probe = TargetProbe(tuning: tuning)
    motion = MotionCore(cursor: cursor, probe: probe, overlay: overlay, tuning: tuning)

    // Before `--inspect`, which is read-only and must keep working while a helper
    // is running -- diagnosing a live cursor is most of the point of it.
    if CommandLine.arguments.contains("--inspect") { inspectAndExit() }
    motion.tracing = CommandLine.arguments.contains("--trace")

    let claim = Solitary(path: argument("--lock") ?? socketPath + ".lock")
    if let refusal = claim.claim() {
        note("not starting: \(refusal)")
        exit(3)
    }
    solitary = claim
    orphanWatch = watchForOrphaning { DispatchQueue.main.async { shutDown() } }

    let listener = Transport(path: socketPath) { frame in
        switch frame.intent {
        case .reloadConfig: DispatchQueue.main.async { reload() }
        case .shutdown: DispatchQueue.main.async { shutDown() }
        default: motion.handle(frame)
        }
    }
    do {
        try listener.start()
        transport = listener
    } catch {
        note("could not listen on \(socketPath): \(error)")
        exit(1)
    }

    if !AXIsProcessTrusted() {
        note(
            """
            no Accessibility permission yet. Motion and clicks work; highlighting \
            and snapping do not, because nothing can be asked what is on screen. \
            Grant it to this binary in System Settings > Privacy & Security > \
            Accessibility.
            """)
    }

    // An accessory app: no dock icon, no menu, but a real run loop, which the
    // overlay window and Core Animation both require.
    let app = NSApplication.shared
    app.setActivationPolicy(.accessory)
    let delegate = BridgeDelegate(socketPath: socketPath)
    app.delegate = delegate
    app.run()
    // NSApplication.run does not return, but the compiler wants to be told.
    exit(0)
}
