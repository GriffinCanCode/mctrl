// Exactly one helper, enforced.
//
// The whole design rests on there being a single writer to the cursor. Two
// helpers running at once breaks that quietly rather than loudly: the newcomer
// unlinks and rebinds the socket, so it receives every frame and looks entirely
// healthy, while the incumbent sits at its last goal with a live motion thread, a
// live probe, and a live overlay. Measured with two deliberately started: both
// bound, both integrating, two highlight windows, and a stale process one tick
// away from yanking the cursor back to where it last thought it should be. That
// is precisely the cursor-fighting-itself this process exists to eliminate.
//
// A socket cannot express the constraint, so a lock file does. `flock` rather than
// a pid file because the kernel releases it when the holder dies, however it dies:
// there is no stale lock to reason about after a crash, a kill -9, or a panic.
//
// The newcomer wins. An orphan whose parent died is invisible to the user and
// would otherwise block every future launch, so the incumbent is asked to leave
// and the lock is taken once it does. Restarting the app is the common case and it
// must simply work.

import Darwin
import Foundation

/// Holds the exclusive claim to be *the* helper. Keep it alive for as long as the
/// process runs -- releasing it, or letting it deinit, gives up the claim.
final class Solitary {
    private let path: String
    private var descriptor: Int32 = -1

    init(path: String) {
        self.path = path
    }

    /// Take the claim, evicting an incumbent if there is one.
    ///
    /// - Returns: nil on success, or a sentence explaining who would not leave.
    func claim(evictionGrace: Double = 3.0) -> String? {
        let directory = (path as NSString).deletingLastPathComponent
        try? FileManager.default.createDirectory(
            atPath: directory, withIntermediateDirectories: true)

        descriptor = open(path, O_RDWR | O_CREAT, 0o644)
        guard descriptor >= 0 else {
            // Not fatal on its own: refusing to run because a lock file could not
            // be created would be worse than running without the guarantee.
            return nil
        }

        if flock(descriptor, LOCK_EX | LOCK_NB) == 0 {
            record()
            return nil
        }

        let incumbent = readPid()
        if let incumbent, incumbent != getpid() {
            kill(incumbent, SIGTERM)
        }

        let deadline = Date().addingTimeInterval(evictionGrace)
        while Date() < deadline {
            usleep(50_000)
            if flock(descriptor, LOCK_EX | LOCK_NB) == 0 {
                record()
                return nil
            }
        }

        close(descriptor)
        descriptor = -1
        let who = incumbent.map { "pid \($0)" } ?? "another process"
        return "\(who) is already running as the helper and did not exit when asked"
    }

    /// The pid is not the lock -- `flock` is -- and exists only so a newcomer knows
    /// whom to ask to leave.
    private func record() {
        ftruncate(descriptor, 0)
        lseek(descriptor, 0, SEEK_SET)
        let line = "\(getpid())\n"
        _ = line.withCString { write(descriptor, $0, strlen($0)) }
    }

    private func readPid() -> pid_t? {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8),
            let value = Int32(text.trimmingCharacters(in: .whitespacesAndNewlines))
        else { return nil }
        return value
    }

    func release() {
        guard descriptor >= 0 else { return }
        flock(descriptor, LOCK_UN)
        close(descriptor)
        descriptor = -1
    }
}

/// Exit when the process that started us is gone.
///
/// A helper outliving its parent is the way orphans are made, and an orphan still
/// holds Accessibility permission and a motion thread. Nothing sends it a signal:
/// a parent that crashes or is force-quit does not get to run cleanup, so noticing
/// has to be this side's job. Being reparented to launchd is the signal.
func watchForOrphaning(every seconds: Double = 1.0, onOrphan: @escaping () -> Void)
    -> DispatchSourceTimer
{
    let timer = DispatchSource.makeTimerSource(queue: .global(qos: .utility))
    timer.schedule(deadline: .now() + seconds, repeating: seconds)
    timer.setEventHandler {
        // Launched from a shell rather than by the app, the parent is that shell
        // and this stays false for as long as it lives.
        if getppid() == 1 { onOrphan() }
    }
    timer.resume()
    return timer
}
