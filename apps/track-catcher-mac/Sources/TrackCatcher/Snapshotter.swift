import AppKit
import SwiftUI
import TrackCatcherCore

/// `TrackCatcher --snapshot DIR`: drive the real window through every state
/// the design shows and save each as a PNG, twice: the window's own render
/// (`<name>.png`, no permission needed) and `screencapture -l` of the window
/// (`<name>-screen.png`, needs Screen Recording; skipped with a note if it
/// fails). The readout comes from the real engine on the demo signal.
@MainActor
struct Snapshotter {
    let model: AppModel
    let window: NSWindow

    func run(into dir: URL) {
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        var log: [String] = [] {
            // Written as it grows, so a run that hangs still says how far it got.
            didSet { try? log.joined(separator: "\n").write(to: dir.appendingPathComponent("snapshots.txt"), atomically: true, encoding: .utf8) }
        }
        // Snapshot mode renders design states; it never starts mic capture.
        log.append("capture: UNAVAILABLE in snapshot mode: no mic capture runs; each state's mic and listening flags are set directly to render it, and the readout is odj-live's analysis of the demo signal. These PNGs show layout, not a working capture path.")
        do {
            try feedDemo(seconds: 40)
            log.append("engine: demo fed 40 s; shown \(model.hold.shown?.camelot ?? "none"), bpm \(model.shownBpm.map(String.init) ?? "none")")
        } catch {
            log.append("engine: FAILED \(error)")
        }
        window.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)

