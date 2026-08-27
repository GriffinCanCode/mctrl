// Does a frame Python packed decode to what Python meant?
//
// The two ends of this socket are written in different languages against a byte
// layout neither can check against the other at compile time, and a mismatch is
// silent: a wrong offset reads a plausible number, so the cursor moves the wrong
// distance rather than anything raising. The fixtures below are the exact bytes
// `struct.Struct("<IHHIIdddII")` produces in `control/bridge.py`, so a change to
// either side has to break this to get past.

import CoreGraphics
import Foundation
import Testing

@testable import BridgeCore

/// Build a frame the way the Python client does, to confirm the reader agrees.
private func pack(
    magic: UInt32 = 0x4D49_4E44,
    version: UInt16 = 1,
    intent: UInt16,
    sequence: UInt32 = 1,
    flags: UInt32 = 0,
    a: Double = 0,
    b: Double = 0,
    sentAt: Double = 0,
    button: UInt32 = 0
) -> [UInt8] {
    var bytes = [UInt8]()
    func append<T>(_ value: T) {
        withUnsafeBytes(of: value) { bytes.append(contentsOf: $0) }
    }
    append(magic.littleEndian)
    append(version.littleEndian)
    append(intent.littleEndian)
    append(sequence.littleEndian)
    append(flags.littleEndian)
    append(a.bitPattern.littleEndian)
    append(b.bitPattern.littleEndian)
    append(sentAt.bitPattern.littleEndian)
    append(button.littleEndian)
    append(UInt32(0).littleEndian)  // padding to 48
    return bytes
}

private func decode(_ bytes: [UInt8]) -> Frame? {
    bytes.withUnsafeBytes { Frame($0) }
}

@Suite("the wire format")
struct ProtocolTests {
    @Test("the frame is the 48 bytes the Python struct produces")
    func frameSize() {
        #expect(Frame.byteCount == 48)
        #expect(pack(intent: 1).count == 48)
    }

    @Test("a pointer delta survives the trip intact")
    func moveByRoundTrips() {
        let frame = decode(pack(intent: 1, sequence: 42, a: -12.5, b: 7.25, sentAt: 1234.5))
        #expect(frame?.intent == .moveBy)
        #expect(frame?.sequence == 42)
        #expect(frame?.a == -12.5)
        #expect(frame?.b == 7.25)
        #expect(frame?.sentAt == 1234.5)
    }

    @Test("every intent Python can send is one this side knows")
    func intentsAgree() {
        // The numbers in control/bridge.py, in order.
        let expected: [(UInt16, Intent)] = [
            (1, .moveBy), (2, .warpToFraction), (3, .click), (4, .press), (5, .release),
            (6, .scroll), (7, .setMode), (8, .releaseAll), (9, .reloadConfig), (10, .shutdown),
        ]
        for (raw, intent) in expected {
            #expect(decode(pack(intent: raw))?.intent == intent)
        }
    }

    @Test("mode flags land on the right bits")
    func modeFlags() {
        // ENGAGED | POINTING in bridge.py is 1 | 2.
        let frame = decode(pack(intent: 7, flags: 0b011))
        #expect(frame?.modeFlags.contains(.engaged) == true)
        #expect(frame?.modeFlags.contains(.pointing) == true)
        #expect(frame?.modeFlags.contains(.sweeping) == false)

        let sweeping = decode(pack(intent: 7, flags: 0b101))
        #expect(sweeping?.modeFlags.contains(.sweeping) == true)
        #expect(sweeping?.modeFlags.contains(.pointing) == false)
    }

    @Test("the button field maps to a real button, defaulting to left")
    func buttons() {
        #expect(decode(pack(intent: 3, button: 0))?.mouseButton == .left)
        #expect(decode(pack(intent: 3, button: 1))?.mouseButton == .right)
        // Anything unexpected must not be a third button; left is the safe read.
        #expect(decode(pack(intent: 3, button: 99))?.mouseButton == .left)
    }

