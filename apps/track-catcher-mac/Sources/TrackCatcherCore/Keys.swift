import Foundation

/// A musical key: pitch class 0 (C) to 11 (B) and a mode bit, named the way
/// the design names it ("12B", "E major", "E maj").
public struct MusicalKey: Hashable, Codable, Sendable {
    public let pc: Int
    public let minor: Bool

    public init(pc: Int, minor: Bool) {
        self.pc = ((pc % 12) + 12) % 12
        self.minor = minor
    }

    /// Note names as the window shows them.
    public static let notes = ["C", "C♯", "D", "E♭", "E", "F", "F♯", "G", "A♭", "A", "B♭", "B"]

    /// Camelot number 1-12: C major is 8B, A minor is 8A, a fifth up is +1.
    public var camelotNumber: Int {
        let rel = minor ? (pc + 3) % 12 : pc
        return (7 * rel % 12 + 7) % 12 + 1
    }

    public var camelot: String { "\(camelotNumber)\(minor ? "A" : "B")" }

    /// Open Key: C major is 1d, A minor is 1m.
    public var openKey: String { "\((camelotNumber + 4) % 12 + 1)\(minor ? "m" : "d")" }

    /// "E major", "F♯ minor".
    public var name: String { MusicalKey.notes[pc] + (minor ? " minor" : " major") }

    /// "E maj", "F♯m".
    public var shortName: String { MusicalKey.notes[pc] + (minor ? "m" : " maj") }

    /// Pitch classes of the key's scale, tonic first.
    public var scale: [Int] {
        (minor ? [0, 2, 3, 5, 7, 8, 10] : [0, 2, 4, 5, 7, 9, 11]).map { ($0 + pc) % 12 }
    }

    /// Parse a Camelot code such as "12B" or "8a".
    public init?(camelot code: String) {
        let s = code.trimmingCharacters(in: .whitespaces).uppercased()
        guard let mode = s.last, mode == "A" || mode == "B",
              let n = Int(s.dropLast()), (1...12).contains(n) else { return nil }
        let minor = mode == "A"
        // Invert camelotNumber: n - 1 = 7 * rel + 7 (mod 12), and 7 is its own inverse mod 12.
        let rel = (7 * (n - 1 - 7) % 12 + 12) % 12
        self.init(pc: minor ? rel + 9 : rel, minor: minor)
    }

    /// All 24 keys in Camelot order: 1A, 1B, 2A ... 12B.
    public static let all: [MusicalKey] = (1...12).flatMap { n in
        ["A", "B"].compactMap { MusicalKey(camelot: "\(n)\($0)") }
    }
}
