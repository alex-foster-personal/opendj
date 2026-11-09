import AppKit
import AVFoundation
import CoreAudio
import ShazamKit
import TrackCatcherCore

/// One input device, for the Settings picker.
struct AudioInput: Hashable, Identifiable {
    let id: AudioDeviceID
    let uid: String
    let name: String

    /// A Core Audio call that failed while listing inputs, with its OSStatus,
    /// so Settings shows the failure instead of an empty device list.
    struct ListError: LocalizedError {
        let what: String
        let status: OSStatus
        var errorDescription: String? { "Can't list inputs: \(what) failed (OSStatus \(status))" }
    }

    /// Every device with at least one input channel. Throws when Core Audio
    /// can't list the devices or can't read an input device's UID or name.
    static func all() throws -> [AudioInput] {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        var status = AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size)
        guard status == noErr else { throw ListError(what: "device count", status: status) }
        var ids = [AudioDeviceID](repeating: 0, count: Int(size) / MemoryLayout<AudioDeviceID>.size)
        status = AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &ids)
        guard status == noErr else { throw ListError(what: "device list", status: status) }
        return try ids.compactMap { id in
            guard try inputChannels(id) > 0 else { return nil }
            return AudioInput(id: id, uid: try readString(id, kAudioDevicePropertyDeviceUID, "device UID"),
                              name: try readString(id, kAudioObjectPropertyName, "device name"))
        }
    }

    /// The system default input's name, for "Default (…)".
    static func defaultName() -> String? {
        defaultID().flatMap { string($0, kAudioObjectPropertyName) }
    }

    /// The system default input device.
    static func defaultID() -> AudioDeviceID? {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                              mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var id = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &addr, 0, nil, &size, &id) == noErr,
              id != 0 else { return nil }
        return id
    }

    private static func inputChannels(_ id: AudioDeviceID) throws -> Int {
        var addr = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration,
                                              mScope: kAudioDevicePropertyScopeInput,
                                              mElement: kAudioObjectPropertyElementMain)
        var size: UInt32 = 0
        var status = AudioObjectGetPropertyDataSize(id, &addr, 0, nil, &size)
        guard status == noErr else { throw ListError(what: "input stream size", status: status) }
        guard size > 0 else { return 0 }
        let raw = UnsafeMutableRawPointer.allocate(byteCount: Int(size), alignment: MemoryLayout<AudioBufferList>.alignment)
        defer { raw.deallocate() }
        status = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, raw)
        guard status == noErr else { throw ListError(what: "input stream configuration", status: status) }
        let list = UnsafeMutableAudioBufferListPointer(raw.assumingMemoryBound(to: AudioBufferList.self))
        return list.reduce(0) { $0 + Int($1.mNumberChannels) }
    }

    private static func string(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector) -> String? {
        var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value) == noErr, let v = value else { return nil }
        return v.takeRetainedValue() as String
    }

    private static func readString(_ id: AudioDeviceID, _ selector: AudioObjectPropertySelector,
                                   _ what: String) throws -> String {
        var addr = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal,
                                              mElement: kAudioObjectPropertyElementMain)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        let status = AudioObjectGetPropertyData(id, &addr, 0, nil, &size, &value)
        guard status == noErr else { throw ListError(what: what, status: status) }
        guard let v = value else { throw ListError(what: what, status: kAudioHardwareUnspecifiedError) }
        return v.takeRetainedValue() as String
    }
}

