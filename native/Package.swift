// swift-tools-version: 6.0

import PackageDescription

// Swift 5 language mode, deliberately. The concurrency here is narrow and already
// enforced structurally: one thread owns the cursor, the probe publishes immutable
// value types behind a lock, and AppKit work is hopped to main. Swift 6 mode
// cannot see any of that, because AXUIElement is a non-Sendable C type that has to
// cross a queue boundary by design -- satisfying the checker would mean wrapping
// every accessibility handle in @unchecked Sendable, which adds noise and asserts
// the same invariant without proving anything more.
let mode: [SwiftSetting] = [.swiftLanguageMode(.v5)]

let package = Package(
    name: "mindcontrol-bridge",
    platforms: [.macOS(.v14)],
    targets: [
        // The logic lives in a library so it can be tested. Target selection and
        // frame decoding are pure functions over value types, and both have had a
        // real bug in them, so they are worth holding still.
        .target(name: "BridgeCore", path: "Sources/BridgeCore", swiftSettings: mode),
        .executableTarget(
            name: "mindcontrol-bridge",
            dependencies: ["BridgeCore"],
            path: "Sources/Bridge",
            swiftSettings: mode
        ),
        .testTarget(
            name: "BridgeCoreTests",
            dependencies: ["BridgeCore"],
            path: "Tests/BridgeCoreTests",
            swiftSettings: mode
        ),
    ]
)
