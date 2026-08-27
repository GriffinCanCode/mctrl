// Everything about the feel, in one struct.
//
// Read from a JSON file that Python writes out of `config.toml`, so the whole app
// still has exactly one place to tune -- the TOML -- and this process does not
// need a TOML parser to honour it. A `.reloadConfig` frame re-reads the file, so
// the menu bar's reload keeps working without restarting the helper.
//
// Defaults here are the ones that matter: the helper runs with a sane feel even
// if the file is missing entirely.

import CoreGraphics
import Foundation

struct Tuning: Codable {
    // --------------------------------------------------------------- motion
    /// Seconds for the cursor to close most of the gap to where the hand is
    /// pointing. This is the whole answer to "not smooth": the camera delivers a
    /// new goal thirty times a second and the display wants a new position a
    /// hundred and twenty times a second, so the gap has to be interpolated
    /// rather than stepped. Small enough not to feel laggy, large enough that the
    /// steps disappear.
    var motionTimeConstant: Double = 0.045
    /// Below this, a new position is not worth an event. Sub-pixel posts cost a
    /// round trip through the window server and move nothing.
    var minimumStepPixels: Double = 0.35
    /// Ceiling on cursor speed, in pixels per second. A warp across three
    /// monitors should still read as travel rather than a teleport.
    var maximumSpeed: Double = 26000.0

    // ----------------------------------------------------------------- snap
    var snapEnabled: Bool = true
    /// How far from a target its pull is felt, in pixels.
    var snapRadius: Double = 96.0
    /// Fraction of the remaining gap the pull closes per second at full strength.
    var snapStrength: Double = 0.75
    /// A target no larger than this in both axes is pulled to its centre; anything
    /// bigger is pulled only to its nearest edge, so a large panel can be entered
    /// anywhere instead of yanking the cursor to the middle of it.
    var smallTargetPixels: Double = 72.0
    /// Scoring bonus the current target keeps, so the highlight does not flicker
    /// between two adjacent buttons when the cursor sits on the boundary. The same
    /// hysteresis trick the pinch detector already uses, in space instead of time.
    var snapStickiness: Double = 1.4
    /// How much to favour targets in the direction of travel. Where the hand is
    /// heading is better evidence of intent than raw proximity.
    var snapHeadingWeight: Double = 0.45
    /// Snap to words inside text, not just to the text element as a whole.
    var textSnapEnabled: Bool = true

    // ---------------------------------------------------------------- probe
    /// Milliseconds between accessibility hit tests. Measured cost is 0.43 ms at
    /// the median but 3.46 ms at the 95th percentile, which is why this runs on
    /// its own thread and the cursor never waits for it.
    var probeIntervalMs: Double = 16.0
    /// How far ahead along the velocity vector to look, in seconds. Probing where
    /// the cursor is going rather than where it is means the target is already
    /// resolved by the time it arrives.
    var probeLookaheadS: Double = 0.08
    /// Give up on an unresponsive application this quickly, in seconds. An app
    /// wedged on its main thread must cost one skipped probe, not a frozen cursor.
    var probeTimeoutS: Double = 0.05
    /// Drop a target the probe has not seen for this long.
    var targetLifetimeMs: Double = 350.0

    // -------------------------------------------------------------- overlay
    var overlayEnabled: Bool = true
    var overlayCornerRadius: Double = 6.0
    var overlayBorderWidth: Double = 2.0
    /// Seconds for the highlight to glide to a new target. The compositor runs
    /// this, not us, which is why it stays smooth while the probe is stuttering.
    var overlayGlideS: Double = 0.11
    /// RGBA, 0-1.
    var overlayBorderColor: [Double] = [0.36, 0.72, 1.0, 0.95]
    var overlayFillColor: [Double] = [0.36, 0.72, 1.0, 0.14]

    // --------------------------------------------------------------- clicks
    var doubleClickMs: Double = 400.0

    /// Read the tuning file, keeping the default for anything it does not mention.
    ///
    /// Swift's synthesised decoder demands every key and throws on the first one
    /// missing, which would mean a single field added on this side -- or dropped on
    /// Python's -- silently discarded the user's entire configuration and ran on
    /// defaults. So the defaults are encoded to a dictionary first and the file is
    /// overlaid onto it. Encoding with the same snake_case strategy the file uses
    /// means the two sets of keys line up without a translation table, and without
    /// naming all twenty-odd fields a second time just to list them.
    static func load(_ path: String?) -> Tuning {
        let defaults = Tuning()
        guard let path, let data = FileManager.default.contents(atPath: path) else {
            return defaults
        }

        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase

        do {
            let base = try encoder.encode(defaults)
            guard var merged = try JSONSerialization.jsonObject(with: base) as? [String: Any],
                let patch = try JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return defaults }
            // Unknown keys are carried through and ignored by the decoder, which is
            // the same forgiveness config.py extends to unknown TOML keys.
            for (key, value) in patch { merged[key] = value }
            let union = try JSONSerialization.data(withJSONObject: merged)
            return try decoder.decode(Tuning.self, from: union)
        } catch {
            FileHandle.standardError.write(
                Data("[bridge] ignoring unreadable tuning file: \(error)\n".utf8))
            return defaults
        }
    }

    var borderCGColor: CGColor { Tuning.color(overlayBorderColor, fallback: 1.0) }
    var fillCGColor: CGColor { Tuning.color(overlayFillColor, fallback: 0.15) }

    private static func color(_ parts: [Double], fallback alpha: Double) -> CGColor {
        guard parts.count >= 3 else {
            return CGColor(red: 0.36, green: 0.72, blue: 1.0, alpha: alpha)
        }
        return CGColor(
            red: parts[0], green: parts[1], blue: parts[2],
            alpha: parts.count > 3 ? parts[3] : alpha)
    }
}

private extension FileManager {
    func contents(atPath path: String) -> Data? {
        FileManager.default.fileExists(atPath: path)
            ? try? Data(contentsOf: URL(fileURLWithPath: path)) : nil
    }
}