/// Mic capture: the system default input unless Settings picked one.
/// Hands mono f32 to the engine and the raw buffer to ShazamKit.
final class MicCapture {
    private let engine = AVAudioEngine()
    var onSamples: (([Float], AVAudioPCMBuffer, AVAudioTime) -> Void)?
    var onLevel: ((Float) -> Void)?
    /// Called on the main queue when the system default input changes.
    var onDefaultInputChange: (() -> Void)?
    private var running = false
    private var defaultAddr = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDefaultInputDevice,
                                                         mScope: kAudioObjectPropertyScopeGlobal,
                                                         mElement: kAudioObjectPropertyElementMain)
    private var defaultListener: AudioObjectPropertyListenerBlock?
    /// Called on the main queue when AVAudioEngine stopped itself because its
    /// input changed under it (a picked mic unplugged, a new rate or channel
    /// count). Capture is dead until restarted.
    var onEngineStopped: (() -> Void)?
    private var configObserver: NSObjectProtocol?

    init() {
        let block: AudioObjectPropertyListenerBlock = { [weak self] _, _ in self?.onDefaultInputChange?() }
        if AudioObjectAddPropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &defaultAddr, .main, block) == noErr {
            defaultListener = block
        }
        configObserver = NotificationCenter.default.addObserver(
            forName: .AVAudioEngineConfigurationChange, object: engine, queue: .main
        ) { [weak self] _ in
            // A change we caused while restarting leaves the engine running;
            // only a self-stop means capture died.
            guard let self, self.running, !self.engine.isRunning else { return }
            self.onEngineStopped?()
        }
    }

    deinit {
        if let configObserver { NotificationCenter.default.removeObserver(configObserver) }
        if let block = defaultListener {
            AudioObjectRemovePropertyListenerBlock(AudioObjectID(kAudioObjectSystemObject), &defaultAddr, .main, block)
        }
    }

    /// Starts capture and returns the sample rate the engine will see.
    func start(uid: String?) throws -> Double {
        stop()
        try apply(uid: uid)
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw NSError(domain: "TrackCatcher", code: 1, userInfo: [NSLocalizedDescriptionKey: "the input has no channels"])
        }
        input.installTap(onBus: 0, bufferSize: 4096, format: format) { [weak self] buffer, time in
            guard let self, let ch = buffer.floatChannelData else { return }
            let n = Int(buffer.frameLength), chans = Int(buffer.format.channelCount)
            var mono = [Float](repeating: 0, count: n)
            var sum: Float = 0
            for i in 0..<n {
                var v: Float = 0
                for c in 0..<chans { v += ch[c][i] }
                v /= Float(chans)
                mono[i] = v
                sum += v * v
            }
            self.onLevel?(n > 0 ? (sum / Float(n)).squareRoot() : 0)
            self.onSamples?(mono, buffer, time)
        }
        engine.prepare()
        do { try engine.start() } catch {
            // The tap is already on the bus; leave it there and the next
            // start installs a second one, which AVAudioEngine asserts on.
            input.removeTap(onBus: 0)
            throw error
        }
        running = true
        return format.sampleRate
    }

    func stop() {
        guard running else { return }
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        running = false
    }

    enum BindError: LocalizedError {
        case noDevice(String?), rejected(OSStatus)
        var errorDescription: String? {
            switch self {
            case .noDevice(let uid): return uid == nil ? "no default input device" : "the selected input is gone"
            case .rejected(let status): return "Core Audio refused the input (\(status))"
            }
        }
    }

    private func apply(uid: String?) throws {
        // nil means Default: the engine and its input unit are reused across
        // restarts, so a device picked earlier stays bound unless we set the
        // current default explicitly. A device we can't bind is an error,
        // never a silent fall back to whatever was bound before.
        let target = try uid.map { u in try AudioInput.all().first(where: { $0.uid == u })?.id } ?? AudioInput.defaultID()
        guard let target else { throw BindError.noDevice(uid) }
        guard let unit = engine.inputNode.audioUnit else { throw BindError.rejected(kAudioUnitErr_Uninitialized) }
        var id = target
        let status = AudioUnitSetProperty(unit, kAudioOutputUnitProperty_CurrentDevice, kAudioUnitScope_Global, 0,
                                          &id, UInt32(MemoryLayout<AudioDeviceID>.size))
        guard status == noErr else { throw BindError.rejected(status) }
    }
}

/// Catch: ShazamKit on the buffers the app already captures.
final class ShazamCatcher: NSObject, SHSessionDelegate {
    struct Match {
        let title: String
        let artist: String
        let isrc: String?
        let appleMusicID: String?
    }

    enum CatchError: LocalizedError {
        case noMatch, timedOut, stopped, incompleteMatch
        var errorDescription: String? {
            switch self {
            case .incompleteMatch: return "Shazam matched but sent no title or artist, so nothing was saved."
            case .noMatch: return "Shazam didn't recognize it. Try again in a louder part."
            case .timedOut: return "Nothing recognized in 15 s."
            case .stopped: return "Listening stopped before a match. Catch again once listening."
            }
        }
    }

    private var session: SHSession?
    private var done: ((Result<Match, Error>) -> Void)?
    private let queue = DispatchQueue(label: "trackcatcher.shazam")
    /// Bumped per catch so an earlier catch's timeout can't end a later one.
    private var generation = 0

