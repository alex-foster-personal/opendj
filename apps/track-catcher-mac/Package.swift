// swift-tools-version:5.9
// Open DJ Track Catcher: a small always-on-top Mac app that shows the live
// key, notes and BPM of whatever is playing and catches the track into the
// Open DJ Inbox.
//
// Three targets, kept apart on purpose (KISS, modular):
// - TrackCatcherCore: Foundation only. Key names, copy text, the ASCII
//   wheel, tap tempo, the engine's JSON lines, the Inbox file. Builds and is
//   tested on Linux as well as macOS.
// - TrackCatcher: the macOS app (SwiftUI + AppKit, AVFoundation, ShazamKit).
//   It talks to the engine only through the `odj-live` CLI.
// - track-catcher: the CLI behind every button, so an agent can drive the
//   app without clicking.
import PackageDescription

var targets: [Target] = [
    .target(name: "TrackCatcherCore"),
    .testTarget(name: "TrackCatcherCoreTests", dependencies: ["TrackCatcherCore"]),
    .executableTarget(name: "track-catcher", dependencies: ["TrackCatcherCore"]),
]
var products: [Product] = [
    .library(name: "TrackCatcherCore", targets: ["TrackCatcherCore"]),
    .executable(name: "track-catcher", targets: ["track-catcher"]),
]
#if os(macOS)
targets.append(.executableTarget(name: "TrackCatcher", dependencies: ["TrackCatcherCore"]))
products.append(.executable(name: "TrackCatcher", targets: ["TrackCatcher"]))
#endif

let package = Package(
    name: "TrackCatcher",
    platforms: [.macOS(.v14)],
    products: products,
    targets: targets
)
