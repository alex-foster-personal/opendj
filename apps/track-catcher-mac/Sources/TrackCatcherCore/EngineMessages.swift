import Foundation

/// One key score from `odj-live`. The engine keeps `score` (the profile
/// match) and publishes no per-key probability (TCLIVE-05), so the bars
/// draw from `score`.
public struct EngineKeyScore: Codable, Equatable, Sendable {
    public let camelot: String
    public let openkey: String
    public let name: String
    public let score: Double

    public init(camelot: String, openkey: String, name: String, score: Double) {
        self.camelot = camelot
        self.openkey = openkey
        self.name = name
        self.score = score
    }

    /// Bar length in 0...1: the kept score floored at 0 and capped at 1.
    public var barFraction: Double { min(max(score, 0), 1) }
}

/// One `{"type":"frame"}` line from `odj-live` (see apps/live-core/src/lib.rs).
public struct EngineFrame: Codable, Equatable, Sendable {
    public let t: Double
    public let chroma: [Double]
    public let chromaFast: [Double]
    public let chromaSlow: [Double]
    public let keys: [EngineKeyScore]
    public let key: String?
    public let keyName: String?
    public let keyOpenkey: String?
    public let confidence: Double?
    public let margin: Double?
    public let bpm: Double?
    public let tuningCents: Double?

    enum CodingKeys: String, CodingKey {
        case t, chroma, keys, key, confidence, margin, bpm
        case chromaFast = "chroma_fast", chromaSlow = "chroma_slow"
        case keyName = "key_name", keyOpenkey = "key_openkey", tuningCents = "tuning_cents"
    }
}

public struct EngineSegment: Codable, Equatable, Sendable {
    public let startS: Double
    public let endS: Double
    public let keyCamelot: String
    public let keyName: String
    public let confidence: Double

    enum CodingKeys: String, CodingKey {
        case confidence
        case startS = "start_s", endS = "end_s", keyCamelot = "key_camelot", keyName = "key_name"
    }
}

public struct EngineSegmentBlock: Codable, Equatable, Sendable {
    public let status: String
    public let reason: String?
    public let window: String
    public let segments: [EngineSegment]
}

/// The `{"type":"summary"}` line at the end of a file or stream.
public struct EngineSummary: Codable, Equatable, Sendable {
    public let durationS: Double
    public let keys: [EngineKeyScore]
    public let key: String?
    public let keyName: String?
    public let confidence: Double?
    public let margin: Double?
    public let bpm: Double?
    public let tuningCents: Double?
    public let segments: EngineSegmentBlock

    enum CodingKeys: String, CodingKey {
        case keys, key, confidence, margin, bpm, segments
        case durationS = "duration_s", keyName = "key_name", tuningCents = "tuning_cents"
    }
}

public enum EngineLine: Equatable, Sendable {
    case frame(EngineFrame)
    case summary(EngineSummary)

    private struct Kind: Decodable { let type: String }

    /// Decode one JSON line. Unknown types and malformed lines are errors,
    /// never silently dropped.
    public static func decode(_ line: Data) throws -> EngineLine {
        let d = JSONDecoder()
        switch try d.decode(Kind.self, from: line).type {
        case "frame": return .frame(try d.decode(EngineFrame.self, from: line))
        case "summary": return .summary(try d.decode(EngineSummary.self, from: line))
        case let other: throw EngineError.unknownLine(other)
        }
    }
}

public enum EngineError: Error, Equatable, CustomStringConvertible {
    case unknownLine(String)
    case notFound(String)
    case failed(status: Int32, stderr: String)

    public var description: String {
        switch self {
        case .unknownLine(let t): return "odj-live sent an unknown line type \(t)"
        case .notFound(let where_): return "odj-live not found (looked in \(where_))"
        case .failed(let s, let e): return "odj-live exited \(s): \(e)"
        }
    }
}

/// Splits a byte stream into lines; keeps a partial line until it completes.
public struct LineSplitter {
    private var buffer = Data()
    public init() {}

    public mutating func push(_ data: Data) -> [Data] {
        buffer.append(data)
        var lines: [Data] = []
        while let nl = buffer.firstIndex(of: 0x0A) {
            let line = buffer[buffer.startIndex..<nl]
            if !line.isEmpty { lines.append(Data(line)) }
            buffer.removeSubrange(buffer.startIndex...nl)
        }
        return lines
    }
}
