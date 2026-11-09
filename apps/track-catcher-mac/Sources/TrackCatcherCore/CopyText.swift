import Foundation

/// The text each click copies, word for word as the design specifies.
public enum CopyText {
    /// Click on the big key: "12B · E major · chord E · 124 BPM".
    /// Missing parts are left out rather than shown as placeholders.
    public static func readout(key: MusicalKey, chord: String?, bpm: Int?) -> String {
        var parts = [key.camelot, key.name]
        if let chord, !chord.isEmpty { parts.append("chord \(chord)") }
        if let bpm { parts.append("\(bpm) BPM") }
        return parts.joined(separator: " · ")
    }

    /// Click on one key of the wheel: "9A · E minor".
    public static func wheelKey(_ key: MusicalKey) -> String {
        "\(key.camelot) · \(key.name)"
    }

    /// Click on the note bars: the seven loudest pitch classes, loudest first.
    /// Only pitch classes actually heard (above zero) are named, so an empty
    /// readout copies "Notes heard: none yet" rather than seven arbitrary notes.
    public static func notesHeard(_ chroma: [Double]) -> String {
        guard chroma.contains(where: { $0 > 0 }) else { return "Notes heard: none yet" }
        let order = chroma.indices.filter { chroma[$0] > 0 }.sorted { chroma[$0] > chroma[$1] }.prefix(7)
        return "Notes heard: " + order.map { MusicalKey.notes[$0] }.joined(separator: " ")
    }

    /// The toast shown after a copy: a multi-line copy is named, not printed.
    public static func toast(for copied: String) -> String {
        "Copied  " + (copied.contains("\n") ? "ASCII wheel" : copied)
    }

    /// Shift-click on the wheel: a 7-row Camelot wheel in plain text, the
    /// current key in brackets. Same layout as the design's `asciiWheel`.
    public static func asciiWheel(current: MusicalKey) -> String {
        let cur = current.camelot
        func cell(_ n: Int, _ l: String) -> String {
            let c = "\(n)\(l)"
            return c == cur ? "[\(c)]" : c
        }
        let pos: [(Int, Int, Int)] = [
            (12, 0, 9), (1, 1, 15), (2, 2, 19), (3, 3, 21), (4, 4, 19), (5, 5, 15),
            (6, 6, 9), (7, 5, 3), (8, 4, 0), (9, 3, -2), (10, 2, 0), (11, 1, 3),
        ]
        var rows = Array(repeating: Array(repeating: Character(" "), count: 30), count: 7)
        for (n, r, c) in pos {
            let text = Array(cell(n, "B") + "/" + cell(n, "A"))
            let col = max(0, c + 4)
            for i in 0..<text.count where col + i < 30 {
                rows[r][col + i] = text[i]
            }
        }
        let body = rows.map { row -> String in
            var s = String(row)
            while s.last == " " { s.removeLast() }
            return s
        }
        return "Camelot (B/A), now \(cur)\n" + body.joined(separator: "\n")
    }

    /// m:ss for a time in seconds.
    public static func clock(_ seconds: Double) -> String {
        let s = max(0, Int(seconds.rounded()))
        return String(format: "%d:%02d", s / 60, s % 60)
    }

    /// The drop zone's result line:
    /// "8A · A minor · 124 BPM · key change at 3:12 → 9A · E minor".
    public static func trackResult(_ summary: EngineSummary) -> String {
        // The line reads in time order, so when segmentation worked it opens
        // on the first segment's key: the whole-track key comes from mean
        // chroma and can differ from how the track starts.
        let segs = (summary.segments.status == "ok" ? summary.segments.segments : [])
            .compactMap { seg in MusicalKey(camelot: seg.keyCamelot).map { (seg.startS, $0) } }
        guard let key = segs.first?.1 ?? summary.key.flatMap({ MusicalKey(camelot: $0) }) else {
            return "No key found" + (summary.segments.reason.map { " (\($0))" } ?? "")
        }
        var parts = [key.camelot, key.name]
        if let bpm = summary.bpm { parts.append("\(Int(bpm.rounded())) BPM") }
        var line = parts.joined(separator: " · ")
        var current = key.camelot
        for (start, k) in segs.dropFirst() where k.camelot != current {
            line += " · key change at \(clock(start)) → \(k.camelot) · \(k.name)"
            current = k.camelot
        }
        return line
    }
}
