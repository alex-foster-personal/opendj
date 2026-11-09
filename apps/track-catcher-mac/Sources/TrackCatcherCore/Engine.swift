import Foundation

/// The engine is the `odj-live` CLI from apps/live-core, run as a child
/// process. The app never links Rust; it writes raw f32 samples to stdin and
/// reads JSON lines from stdout. That keeps the two halves swappable.
public enum EngineLocator {
    /// `ODJ_LIVE` if set, then next to the running executable (inside the
    /// .app that is Contents/MacOS), then each `PATH` entry.
    public static func find(environment: [String: String] = ProcessInfo.processInfo.environment,
                            executableDir: URL? = Bundle.main.executableURL?.deletingLastPathComponent()) throws -> URL {
        var tried: [String] = []
        var candidates: [URL] = []
        if let env = environment["ODJ_LIVE"], !env.isEmpty { candidates.append(URL(fileURLWithPath: env)) }
        if let dir = executableDir { candidates.append(dir.appendingPathComponent("odj-live")) }
        for p in (environment["PATH"] ?? "").split(separator: ":") {
            candidates.append(URL(fileURLWithPath: String(p)).appendingPathComponent("odj-live"))
        }
        for c in candidates {
            tried.append(c.path)
            if FileManager.default.isExecutableFile(atPath: c.path) { return c }
        }
        throw EngineError.notFound(tried.joined(separator: ", "))
    }
}

/// A live stream: push samples as they arrive, frames come back on `onFrame`.
public final class LiveEngine: @unchecked Sendable {
    private let process = Process()
    private let stdin = Pipe()
    private let stdout = Pipe()
    private let stderr = Pipe()
    private var splitter = LineSplitter()
    private let lock = NSLock()
    private let writer = DispatchQueue(label: "odj-live.stdin")
    private var queued = 0
    private var stopped = false
    /// Buffers handed to `enqueue` but not yet written. Past this the newest
    /// buffer is dropped, so a stalled engine can't grow memory without bound.
    public let maxQueued: Int
    /// Buffers `enqueue` dropped because the engine fell behind.
    public private(set) var dropped = 0
    /// Fires on the enqueuing thread when a drop starts a run of drops,
    /// not for each one: everything analyzed after it has a hole in the
    /// audio and a backlog of older buffers ahead of the newest. Must not block.
    public var onDrop: (() -> Void)?
    private var dropping = false
    public var onFrame: ((EngineFrame) -> Void)?
    public var onSummary: ((EngineSummary) -> Void)?
    public var onError: ((Error) -> Void)?
    /// The process exited, with its status; fires for `stop()` too.
    public var onExit: ((Int32) -> Void)?

    public init(executable: URL, sampleRate: Int, hz: Double = 10, maxQueued: Int = 64) {
        self.maxQueued = maxQueued
        process.executableURL = executable
        process.arguments = ["--stdin-f32", "--rate", String(sampleRate), "--hz", String(hz)]
        process.standardInput = stdin
        process.standardOutput = stdout
        process.standardError = stderr
    }

    public func start() throws {
        stdout.fileHandleForReading.readabilityHandler = { [weak self] h in
            guard let self else { return }
            let data = h.availableData
            if data.isEmpty { h.readabilityHandler = nil; return }
            self.lock.lock()
            let lines = self.splitter.push(data)
            self.lock.unlock()
            for line in lines {
                do {
                    switch try EngineLine.decode(line) {
                    case .frame(let f): self.onFrame?(f)
                    case .summary(let s): self.onSummary?(s)
                    }
                } catch { self.onError?(error) }
            }
        }
        process.terminationHandler = { [weak self] p in self?.onExit?(p.terminationStatus) }
        try process.run()
    }

    /// Mono f32 samples at the rate given to `init`.
    public func push(_ samples: [Float]) {
        guard process.isRunning, !samples.isEmpty else { return }
        let data = samples.withUnsafeBufferPointer { Data(buffer: $0) }
        do { try stdin.fileHandleForWriting.write(contentsOf: data) } catch { onError?(error) }
    }

    /// For real-time callers such as an audio tap: copies nothing further,
    /// never blocks, and writes on a serial queue so a slow engine can't
    /// stall the caller. Order is preserved.
    public func enqueue(_ samples: [Float]) {
        lock.lock()
        if queued >= maxQueued {
            dropped += 1
            let first = !dropping
            dropping = true
            lock.unlock()
            if first { onDrop?() }
            return
        }
        dropping = false
        queued += 1
        lock.unlock()
        writer.async { [self] in
            lock.lock(); let skip = stopped; lock.unlock()
            if !skip { push(samples) }
            lock.lock(); queued -= 1; lock.unlock()
        }
    }

    /// Close stdin: the engine prints its summary and exits. Waits for
    /// buffers already enqueued so none are written after the close.
    public func finish() {
        writer.sync {}
        try? stdin.fileHandleForWriting.close()
    }

    /// Stop now: buffers still queued are discarded, not waited for, so a
    /// stalled engine can't hang the caller.
    public func stop() {
        lock.lock(); stopped = true; lock.unlock()
        try? stdin.fileHandleForWriting.close()
        if process.isRunning { process.terminate() }
    }
}

/// Whole-file analysis for the drop zone.
public enum FileAnalysis {
    /// Runs `odj-live --file`; `progress` gets 0...1 from frame times over
    /// `duration` (pass the file's duration when known, else progress stays
    /// at 0 until the summary). `started` gets the running process, so a
    /// caller can terminate an analysis nobody wants any more. Returns the summary.
    public static func run(executable: URL, file: URL, duration: Double?,
                           started: ((Process) -> Void)? = nil,
                           progress: @escaping (Double) -> Void) throws -> EngineSummary {
        let process = Process()
        process.executableURL = executable
        process.arguments = ["--file", file.path, "--hz", "2"]
        let out = Pipe(), err = Pipe()
        process.standardOutput = out
        process.standardError = err
        try process.run()
        started?(process)
        var splitter = LineSplitter()
        var summary: EngineSummary?
        var decodeError: Error?
        let reader = out.fileHandleForReading
        while true {
            let data = reader.availableData
            if data.isEmpty { break }
            for line in splitter.push(data) {
                do {
                    switch try EngineLine.decode(line) {
                    case .frame(let f):
                        if let d = duration, d > 0 { progress(min(1, f.t / d)) }
                    case .summary(let s): summary = s
                    }
                } catch { decodeError = decodeError ?? error }
            }
        }
        process.waitUntilExit()
        let errText = String(decoding: err.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
        guard process.terminationStatus == 0 else {
            throw EngineError.failed(status: process.terminationStatus, stderr: errText)
        }
        if let decodeError { throw decodeError }
        guard let summary else { throw EngineError.failed(status: 0, stderr: "no summary line. \(errText)") }
        progress(1)
        return summary
    }
}
