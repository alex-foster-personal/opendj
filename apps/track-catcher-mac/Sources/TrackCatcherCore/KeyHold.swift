import Foundation

/// The shown key flips only after a new key has led for `holdSeconds`; while
/// it waits, the window shows "E major · F♯m?".
public struct KeyHold: Sendable {
    public let holdSeconds: Double
    public private(set) var shown: MusicalKey?
    public private(set) var pending: MusicalKey?
    private var pendingSince = 0.0

    public init(holdSeconds: Double = 6) { self.holdSeconds = holdSeconds }

    public mutating func update(top: MusicalKey?, at t: Double) {
        guard let top else { pending = nil; return }
        guard let current = shown else { shown = top; return }
        if top == current { pending = nil; return }
        if pending != top { pending = top; pendingSince = t; return }
        if t - pendingSince >= holdSeconds { shown = top; pending = nil }
    }

    /// "E major" or "E major · F♯m?".
    public var label: String {
        guard let shown else { return "" }
        return shown.name + (pending.map { " · \($0.shortName)?" } ?? "")
    }
}

/// Commands the CLI sends to the running app (and the app's own buttons
/// call). One name per button, so an agent can drive everything.
public enum AppCommand: String, CaseIterable, Sendable {
    case catchTrack = "catch"
    case nowPlaying = "now-playing"
    case listen, stop, tap, copy, settings, setup, quit
    case compact, pin, half, double
    /// Toggle the bars between notes heard and key scores, and the ring
    /// between Camelot and piano order (the views' right-click menus).
    case bars, ring
    case copyNotes = "copy-notes"
    case copyWheel = "copy-wheel"
    /// Copies one wheel key, named with `--key 9A`, as a click on it does.
    case copyKey = "copy-key"

    /// Distributed notification name the app observes.
    public static let notificationName = "com.opendj.trackcatcher.command"
}