    @Test("anything that is not ours is refused")
    func rejectsForeignFrames() {
        #expect(decode(pack(magic: 0xDEAD_BEEF, intent: 1)) == nil, "wrong magic")
        #expect(decode(pack(version: 2, intent: 1)) == nil, "wrong version")
        #expect(decode(pack(intent: 250)) == nil, "unknown intent")
        #expect(decode(Array(pack(intent: 1).prefix(20))) == nil, "truncated")
        #expect(decode([]) == nil, "empty")
    }

    @Test("the event tag matches the one control/events.py stamps")
    func markerAgrees() {
        // If these ever disagree the app stops recognising its own cursor motion
        // and suspends itself the instant it starts working.
        #expect(eventMarker == 0x4D49_4E44)
    }
}

@Suite("tuning decoded from Python's JSON")
struct TuningTests {
    @Test("a partial file sets what it names and leaves the rest at its default")
    func snakeCaseIsConverted() throws {
        // Deliberately partial. Swift's synthesised decoder would throw on the
        // first absent key and drop the whole file, which would mean one field
        // added on either side silently reverted every setting the user had made.
        // The trailing single-letter keys are here because the snake_case
        // conversion has to get "probe_lookahead_s" -> probeLookaheadS right.
        let json = """
            {
              "motion_time_constant": 0.06,
              "snap_radius": 120.0,
              "snap_strength": 0.5,
              "probe_interval_ms": 20.0,
              "probe_lookahead_s": 0.1,
              "probe_timeout_s": 0.08,
              "overlay_glide_s": 0.2,
              "overlay_border_color": [1.0, 0.0, 0.0, 1.0],
              "double_click_ms": 250.0,
              "text_snap_enabled": false
            }
            """
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("mc-tuning-\(UUID().uuidString).json")
        try Data(json.utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        let tuning = Tuning.load(url.path)
        #expect(tuning.motionTimeConstant == 0.06)
        #expect(tuning.snapRadius == 120.0)
        #expect(tuning.snapStrength == 0.5)
        #expect(tuning.probeIntervalMs == 20.0)
        #expect(tuning.probeLookaheadS == 0.1)
        #expect(tuning.probeTimeoutS == 0.08)
        #expect(tuning.overlayGlideS == 0.2)
        #expect(tuning.doubleClickMs == 250.0)
        #expect(tuning.textSnapEnabled == false)
        #expect(tuning.overlayBorderColor == [1.0, 0.0, 0.0, 1.0])

        // Never mentioned in the file, so still the built-in value.
        #expect(tuning.minimumStepPixels == Tuning().minimumStepPixels)
        #expect(tuning.snapStickiness == Tuning().snapStickiness)
        #expect(tuning.overlayEnabled == Tuning().overlayEnabled)
    }

    @Test("a key this side does not know is ignored, not fatal")
    func unknownKeysAreForgiven() throws {
        // The same forgiveness config.py extends to unknown TOML keys, so a newer
        // Python writing a field an older helper has never heard of still works.
        let json = #"{"snap_radius": 150.0, "some_future_knob": 3}"#
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("mc-tuning-\(UUID().uuidString).json")
        try Data(json.utf8).write(to: url)
        defer { try? FileManager.default.removeItem(at: url) }

        #expect(Tuning.load(url.path).snapRadius == 150.0)
    }

    @Test("a missing or unreadable file leaves a usable feel rather than zeroes")
    func fallsBackToDefaults() {
        // Zeroed tuning would mean an infinitely stiff spring and no snap radius,
        // so the failure mode has to be defaults, not an empty struct.
        for path in [nil, "/nonexistent/mindcontrol/tuning.json"] {
            let tuning = Tuning.load(path)
            #expect(tuning.motionTimeConstant > 0)
            #expect(tuning.snapRadius > 0)
            #expect(tuning.doubleClickMs > 0)
        }
    }
}
