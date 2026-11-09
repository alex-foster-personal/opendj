import AppKit
import SwiftUI
import TrackCatcherCore

/// App entry: one floating window, a menu bar readout, CLI commands over a
/// distributed notification, and `--snapshot DIR` for screenshot evidence.
@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model: AppModel
    var window: NSWindow!
    var status: NSStatusItem?
    private var demo: DemoFeed?
    private var observers: [Any] = []
    /// `--snapshot DIR`: the output folder, plus a one-off defaults domain
    /// deleted at exit, so a snapshot run leaves the user's data alone.
    private let snapshotDir: URL?
    private let snapshotSuite: String?

    override init() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--snapshot"), i + 1 < args.count {
            let dir = URL(fileURLWithPath: args[i + 1])
            let suite = "com.opendj.trackcatcher.snapshot.\(UUID().uuidString)"
            // No fallback to the real defaults: a snapshot that can't isolate
            // itself exits nonzero, which the snapshot gate reports.
            guard let defaults = UserDefaults(suiteName: suite) else { fatalError("snapshot: no defaults suite \(suite)") }
            snapshotDir = dir
            snapshotSuite = suite
            model = AppModel(defaults: defaults, inbox: Inbox(url: dir.appendingPathComponent("snapshot-inbox.jsonl")))
        } else {
            snapshotDir = nil
            snapshotSuite = nil
            model = AppModel()
        }
        super.init()
    }

    func applicationWillTerminate(_ note: Notification) {
        if let suite = snapshotSuite { UserDefaults.standard.removePersistentDomain(forName: suite) }
    }

    func applicationDidFinishLaunching(_ note: Notification) {
        window = makeWindow()
        model.onKeepOnTop = { [weak self] on in self?.window.level = on ? .floating : .normal }
        window.level = model.keepOnTop ? .floating : .normal
        // The hosting view can't resize a plain NSWindow, so compact mode
        // sets the window's size to what the content now wants.
        observers.append(model.$compact.dropFirst().sink { [weak self] _ in
            DispatchQueue.main.async {
                guard let w = self?.window, let v = w.contentView else { return }
                w.setContentSize(v.fittingSize)
            }
        })

        if let dir = snapshotDir {
            Snapshotter(model: model, window: window).run(into: dir)
            return
        }

        makeStatusItem()
        observeCommands()
        observeOpenDJ()
        model.launch()
        // First launch has no audio: preview the readout on the demo signal,
        // through the real engine, under the NO AUDIO banner. After Later
        // (not asked, not listening) it stays off on every relaunch; a
        // launch already denied keeps the demo off as before.
        if model.mic != .denied && AppDelegate.showsDemo(model.mic, listening: model.listening) { startDemo() }
        // The demo runs only behind the setup overlay or the NO AUDIO
        // banner. Granting access, or choosing Later (NOT LISTENING with no
        // overlay), ends it so the readout never moves without a source.
        observers.append(model.$mic.combineLatest(model.$listening).sink { [weak self] m, listening in
            if !AppDelegate.showsDemo(m, listening: listening) { self?.demo?.stop(); self?.demo = nil }
        })
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    /// The demo runs only behind the setup overlay or the NO AUDIO banner;
    /// launch and the observer share this so they can't disagree.
    nonisolated static func showsDemo(_ mic: MicState, listening: Bool) -> Bool {
        mic == .firstLaunch || mic == .denied || (mic == .notAsked && listening)
    }

    // Closing the window leaves the menu bar item; "Keep running after Open
    // DJ quits" is about Open DJ, so it is checked when Open DJ quits.
    func applicationShouldTerminateAfterLastWindowClosed(_ app: NSApplication) -> Bool { false }

    /// Open DJ's bundle id is com.opendj.desktop; dev lanes add a suffix
    /// (com.opendj.desktop.electron).
    nonisolated static func isOpenDJ(_ bundleID: String?) -> Bool {
        guard let id = bundleID else { return false }
        return id == "com.opendj.desktop" || id.hasPrefix("com.opendj.desktop.")
    }

    private func observeOpenDJ() {
        let ws = NSWorkspace.shared.notificationCenter
        observers.append(ws.addObserver(forName: NSWorkspace.didTerminateApplicationNotification, object: nil,
                                        queue: .main) { [weak self] note in
            let app = note.userInfo?[NSWorkspace.applicationUserInfoKey] as? NSRunningApplication
            guard AppDelegate.isOpenDJ(app?.bundleIdentifier) else { return }
            // The inner capture list copies the weak reference, so the Task
            // never touches the outer closure's mutable `self` (Swift 5.10
            // rejects that as a concurrent capture).
            Task { @MainActor [weak self] in
                guard let self, !self.model.keepRunning else { return }
                NSApp.terminate(nil)
            }
        })
    }

    private func makeWindow() -> NSWindow {
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 830, height: 520),
                         styleMask: [.titled, .closable, .miniaturizable, .fullSizeContentView],
                         backing: .buffered, defer: false)
        w.titleVisibility = .hidden
        w.titlebarAppearsTransparent = true
        // An empty compact toolbar makes the title bar about as tall as the
        // design's (41 pt), so the real traffic lights sit centered in it
        // beside the brand instead of above it.
        let toolbar = NSToolbar(identifier: "TrackCatcher")
        toolbar.showsBaselineSeparator = false
        w.toolbar = toolbar
        w.toolbarStyle = .unifiedCompact
        w.isMovableByWindowBackground = true
        w.backgroundColor = NSColor(Theme.bg)
        w.title = "Open DJ Track Catcher"
        let host = EdgeToEdgeHostingView(rootView: ContentView(model: model))
        // No preferredContentSize sizing: it collapsed the window to 0x0 when a saved frame was restored.
        // The window is not resizable, so it takes exactly the content's size in every state.
        host.sizingOptions = [.intrinsicContentSize]
        w.contentView = host
        w.center()
        w.setFrameAutosaveName("TrackCatcherWindow")
        // A saved frame can be unusable: a 0x0 frame (seen after a run whose content had not laid out),
        // or one on a display that is no longer attached. Either way the window never draws, so fall back
        // to the design size, centred on the main screen.
        let onSomeScreen = NSScreen.screens.contains { $0.visibleFrame.intersects(w.frame) }
        let usable = w.frame.width >= 300 && w.frame.height >= 200 && onSomeScreen
        if !usable {
            w.setContentSize(NSSize(width: 830, height: 520))
            w.center()
        }
        return w
    }

    private func makeStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.font = .monospacedSystemFont(ofSize: 12, weight: .regular)
        item.button?.title = "♪ -"
        item.button?.target = self
        item.button?.action = #selector(showWindow)
        status = item
        // A tapped tempo wins over the heard one (as in shownBpm), so tap
        // changes refresh too. @Published emits before the property is set,
        // so the title uses the emitted values, not the model's. Stopped or
        // silent capture keeps the window's readout but blanks the menu bar,
        // which claims "heard now".
        let readout = model.$hold.combineLatest(model.$bpm, model.$tap)
        observers.append(readout.combineLatest(model.$listening, model.$mic).sink { [weak self] values, listening, mic in
            guard let self else { return }
            let (hold, heard, tap) = values
            guard listening, mic == .granted else {
                self.status?.button?.title = "♪ -"
                self.status?.button?.toolTip = "Not listening: no key or BPM heard now"
                return
            }
            let key = hold.shown?.camelot ?? "-"
            let bpm = (tap.bpm ?? heard).map { " · \(Int($0.rounded()))" } ?? ""
            self.status?.button?.title = "♪ \(key)\(bpm)"
            self.status?.button?.toolTip = "Key (Camelot) and BPM heard now"
        })
    }

    @objc private func showWindow() {
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
    }

    private func observeCommands() {
        let center = DistributedNotificationCenter.default()
        observers.append(center.addObserver(forName: Notification.Name(AppCommand.notificationName), object: nil,
                                            queue: .main) { [weak self] note in
            guard let raw = note.userInfo?["command"] as? String, let cmd = AppCommand(rawValue: raw) else { return }
            let playlist = note.userInfo?["playlist"] as? String
            let key = note.userInfo?["key"] as? String
            Task { @MainActor [weak self] in self?.model.perform(cmd, playlist: playlist, key: key) }
        })
    }

    private func startDemo() {
        do {
            let feed = try DemoFeed(model: model)
            feed.onFailure = { [weak self] why in
                self?.model.showToast("Demo unavailable: the analyzer stopped (\(why))")
            }
            try feed.start()
            demo = feed
        } catch {
            NSLog("demo: \(error)")
            model.showToast("Demo unavailable: the analyzer didn't start (\(error))")
        }
    }
}

