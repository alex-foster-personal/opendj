import AppKit
import AVFoundation
import Combine
import ServiceManagement
import TrackCatcherCore

/// Mic permission as the window shows it.
enum MicState: Equatable {
    case firstLaunch, notAsked, granted, denied, silent
}

/// Everything the window shows, and every action behind a button. Views read
/// this; the CLI and the buttons call the same methods.
@MainActor
final class AppModel: ObservableObject {
    // Readout
    @Published var chromaFast = [Double](repeating: 0, count: 12)
    @Published var keyScores: [EngineKeyScore] = []
    @Published var hold = KeyHold()
    @Published var confidence: Double?
    @Published var margin: Double?
    @Published var bpm: Double?
    @Published var chord: String?
    @Published var history: [MusicalKey] = []
    @Published var tap = TapTempo()

    // View state
    @Published var mic: MicState = .firstLaunch
    @Published var listening = false
    @Published var gateStep = 1
    @Published var showKeys: Bool { didSet { defaults.set(showKeys, forKey: "showKeys") } }
    @Published var camelotRing: Bool { didSet { defaults.set(camelotRing, forKey: "camelotRing") } }
    @Published var compact = false
    @Published var settingsOpen = false
    @Published var splitMenu: CaptureKind?
    @Published var caught: (title: String, detail: String)?
    @Published var toast: String?
    @Published var drop: DropState = .idle
    @Published var level: Float = 0
    @Published var noMicTries = 0

    // Settings, remembered
    @Published var keepOnTop: Bool { didSet { defaults.set(keepOnTop, forKey: "keepOnTop"); onKeepOnTop?(keepOnTop) } }
    @Published var launchAtLogin: Bool { didSet { if !syncingLogin { setLaunchAtLogin(launchAtLogin) } } }
    private var syncingLogin = false
    @Published var keepRunning: Bool { didSet { defaults.set(keepRunning, forKey: "keepRunning") } }
    @Published var nowPlayingSpotify: Bool { didSet { defaults.set(nowPlayingSpotify, forKey: "npSpotify") } }
    @Published var nowPlayingMusic: Bool { didSet { defaults.set(nowPlayingMusic, forKey: "npMusic") } }
    @Published var inputUID: String? { didSet { defaults.set(inputUID, forKey: "inputUID"); restartForNewInput() } }
    @Published var inputs: [AudioInput] = []
    /// Why the input list couldn't be read, shown in Settings; nil when it was.
    @Published var inputsError: String?

    enum CaptureKind: String { case catchTrack = "catch", nowPlaying = "nowp" }
    enum DropState: Equatable { case idle, over, analyzing(name: String, progress: Double), done(String), failed(String) }

    let defaults: UserDefaults
    let inbox: Inbox
    let capture = MicCapture()
    let shazam = ShazamCatcher()
    var engine: LiveEngine?
    /// What the audio tap feeds. The tap outlives an engine restart, so it
    /// reads the current engine here rather than capturing one.
    private let feed = EngineSlot()
    private var captureRate: Double = 0
    /// Bumped whenever the analysis a capture snapshotted stops being live:
    /// the analyzer falling behind and restarting, or listening stopping
    /// for any reason (Stop, a dead engine, an input change, silence). A
    /// capture that spans a bump stores no key or BPM.
    private var analysisEpoch = 0
    var onKeepOnTop: ((Bool) -> Void)?
    private var lastHistoryAt = -1.0
    private var dropGeneration = 0
    private var silentSince: Date?

