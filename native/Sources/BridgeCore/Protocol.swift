// The wire between the gesture engine and the machine.
//
// Python decides what a hand meant; this process decides what the cursor does
// about it. Between them sits one fixed-width binary frame over a Unix datagram
// socket -- 48 bytes, little-endian, no allocation, no parsing.
//
// Datagrams rather than a stream, because every intent is independent and
// self-delimiting: there is no framing to get wrong, and a dropped packet costs
// one frame of motion rather than desynchronising the connection. At the rates
// involved -- thirty pointer updates a second, a click now and then -- the
// socket never comes close to its buffer.
//
// Deltas travel, not positions. The sender never needs to know where the cursor
// actually is, which means it never has to read it back, which means there is no
// round trip anywhere on the interaction path.

import Foundation

enum Intent: UInt16 {
    /// Relative pointer motion, in pixels, from the gesture engine.
    case moveBy = 1
    /// Absolute jump expressed as a fraction of the gaze-calibrated display.
    case warpToFraction = 2
    case click = 3
    case press = 4
    case release = 5
    case scroll = 6
    /// Engagement and which gesture is running, so snapping knows when to apply.
    case setMode = 7
    /// Drop everything held. Sent on suspend, disengage and shutdown.
    case releaseAll = 8
    /// Re-read the tuning file named on the command line.
    case reloadConfig = 9
    case shutdown = 10
}

/// Bit flags carried by `.setMode`.
struct ModeFlags: OptionSet {
    let rawValue: UInt32

    /// Gesture output should reach the system at all.
    static let engaged = ModeFlags(rawValue: 1 << 0)
    /// A pointing gesture is driving, so targets should be sought and snapped to.
    static let pointing = ModeFlags(rawValue: 1 << 1)
    /// A scroll or swipe is running. Snapping must stand down: these gestures are
    /// not aiming at anything, and a magnetic pull during a sweep fights the hand.
    static let sweeping = ModeFlags(rawValue: 1 << 2)
}

struct Frame {
    static let magic: UInt32 = 0x4D49_4E44  // "MIND", the same tag the events carry
    static let version: UInt16 = 1
    static let byteCount = 48

    let intent: Intent
    let sequence: UInt32
    let flags: UInt32
    /// dx, or the x fraction for a warp.
    let a: Double
    /// dy, or the y fraction for a warp.
    let b: Double
    /// Sender's monotonic clock, for measuring one-way latency.
    let sentAt: Double
    let button: UInt32

    var modeFlags: ModeFlags { ModeFlags(rawValue: flags) }

    /// Decode one datagram, rejecting anything that is not ours.
    init?(_ bytes: UnsafeRawBufferPointer) {
        guard bytes.count >= Frame.byteCount,
            UInt32(littleEndian: bytes.loadUnaligned(fromByteOffset: 0, as: UInt32.self))
                == Frame.magic,
            UInt16(littleEndian: bytes.loadUnaligned(fromByteOffset: 4, as: UInt16.self))
                == Frame.version,
            let intent = Intent(
                rawValue: UInt16(
                    littleEndian: bytes.loadUnaligned(fromByteOffset: 6, as: UInt16.self)))
        else { return nil }

        self.intent = intent
        sequence = UInt32(littleEndian: bytes.loadUnaligned(fromByteOffset: 8, as: UInt32.self))
        flags = UInt32(littleEndian: bytes.loadUnaligned(fromByteOffset: 12, as: UInt32.self))
        a = Double(
            bitPattern: UInt64(
                littleEndian: bytes.loadUnaligned(fromByteOffset: 16, as: UInt64.self)))
        b = Double(
            bitPattern: UInt64(
                littleEndian: bytes.loadUnaligned(fromByteOffset: 24, as: UInt64.self)))
        sentAt = Double(
            bitPattern: UInt64(
                littleEndian: bytes.loadUnaligned(fromByteOffset: 32, as: UInt64.self)))
        button = UInt32(littleEndian: bytes.loadUnaligned(fromByteOffset: 40, as: UInt32.self))
    }

    var mouseButton: MouseButton { button == 1 ? .right : .left }
}
