import Foundation

/// Tap BPM: four taps give a reading, it averages the last eight intervals,
/// and a gap over 2 s starts again. ½ and ×2 fix half or double tempo.
public struct TapTempo: Sendable {
    public private(set) var taps: [Double] = []
    public private(set) var bpm: Double?
    private var factor = 1.0

    public init() {}

    /// `now` in seconds (any monotonic clock).
    public mutating func tap(at now: Double) {
        if let last = taps.last, now - last > 2 { taps = []; bpm = nil; factor = 1 }
        taps.append(now)
        if taps.count > 9 { taps.removeFirst() }
        if taps.count >= 4, let first = taps.first, let last = taps.last, last > first {
            bpm = 60 * Double(taps.count - 1) / (last - first) * factor
        }
    }

    public mutating func halve() { if bpm != nil { factor /= 2; bpm! /= 2 } }
    public mutating func double() { if bpm != nil { factor *= 2; bpm! *= 2 } }

    /// "tap 4+ times", "2 taps…", "tapped 124.0".
    public var label: String {
        if let bpm { return String(format: "tapped %.1f", bpm) }
        if taps.isEmpty { return "tap 4+ times" }
        return "\(taps.count) tap\(taps.count > 1 ? "s" : "")…"
    }
}