        let states: [(String, (AppModel) -> Void)] = [
            ("window", { _ in }),
            ("settings", { m in m.settingsOpen = true }),
            // The design draws the Now playing menu open (reference-capture-menu.png).
            ("capture-menu", { m in m.splitMenu = .nowPlaying }),
            ("key-scores", { m in m.showKeys = true }),
            ("piano-ring", { m in m.camelotRing = false }),
            ("toast", { m in m.copyReadout() }),
            ("setup-1", { m in m.mic = .notAsked; m.listening = true; m.gateStep = 1 }),
            ("setup-2", { m in m.mic = .notAsked; m.listening = true; m.gateStep = 2 }),
            ("first-launch", { m in m.mic = .firstLaunch; m.listening = false }),
            ("no-mic", { m in m.mic = .denied; m.listening = true }),
            ("silent", { m in m.mic = .silent; m.listening = true }),
            ("compact", { m in m.compact = true }),
        ]
        for (name, setup) in states {
            reset()
            setup(model)
            settle()
            log.append(save(name, in: dir))
        }
        // Now playing through the production path (Apple Events, then the
        // inbox write). Whatever it really returns is what the PNG shows; on
        // a machine with no player running that is the real "Nothing
        // playing" result, never a made-up track.
        reset()
        model.nowPlaying()
        let npDeadline = Date().addingTimeInterval(20)
        while Date() < npDeadline, model.caught?.title.isEmpty ?? true { settle(0.2) }
        if let c = model.caught, !c.title.isEmpty {
            log.append("now-playing: \(c.title) | \(c.detail)")
        } else {
            log.append("now-playing: UNAVAILABLE: no result in 20 s (\(model.caught?.detail ?? "nothing shown"))")
        }
        log.append(save("now-playing", in: dir))
        // The drop zone on a real file: write the demo to a WAV and analyze it.
        reset()
        let wav = dir.appendingPathComponent("demo-96s.wav")
        do {
            try writeDemoWav(to: wav, seconds: 96)
            model.analyze(wav)
            let deadline = Date().addingTimeInterval(60)
            // The progress PNG comes from the real analysis, the first time it
            // reports a fraction, never from a state set by hand.
            var progressShot = "drop-progress: UNAVAILABLE no intermediate progress was published"
            while Date() < deadline, case .analyzing(_, let p) = model.drop {
                if p > 0, progressShot.contains("UNAVAILABLE") {
                    progressShot = "drop-progress: \(Int(p * 100))% | " + save("drop-progress", in: dir)
                }
                settle(0.2)
            }
            log.append(progressShot)
            if case .done = model.drop {
                log.append("drop: \(model.drop)")
            } else {
                // Still analyzing at the deadline, or failed: the app's drop
                // path didn't finish, whatever the CLI says later.
                log.append("drop: UNAVAILABLE \(model.drop)")
            }
        } catch {
            log.append("drop: FAILED \(error)")
        }
        settle()
        log.append(save("drop-result", in: dir))
        print(log.joined(separator: "\n"))
        NSApp.terminate(nil)
    }

    private func reset() {
        model.settingsOpen = false
        model.splitMenu = nil
        model.showKeys = false
        model.camelotRing = true
        model.compact = false
        model.toast = nil
        model.caught = nil
        model.drop = .idle
        model.mic = .granted
        model.listening = true
        model.gateStep = 1
    }

    private func settle(_ seconds: Double = 0.6) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
    }

    private func feedDemo(seconds: Int) throws {
        let engine = LiveEngine(executable: try EngineLocator.find(), sampleRate: 22050, hz: 10)
        final class Flag: @unchecked Sendable { var done = false }
        let flag = Flag()
        engine.onFrame = { [weak model] f in Task { @MainActor in model?.apply(f) } }
        // Hop to the main actor so the summary lands after every frame's apply.
        engine.onSummary = { _ in Task { @MainActor in flag.done = true } }
        try engine.start()
        var demo = DemoSignal(sampleRate: 22050)
        for _ in 0..<seconds { engine.push(demo.next(22050)) }
        engine.finish()
        let deadline = Date().addingTimeInterval(60)
        while !flag.done && Date() < deadline { settle(0.1) }
        // A stalled analyzer must not pass as "demo fed" evidence.
        guard flag.done else {
            engine.stop()
            throw NSError(domain: "Snapshotter", code: 1,
                          userInfo: [NSLocalizedDescriptionKey: "demo analysis gave no summary within 60 s"])
        }
    }

    private func writeDemoWav(to url: URL, seconds: Int) throws {
        var demo = DemoSignal(sampleRate: 22050)
        let samples = demo.next(22050 * seconds)
        var data = Data()
        func u32(_ v: UInt32) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        func u16(_ v: UInt16) { withUnsafeBytes(of: v.littleEndian) { data.append(contentsOf: $0) } }
        data.append(contentsOf: Array("RIFF".utf8)); u32(UInt32(36 + samples.count * 2))
        data.append(contentsOf: Array("WAVEfmt ".utf8)); u32(16); u16(1); u16(1); u32(22050); u32(44100); u16(2); u16(16)
        data.append(contentsOf: Array("data".utf8)); u32(UInt32(samples.count * 2))
        for s in samples { u16(UInt16(bitPattern: Int16(max(-1, min(1, s)) * 32767))) }
        try data.write(to: url)
    }

    private func save(_ name: String, in dir: URL) -> String {
        guard let view = window.contentView else { return "\(name): no content view" }
        view.layoutSubtreeIfNeeded()
        var notes: [String] = ["window frame \(window.frame) visible \(window.isVisible) onScreen \(window.occlusionState.contains(.visible))"]
        let bounds = view.bounds
        notes.append("view bounds \(bounds)")
        if let rep = view.bitmapImageRepForCachingDisplay(in: bounds) {
            view.cacheDisplay(in: bounds, to: rep)
            if let png = rep.representation(using: .png, properties: [:]) {
                try? png.write(to: dir.appendingPathComponent("\(name).png"))
                notes.append("render \(rep.pixelsWide)x\(rep.pixelsHigh)")
            }
        }
        let screen = dir.appendingPathComponent("\(name)-screen.png")
        // A failed capture must not leave an earlier run's file in its place.
        try? FileManager.default.removeItem(at: screen)
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
        p.arguments = ["-x", "-o", "-l\(window.windowNumber)", screen.path]
        do {
            try p.run()
            p.waitUntilExit()
            notes.append(p.terminationStatus == 0 && FileManager.default.fileExists(atPath: screen.path)
                         ? "screencapture ok" : "screencapture exit \(p.terminationStatus)")
        } catch {
            notes.append("screencapture failed: \(error)")
        }
        return "\(name): " + notes.joined(separator: ", ")
    }
}