/// Plays the demo signal into the real engine at real time.
@MainActor
final class DemoFeed {
    private let engine: LiveEngine
    private var signal = DemoSignal(sampleRate: 22050)
    private var timer: Timer?
    /// False once stopped: frames already queued on the main actor must not
    /// land on the real mic session that replaces the demo.
    private var active = false

    init(model: AppModel) throws {
        engine = LiveEngine(executable: try EngineLocator.find(), sampleRate: 22050, hz: 10)
        engine.onFrame = { [weak self, weak model] f in
            Task { @MainActor [weak self, weak model] in
                guard let self, self.active else { return }
                model?.apply(f)
            }
        }
    }

    /// Called once if the demo's engine dies or speaks garbage after launch.
    var onFailure: ((String) -> Void)?

    func start() throws {
        // A dead engine stops the feed and says so, rather than leaving an
        // empty or frozen demo on screen.
        engine.onExit = { [weak self] status in
            Task { @MainActor [weak self] in self?.fail("exited with status \(status)") }
        }
        engine.onError = { [weak self] error in
            Task { @MainActor [weak self] in self?.fail("\(error)") }
        }
        active = true
        do { try engine.start() } catch { active = false; throw error }
        // Warm up 20 s so the first frame is a working state, then real time.
        // enqueue, not push: a blocking pipe write here would stall the main
        // actor (and the window) until the analyzer drains the warm-up.
        engine.enqueue(signal.next(22050 * 20))
        timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
            Task { @MainActor [weak self] in
                guard let self else { return }
                self.engine.enqueue(self.signal.next(2205))
            }
        }
    }

    private func fail(_ why: String) {
        guard timer != nil else { return }
        stop()
        onFailure?(why)
    }

    func stop() {
        active = false
        timer?.invalidate()
        timer = nil  // before engine.stop(), so its exit isn't read as a failure
        engine.stop()
    }
}

@main
enum TrackCatcherMain {
    @MainActor static func main() {
        let app = NSApplication.shared
        let delegate = AppDelegate()
        app.delegate = delegate
        app.setActivationPolicy(.regular)
        app.run()
    }
}

/// The window's own title row is drawn by ContentView, so the title bar's
/// safe area must not pad it down or add to the window's height.
final class EdgeToEdgeHostingView<Content: View>: NSHostingView<Content> {
    override var safeAreaInsets: NSEdgeInsets { NSEdgeInsetsZero }
}