    /// The app passes the real stores; `--snapshot` passes throwaway ones so
    /// evidence runs never touch the user's preferences or Inbox.
    init(defaults: UserDefaults = .standard, inbox: Inbox = .standard()) {
        self.defaults = defaults
        self.inbox = inbox
        let d = defaults
        d.register(defaults: ["keepOnTop": true, "keepRunning": true, "npSpotify": true, "npMusic": true, "camelotRing": true])
        showKeys = d.bool(forKey: "showKeys")
        camelotRing = d.bool(forKey: "camelotRing")
        keepOnTop = d.bool(forKey: "keepOnTop")
        launchAtLogin = SMAppService.mainApp.status == .enabled
        keepRunning = d.bool(forKey: "keepRunning")
        nowPlayingSpotify = d.bool(forKey: "npSpotify")
        nowPlayingMusic = d.bool(forKey: "npMusic")
        inputUID = d.string(forKey: "inputUID")
        refreshInputs()
        capture.onLevel = { [weak self] rms in
            Task { @MainActor in self?.noteLevel(rms) }
        }
        // Default follows the system input: a new default (AirPods plugged
        // in) relaunches capture on it, at its own sample rate.
        capture.onDefaultInputChange = { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.refreshInputs()
                if self.inputUID == nil { self.restartForNewInput() }
            }
        }
        // Any input, picked or default: an engine that stopped itself is
        // relaunched, and if the input is gone startListening says so.
        capture.onEngineStopped = { [weak self] in
            Task { @MainActor in
                guard let self else { return }
                self.refreshInputs()
                self.restartForNewInput()
            }
        }
    }

    // MARK: Setup and listening

    /// On launch: skip setup entirely when the mic is already granted (the
    /// grant is remembered because every build is signed with one identity).
    func launch() {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: mic = .granted; startListening()
        case .denied, .restricted: mic = .denied; listening = true
        case .notDetermined: mic = defaults.bool(forKey: "setupSeen") ? .notAsked : .firstLaunch
        @unknown default: mic = .notAsked
        }
    }

    func gateNext() {
        if gateStep == 1 { gateStep = 2; defaults.set(true, forKey: "setupSeen"); return }
        AVCaptureDevice.requestAccess(for: .audio) { ok in
            Task { @MainActor in
                self.mic = ok ? .granted : .denied
                if ok { self.startListening() } else { self.listening = true }
            }
        }
    }

    func gateLater() { listening = false; mic = .notAsked }

    func runSetupAgain() {
        settingsOpen = false
        stopListening()
        gateStep = 1
        mic = AVCaptureDevice.authorizationStatus(for: .audio) == .authorized ? .notAsked : .firstLaunch
        listening = true
    }

    /// The red banner's button: first press asks again, then opens Settings.
    /// The design's no-mic flow: the first Try again asks macOS again; when
    /// no grant comes back (a denied mic gets no second prompt) the button
    /// becomes "Open Microphone settings", and that opens Privacy › Microphone.
    func noMicRetry() {
        if mic == .firstLaunch { gateStep = 1; mic = .notAsked; listening = true; return }
        if noMicTries == 0 {
            AVCaptureDevice.requestAccess(for: .audio) { granted in
                Task { @MainActor [weak self] in
                    guard let self else { return }
                    if granted { self.mic = .granted; self.startListening() } else { self.noMicTries = 1 }
                }
            }
            return
        }
        noMicTries += 1
        if let url = URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone") {
            NSWorkspace.shared.open(url)
        }
    }

    func toggleListening() { listening ? stopListening() : startListening() }

    func startListening() {
        let status = AVCaptureDevice.authorizationStatus(for: .audio)
        guard status == .authorized else {
            // From the live status, not `mic`: a grant revoked while running
            // still reads .granted there.
            mic = status == .denied || status == .restricted ? .denied : .notAsked
            listening = true
            return
        }
        // Each odj-live process counts t from zero, so the history clock and
        // the key hold start over with it. Silence is timed per session too.
        lastHistoryAt = -1.0
        silentSince = nil
        // Nothing from the demo or an earlier session (history, chroma, BPM,
        // chord) carries into this one.
        clearReadout()
        do {
            let exe = try EngineLocator.find()
            let rate = try capture.start(uid: inputUID)
            captureRate = rate
            try startEngine(exe)
            // The audio thread never touches the main actor or the pipe:
            // enqueue hands samples to the engine's writer queue.
            let shazam = self.shazam, feed = self.feed
            capture.onSamples = { samples, buffer, time in
                feed.current?.enqueue(samples)
                shazam.feed(buffer, at: time)
            }
            listening = true
            if mic != .silent { mic = .granted }
        } catch {
            // Capture may already be running if only the engine failed.
            capture.stop()
            engine = nil
            feed.current = nil
            showToast("Can't listen: \(error)")
            listening = false
            level = 0
        }
    }

    /// One odj-live process for the current capture rate, made current.
    private func startEngine(_ exe: URL) throws {
        let e = LiveEngine(executable: exe, sampleRate: Int(captureRate), hz: 10)
        // A frame queued by a superseded engine must not land on the new
        // session's key hold or history clock.
        e.onFrame = { [weak self, weak e] f in
            Task { @MainActor in
                guard let self, let e, self.engine === e else { return }
                self.apply(f)
            }
        }
        // An engine that dies or speaks garbage must not leave a stale
        // key on screen under a live LISTENING pill.
        // Weak on both sides: the engine holds these closures.
        e.onError = { [weak self, weak e] error in
            NSLog("odj-live: \(error)")
            Task { @MainActor in if let e { self?.engineFailed(e, "\(error)") } }
        }
        e.onExit = { [weak self, weak e] status in
            Task { @MainActor in if let e { self?.engineFailed(e, "exited with status \(status)") } }
        }
        // A dropped buffer means the engine fell behind: what it reports
        // now is seconds-old backlog with a hole in it, while Shazam hears
        // the present. Start a fresh engine on current audio instead.
        e.onDrop = { [weak self, weak e] in
            Task { @MainActor in if let e { self?.engineFellBehind(e) } }
        }
        try e.start()
        engine = e
        feed.current = e
    }

    /// Capture and a catch in flight carry on; only the analysis restarts,
    /// so no key or BPM heard before the drop reaches the readout or a catch.
    private func engineFellBehind(_ e: LiveEngine) {
        guard listening, engine === e else { return }
        NSLog("odj-live fell behind (\(e.dropped) buffers dropped); restarting it")
        feed.current = nil
        engine = nil
        analysisEpoch += 1
        e.stop()
        // A tempo the user tapped is still this track's; the engine's is not.
        let tapped = tap
        clearReadout()
        tap = tapped
        lastHistoryAt = -1.0
        do { try startEngine(try EngineLocator.find()) } catch {
            stopListening()
            showToast("Analyzer stopped (\(error)). Click Listen to restart.")
        }
    }

    func stopListening() {
        silentSince = nil
        analysisEpoch += 1
        // A catch in flight belongs to this capture; a restart starts clean.
        shazam.cancel()
        capture.stop()
        feed.current = nil
        engine?.stop()
        engine = nil
        listening = false
        level = 0
    }

    /// Only the engine currently listening counts; one already stopped
    /// on purpose has been cleared from `engine` before its exit lands.
    private func engineFailed(_ e: LiveEngine, _ why: String) {
        guard listening, engine === e else { return }
        stopListening()
        clearReadout()
        showToast("Analyzer stopped (\(why)). Click Listen to restart.")
    }

    /// Nothing the dead engine said stays on screen, and a tempo tapped
    /// against an earlier session's track does not override the new one.
    private func clearReadout() {
        tap = TapTempo()
        chromaFast = [Double](repeating: 0, count: 12)
        keyScores = []
        hold = KeyHold()
        confidence = nil
        margin = nil
        bpm = nil
        chord = nil
        history = []
    }

    /// A new input can run at a different sample rate, and odj-live takes
    /// its rate once at launch, so a device switch relaunches both.
    private func restartForNewInput() {
        guard listening, engine != nil else { return }
        stopListening()
        startListening()
    }

    private func noteLevel(_ rms: Float) {
        // A tap callback already in flight when listening stops must not restore
        // a meter reading after stopListening() has reset it to zero.
        guard listening, mic == .granted || mic == .silent else { return }
        level = rms
        // About -60 dBFS for 4 s straight reads as "can't hear anything".
        if rms < 0.001 {
            if silentSince == nil { silentSince = Date() }
            if let s = silentSince, Date().timeIntervalSince(s) > 4, mic != .silent {
                // A catch or Now playing read in flight was given the key
                // and BPM heard before the silence; whatever plays next is
                // another track.
                shazam.cancel()
                analysisEpoch += 1
                mic = .silent
            }
        } else {
            silentSince = nil
            // Sound again may be a different track. The engine's tempo window
            // (12 s of onsets) and slow chroma still hold the one before the
            // silence, so start a fresh engine: only post-silence audio then
            // reaches the readout or a catch.
            // The restart runs while the input is still gated (.silent), so
            // heardNow reads nothing and no frame from the old engine can
            // refill the readout. Only then does the input count as granted.
            if mic == .silent {
                clearReadout()
                restartForNewInput()
                mic = .granted
            }
        }
    }

    // MARK: Engine frames

    func apply(_ f: EngineFrame) {
        // While silent the last heard readout stays on screen (TCAPP-6577b); the
        // analyzer keeps decaying, so its frames must not replace it.
        guard mic != .silent else { return }
        chromaFast = f.chromaFast
        keyScores = f.keys
        confidence = f.confidence
        margin = f.margin
        bpm = f.bpm
        hold.update(top: f.key.flatMap(MusicalKey.init(camelot:)), at: f.t)
        chord = AppModel.chord(from: f.chroma)
        if let shown = hold.shown, f.t - lastHistoryAt >= 1 {
            lastHistoryAt = f.t
            history.append(shown)
            if history.count > 120 { history.removeFirst(history.count - 120) }
        }
    }

    /// The strongest major or minor triad in one chroma frame: "E", "C♯m".
    static func chord(from c: [Double]) -> String? {
        guard c.count == 12, c.contains(where: { $0 > 0.05 }) else { return nil }
        var best: (Double, Int, Bool) = (-1, 0, false)
        for root in 0..<12 {
            for minor in [false, true] {
                let s = c[root] + c[(root + (minor ? 3 : 4)) % 12] + c[(root + 7) % 12]
                if s > best.0 { best = (s, root, minor) }
            }
        }
        return MusicalKey.notes[best.1] + (best.2 ? "m" : "")
    }

    var shownBpm: Int? {
        if let t = tap.bpm { return Int(t.rounded()) }
        return bpm.map { Int($0.rounded()) }
    }

    /// Key and BPM to store with a catch: only what the live mic engine is
    /// hearing now. Stopped listening keeps an older track's readout and the
    /// first-launch demo shows synthesized values; neither belongs on a new
    /// Inbox entry, so both store nil. A granted input gone silent (muted,
    /// unplugged) leaves the last readout up, which is just as stale.
    var heardNow: (key: String?, bpm: Int?) {
        guard listening, engine != nil, mic == .granted else { return (nil, nil) }
        return (hold.shown?.camelot, shownBpm)
    }

    /// True when no key change is waiting out the six-second hold. A candidate
    /// still in that window is not the readout yet, so the readout alone
    /// cannot say which track is playing: a catch or a Now playing read taken
    /// then stores no analysis.
    var readoutSettled: Bool { hold.pending == nil }

    // MARK: Copy

    func copy(_ text: String) {
        NSPasteboard.general.clearContents()
        NSPasteboard.general.setString(text, forType: .string)
        showToast(CopyText.toast(for: text))
    }

    func copyReadout() {
        guard let k = hold.shown else { return }
        copy(CopyText.readout(key: k, chord: chord, bpm: shownBpm))
    }

    func showToast(_ text: String) {
        toast = text
        Task { @MainActor in
            try? await Task.sleep(nanoseconds: 1_800_000_000)
            if self.toast == text { self.toast = nil }
        }
    }

    func doTap() { tap.tap(at: ProcessInfo.processInfo.systemUptime) }

    // MARK: Capture

    func catchTrack(to playlist: String = "Inbox") {
        splitMenu = nil
        guard listening, mic == .granted else {
            caught = ("Not listening", "Start listening first, then Catch.")
            return
        }
        caught = ("", "Listening to what's playing…")
        let (heardKey, heardBpm) = heardNow
        let epoch = analysisEpoch
        let settledAtStart = readoutSettled
        shazam.catchOnce(timeout: 15) { result in
            Task { @MainActor in
                switch result {
                case .success(let m):
                    // Shazam heard live audio, so the match stands. A gapless
                    // or crossfaded change never silences the input, so the
                    // readout is the only sign the track stayed the same: the
                    // key and BPM are kept only if the analyzer never fell
                    // behind, the readout held through the whole catch, and
                    // no key change was still waiting out the hold at either
                    // end. A late change can reach Shazam's match before the
                    // hold promotes it, so the held readout alone can lie.
                    let (nowKey, nowBpm) = self.heardNow
                    let steady = self.analysisEpoch == epoch && nowKey == heardKey && nowBpm == heardBpm
                        && settledAtStart && self.readoutSettled
                    let (key, bpm) = steady ? (heardKey, heardBpm) : (nil, nil)
                    let entry = CatchEntry(source: .shazam, title: m.title, artist: m.artist, playlist: playlist,
                                           heardKey: key, heardBpm: bpm, isrc: m.isrc, appleMusicID: m.appleMusicID)
                    self.store(entry)
                case .failure(let e):
                    let stopped = (e as? ShazamCatcher.CatchError) == .stopped
                    self.caught = (stopped ? "Catch stopped" : "No match", "\(e.localizedDescription)")
                }
            }
        }
    }

    /// One Now playing read at a time: a double click or a repeated CLI
    /// command while a read is out joins it instead of saving a second entry.
    private var nowPlayingInFlight = false

    func nowPlaying(to playlist: String = "Inbox") {
        splitMenu = nil
        guard !nowPlayingInFlight else { return }
        nowPlayingInFlight = true
        var players: [NowPlaying.Player] = []
        if nowPlayingSpotify { players.append(.spotify) }
        if nowPlayingMusic { players.append(.music) }
        caught = ("", "Reading \(players.map(\.rawValue).joined(separator: " and "))…")
        NowPlayingReader.read(players) { track, problem in
            Task { @MainActor in
                self.nowPlayingInFlight = false
                guard let t = track else {
                    self.caught = ("Nothing playing in Spotify or Music", problem ?? "")
                    return
                }
                // Read when the player answers, not when asked: an Automation
                // prompt or a slow Apple Event can let the track change in
                // between. A stop, silence or analyzer restart since has
                // already cleared the live readout, so this stores nil then.
                let (key, bpm) = self.readoutSettled ? self.heardNow : (nil, nil)
                let entry = CatchEntry(source: t.player.source, title: t.title, artist: t.artist, playlist: playlist,
                                       heardKey: key, heardBpm: bpm,
                                       appleMusicID: t.player == .music ? t.id : nil,
                                       spotifyID: t.player == .spotify ? t.id : nil)
                self.store(entry)
            }
        }
    }

    private func store(_ entry: CatchEntry) {
        do {
            try inbox.append(entry)
            caught = ("\(entry.artist) · \(entry.title)", entry.resultLine)
        } catch {
            caught = ("\(entry.artist) · \(entry.title)", "Not saved: \(error.localizedDescription)")
        }
    }

    /// The ▾ menus' playlists, or why the Inbox couldn't be read (a damaged
    /// line is shown, never replaced with an empty history).
    var playlists: Result<[String], Error> { Result { try inbox.recentPlaylists() } }

    /// "Caught 13 Oct" for New playlist…
    static func newPlaylistName(_ date: Date = Date()) -> String {
        let f = DateFormatter()
        f.dateFormat = "d MMM"
        return "Caught " + f.string(from: date)
    }

    // MARK: Drop a track

    private let dropAnalysis = AnalysisSlot()

    func analyze(_ url: URL) {
        // A newer drop wins: an older analysis still running can't overwrite
        // it, and its odj-live process is stopped so it stops using the CPU.
        dropGeneration += 1
        let mine = dropGeneration
        dropAnalysis.begin(mine)
        drop = .analyzing(name: url.lastPathComponent, progress: 0)
        let duration = (try? AVAudioFile(forReading: url)).map { Double($0.length) / $0.processingFormat.sampleRate }
        let publish: @Sendable (DropState) -> Void = { state in
            DispatchQueue.main.async { if self.dropGeneration == mine { self.drop = state } }
        }
        DispatchQueue.global(qos: .userInitiated).async {
            do {
                let exe = try EngineLocator.find()
                let slot = self.dropAnalysis
                let s = try FileAnalysis.run(executable: exe, file: url, duration: duration,
                                             started: { p in slot.adopt(p, generation: mine) }) { p in
                    publish(.analyzing(name: url.lastPathComponent, progress: p))
                }
                publish(.done(CopyText.trackResult(s)))
            } catch {
                publish(.failed("\(error)"))
            }
        }
    }

    // MARK: Settings

    private func setLaunchAtLogin(_ on: Bool) {
        do {
            if on { try SMAppService.mainApp.register() } else { try SMAppService.mainApp.unregister() }
        } catch {
            showToast("Launch at login: \(error.localizedDescription)")
            // Show what the system actually has, not what was asked for.
            syncingLogin = true
            launchAtLogin = SMAppService.mainApp.status == .enabled
            syncingLogin = false
        }
    }

    func refreshInputs() {
        do {
            inputs = try AudioInput.all()
            inputsError = nil
        } catch {
            inputs = []
            inputsError = error.localizedDescription
        }
    }

    var micNote: String {
        if mic == .silent { return "This input is silent. Pick another, or use System default." }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: return "Mic access granted. Remembered across updates."
        case .denied, .restricted: return "No mic access. Turn on Track Catcher in Microphone settings."
        default: return "Mic access not asked yet."
        }
    }

    // MARK: Commands from the CLI

    func perform(_ command: AppCommand, playlist: String?, key: String? = nil) {
        switch command {
        case .catchTrack: catchTrack(to: playlist ?? "Inbox")
        case .nowPlaying: nowPlaying(to: playlist ?? "Inbox")
        case .listen: startListening()
        case .stop: stopListening()
        case .tap: doTap()
        case .copy: copyReadout()
        case .settings:
            // Same as the cog: a device plugged in since launch is listed.
            if !settingsOpen { refreshInputs() }
            settingsOpen.toggle()
        case .setup: runSetupAgain()
        case .quit: NSApp.terminate(nil)
        case .compact: compact.toggle()
        case .pin: keepOnTop.toggle()
        case .half: tap.halve()
        case .double: tap.double()
        case .bars: showKeys.toggle()
        case .ring: camelotRing.toggle()
        case .copyNotes: copy(CopyText.notesHeard(chromaFast))
        case .copyWheel:
            if let k = hold.shown { copy(CopyText.asciiWheel(current: k)) } else { showToast("No key yet") }
        case .copyKey:
            if let k = key.flatMap(MusicalKey.init(camelot:)) { copy(CopyText.wheelKey(k)) } else { showToast("copy-key needs --key like 9A") }
        }
    }
}

/// The one drop analysis worth running. A new drop terminates the previous
/// odj-live process; a process from an older drop that starts after that is
/// terminated at once instead of taking the slot.
final class AnalysisSlot: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    private var generation = 0

    func begin(_ gen: Int) {
        lock.lock()
        generation = gen
        let old = process
        process = nil
        lock.unlock()
        if let old, old.isRunning { old.terminate() }
    }

    func adopt(_ p: Process, generation gen: Int) {
        lock.lock()
        let current = gen == generation
        if current { process = p }
        lock.unlock()
        if !current { p.terminate() }
    }
}

/// The engine the audio tap feeds, swapped from the main actor and read on
/// the audio thread.
private final class EngineSlot: @unchecked Sendable {
    private let lock = NSLock()
    private var engine: LiveEngine?
    var current: LiveEngine? {
        get { lock.lock(); defer { lock.unlock() }; return engine }
        set { lock.lock(); engine = newValue; lock.unlock() }
    }
}
