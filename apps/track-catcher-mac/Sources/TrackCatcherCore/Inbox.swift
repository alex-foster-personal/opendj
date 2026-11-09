import Foundation

/// One caught track. Written as a JSON line to the Inbox file, which is the
/// contract with the main app: Open DJ reads new lines and turns them into
/// Inbox entries (or entries in `playlist`).
public struct CatchEntry: Codable, Equatable, Sendable {
    public enum Source: String, Codable, Sendable { case shazam, spotify, music }

    public var caughtAt: String
    public var source: Source
    public var title: String
    public var artist: String
    public var playlist: String
    public var heardKey: String?
    public var heardBpm: Int?
    public var isrc: String?
    public var appleMusicID: String?
    public var spotifyID: String?

    public init(caughtAt: Date = Date(), source: Source, title: String, artist: String, playlist: String = "Inbox",
                heardKey: String? = nil, heardBpm: Int? = nil, isrc: String? = nil,
                appleMusicID: String? = nil, spotifyID: String? = nil) {
        self.caughtAt = CatchEntry.stamp(caughtAt)
        self.source = source
        self.title = title
        self.artist = artist
        self.playlist = playlist
        self.heardKey = heardKey
        self.heardBpm = heardBpm
        self.isrc = isrc
        self.appleMusicID = appleMusicID
        self.spotifyID = spotifyID
    }

    enum CodingKeys: String, CodingKey {
        case source, title, artist, playlist, isrc
        case caughtAt = "caught_at", heardKey = "heard_key", heardBpm = "heard_bpm"
        case appleMusicID = "apple_music_id", spotifyID = "spotify_id"
    }

    /// UTC, ISO 8601, zone printed by the formatter (never a typed Z).
    static func stamp(_ d: Date) -> String {
        let f = ISO8601DateFormatter()
        f.timeZone = TimeZone(identifier: "UTC")
        return f.string(from: d)
    }

    /// The line shown under the capture buttons:
    /// "from Spotify · heard 12B · 124 BPM · added to Inbox".
    public var resultLine: String {
        var parts = ["from " + (source == .shazam ? "Shazam" : source == .spotify ? "Spotify" : "Apple Music")]
        if let heardKey { parts.append("heard \(heardKey)") }
        if let heardBpm { parts.append("\(heardBpm) BPM") }
        parts.append("added to \(playlist)")
        return parts.joined(separator: " · ")
    }
}

/// Append-only JSON lines file, one `CatchEntry` per line.
public struct Inbox: Sendable {
    public let url: URL

    public init(url: URL) { self.url = url }

    /// ~/Library/Application Support/Open DJ Track Catcher/inbox.jsonl on macOS.
    public static func standard() -> Inbox {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent(".local/share")
        return Inbox(url: base.appendingPathComponent("Open DJ Track Catcher/inbox.jsonl"))
    }

    public func append(_ entry: CatchEntry) throws {
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let enc = JSONEncoder()
        enc.outputFormatting = [.sortedKeys]
        var line = try enc.encode(entry)
        line.append(0x0A)
        if FileManager.default.fileExists(atPath: url.path) {
            let h = try FileHandle(forWritingTo: url)
            defer { try? h.close() }
            try h.seekToEnd()
            try h.write(contentsOf: line)
        } else {
            try line.write(to: url)
        }
    }

    public func entries() throws -> [CatchEntry] {
        guard FileManager.default.fileExists(atPath: url.path) else { return [] }
        let dec = JSONDecoder()
        return try String(decoding: Data(contentsOf: url), as: UTF8.self)
            .split(separator: "\n")
            .map { try dec.decode(CatchEntry.self, from: Data($0.utf8)) }
    }

    /// Playlists for the ▾ menus: Inbox first, then the most recently used,
    /// newest first, without repeats. An Inbox that can't be read or has a
    /// malformed line throws, so the caller shows the damage instead of an
    /// empty history.
    public func recentPlaylists(limit: Int = 5) throws -> [String] {
        let used = try entries().reversed().map(\.playlist)
        var seen: Set<String> = []
        var out: [String] = []
        for name in ["Inbox"] + used where !seen.contains(name) {
            seen.insert(name)
            out.append(name)
            if out.count == limit { break }
        }
        return out
    }
}
