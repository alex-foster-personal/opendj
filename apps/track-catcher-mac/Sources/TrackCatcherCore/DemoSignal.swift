import Foundation

/// The design's demo audio, synthesized: an E major loop (E, C♯m, A, B) that
/// modulates to F♯ minor (F♯m, D, E, C♯) every 32 s, with a 124 BPM click.
/// It goes through the real engine, so the first-launch preview and the
/// snapshot screenshots show the engine's own readout, not canned numbers.
public struct DemoSignal: Sendable {
    public let sampleRate: Double
    public private(set) var position = 0

    static let partA: [(Int, Bool)] = [(4, false), (1, true), (9, false), (11, false)]
    static let partB: [(Int, Bool)] = [(6, true), (2, false), (4, false), (1, false)]
    public static let chordSeconds = 2.0
    public static let partSeconds = 32.0
    public static let bpm = 124.0

    public init(sampleRate: Double = 22050) { self.sampleRate = sampleRate }

    /// The chord sounding at `t` seconds: root pitch class and minor flag.
    public static func chord(at t: Double) -> (Int, Bool) {
        let inB = t.truncatingRemainder(dividingBy: 2 * partSeconds) >= partSeconds
        let part = inB ? partB : partA
        return part[Int(t / chordSeconds) % part.count]
    }

    /// The key the demo is in at `t`: E major (12B) then F♯ minor (11A).
    public static func key(at t: Double) -> MusicalKey {
        t.truncatingRemainder(dividingBy: 2 * partSeconds) >= partSeconds
            ? MusicalKey(pc: 6, minor: true) : MusicalKey(pc: 4, minor: false)
    }

    /// The next `count` mono samples.
    public mutating func next(_ count: Int) -> [Float] {
        var out = [Float](repeating: 0, count: count)
        let beat = 60 / DemoSignal.bpm
        for i in 0..<count {
            let t = Double(position + i) / sampleRate
            let (root, minor) = DemoSignal.chord(at: t)
            let third = (root + (minor ? 3 : 4)) % 12, fifth = (root + 7) % 12
            var v = 0.0
            // Triad around middle C, plus the root an octave down as bass.
            for (pc, octave, amp) in [(root, 4, 0.22), (third, 4, 0.16), (fifth, 4, 0.16), (root, 3, 0.2)] {
                let midi = Double(12 * (octave + 1) + pc)
                let hz = 440 * pow(2, (midi - 69) / 12)
                v += amp * sin(2 * .pi * hz * t)
            }
            // A short click on every beat for the tempo.
            let sinceBeat = t.truncatingRemainder(dividingBy: beat)
            if sinceBeat < 0.01 { v += 0.5 * exp(-sinceBeat * 600) * sin(2 * .pi * 1800 * t) }
            out[i] = Float(v * 0.6)
        }
        position += count
        return out
    }
}