    func catchOnce(timeout: TimeInterval, done: @escaping (Result<Match, Error>) -> Void) {
        queue.async {
            self.generation += 1
            let mine = self.generation
            let s = SHSession()
            s.delegate = self
            self.session = s
            self.done = done
            self.queue.asyncAfter(deadline: .now() + timeout) {
                guard self.generation == mine else { return }
                self.finish(.failure(CatchError.timedOut))
            }
        }
    }

    /// Ends the catch in flight, if any: audio after a stop or an input
    /// change must never complete a catch begun before it, with the key and
    /// BPM captured then. Bumping the generation also voids its timeout.
    func cancel() {
        queue.async {
            self.generation += 1
            self.finish(.failure(CatchError.stopped))
        }
    }

    /// Called from the audio thread with every captured buffer.
    /// The tap's buffer belongs to AVAudioEngine and is reused once the tap
    /// returns, so the queued match gets its own copy.
    func feed(_ buffer: AVAudioPCMBuffer, at time: AVAudioTime) {
        guard let copy = Self.copy(buffer) else { return }
        queue.async { self.session?.matchStreamingBuffer(copy, at: time) }
    }

    static func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
        guard let out = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else { return nil }
        out.frameLength = buffer.frameLength
        let src = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: buffer.audioBufferList))
        let dst = UnsafeMutableAudioBufferListPointer(out.mutableAudioBufferList)
        for (s, d) in zip(src, dst) {
            guard let from = s.mData, let to = d.mData else { return nil }
            memcpy(to, from, Int(min(s.mDataByteSize, d.mDataByteSize)))
        }
        return out
    }

    func session(_ session: SHSession, didFind match: SHMatch) {
        guard let item = match.mediaItems.first else { return }
        queue.async {
            // A late answer from an earlier catch's session is not this catch's.
            guard session === self.session else { return }
            // A match without a title or artist is a provider contract
            // failure, not a track: it ends the catch with that error and
            // writes nothing to the Inbox.
            guard let title = item.title, !title.isEmpty, let artist = item.artist, !artist.isEmpty else {
                self.finish(.failure(CatchError.incompleteMatch))
                return
            }
            self.finish(.success(Match(title: title, artist: artist, isrc: item.isrc, appleMusicID: item.appleMusicID)))
        }
    }

    func session(_ session: SHSession, didNotFindMatchFor signature: SHSignature, error: Error?) {
        // No error is a plain no-match: streaming keeps trying until the
        // timeout. A failed match attempt (network) is the one error worth
        // retrying; anything else, such as a bad audio format or signature,
        // ends the catch now with that error instead of a fake "nothing
        // recognized".
        guard let error else { return }
        NSLog("ShazamKit: \(error)")
        if let sh = error as? SHError, sh.code == .matchAttemptFailed { return }
        queue.async {
            guard session === self.session else { return }
            self.finish(.failure(error))
        }
    }

    private func finish(_ r: Result<Match, Error>) {
        guard let d = done else { return }
        done = nil
        session = nil
        d(r)
    }
}

/// Now playing via Apple Events. One macOS prompt per player, the first time.
/// NSAppleScript is main-thread-only, so the scripts run on the main queue
/// (each is a single short property read of a player that is already running).
enum NowPlayingReader {
    static func read(_ players: [NowPlaying.Player], done: @escaping (NowPlaying.Track?, String?) -> Void) {
        DispatchQueue.main.async {
            var problems: [String] = []
            for p in players {
                // No Apple Event (and no Automation prompt, which would block
                // this main-queue read) for a player that is not running.
                guard !NSRunningApplication.runningApplications(withBundleIdentifier: p.bundleID).isEmpty else { continue }
                var err: NSDictionary?
                guard let script = NSAppleScript(source: NowPlaying.script(for: p)) else { continue }
                let out = script.executeAndReturnError(&err)
                if let err {
                    let n = err[NSAppleScript.errorNumber] as? Int ?? 0
                    problems.append(n == -1743 ? "\(p.rawValue): allow Track Catcher in Automation settings" : "\(p.rawValue): \(err[NSAppleScript.errorMessage] ?? n)")
                    continue
                }
                if let t = NowPlaying.parse(out.stringValue ?? "", player: p) { return done(t, nil) }
            }
            done(nil, problems.isEmpty ? nil : problems.joined(separator: "; "))
        }
    }
}
