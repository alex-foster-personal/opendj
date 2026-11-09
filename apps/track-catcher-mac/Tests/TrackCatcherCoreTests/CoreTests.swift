import Foundation
import XCTest
@testable import TrackCatcherCore

final class KeyTests: XCTestCase {
    func testCamelotMatchesTheWheel() {
        XCTAssertEqual(MusicalKey(pc: 0, minor: false).camelot, "8B")   // C
        XCTAssertEqual(MusicalKey(pc: 9, minor: true).camelot, "8A")    // Am
        XCTAssertEqual(MusicalKey(pc: 4, minor: false).camelot, "12B")  // E
        XCTAssertEqual(MusicalKey(pc: 6, minor: true).camelot, "11A")   // F#m
        XCTAssertEqual(MusicalKey(pc: 7, minor: false).camelot, "9B")   // G, a fifth up
        XCTAssertEqual(MusicalKey(pc: 0, minor: false).openKey, "1d")
        XCTAssertEqual(MusicalKey(pc: 9, minor: true).openKey, "1m")
    }

    func testEveryCamelotCodeRoundTrips() {
        XCTAssertEqual(MusicalKey.all.count, 24)
        XCTAssertEqual(Set(MusicalKey.all).count, 24)
        for k in MusicalKey.all {
            XCTAssertEqual(MusicalKey(camelot: k.camelot), k, k.camelot)
        }
        for pc in 0..<12 { for m in [false, true] {
            let k = MusicalKey(pc: pc, minor: m)
            XCTAssertEqual(MusicalKey(camelot: k.camelot), k)
        } }
    }

    func testBadCodesAreRejected() {
        for bad in ["", "0B", "13A", "12C", "B", "x"] {
            XCTAssertNil(MusicalKey(camelot: bad), bad)
        }
        XCTAssertEqual(MusicalKey(camelot: " 8a "), MusicalKey(pc: 9, minor: true))
    }

    func testNames() {
        XCTAssertEqual(MusicalKey(pc: 4, minor: false).name, "E major")
        XCTAssertEqual(MusicalKey(pc: 6, minor: true).shortName, "F♯m")
        XCTAssertEqual(MusicalKey(pc: 4, minor: false).shortName, "E maj")
    }
}

final class CopyTextTests: XCTestCase {
    let e = MusicalKey(pc: 4, minor: false)

    func testReadoutIsTheDesignsString() {
        XCTAssertEqual(CopyText.readout(key: e, chord: "E", bpm: 124), "12B · E major · chord E · 124 BPM")
        XCTAssertEqual(CopyText.readout(key: e, chord: nil, bpm: nil), "12B · E major")
        XCTAssertEqual(CopyText.wheelKey(MusicalKey(pc: 4, minor: true)), "9A · E minor")
    }

    func testNotesHeardLoudestFirst() {
        var c = [Double](repeating: 0, count: 12)
        c[4] = 1; c[11] = 0.8; c[8] = 0.6; c[1] = 0.5; c[9] = 0.4; c[6] = 0.3; c[3] = 0.2; c[0] = 0.1
        XCTAssertEqual(CopyText.notesHeard(c), "Notes heard: E B A♭ C♯ A F♯ E♭")
    }

    func testNotesHeardNamesNothingBeforeAnyAudio() {
        XCTAssertEqual(CopyText.notesHeard([Double](repeating: 0, count: 12)), "Notes heard: none yet")
        var c = [Double](repeating: 0, count: 12)
        c[4] = 1; c[11] = 0.5
        XCTAssertEqual(CopyText.notesHeard(c), "Notes heard: E B")
    }

    func testAsciiWheelMarksTheCurrentKeyOnly() {
        let w = CopyText.asciiWheel(current: e)
        let lines = w.split(separator: "\n", omittingEmptySubsequences: false)
        XCTAssertEqual(lines.first, "Camelot (B/A), now 12B")
        XCTAssertEqual(lines.count, 8)
        XCTAssertEqual(lines[1], "             [12B]/12A")
        XCTAssertEqual(w.components(separatedBy: "[").count - 1, 1)
        for n in 1...12 { XCTAssertTrue(w.contains("\(n)A"), "\(n)A") }
        // Control: another key moves the brackets.
        let other = CopyText.asciiWheel(current: MusicalKey(pc: 9, minor: true))
        XCTAssertTrue(other.contains("8B/[8A]"))
        XCTAssertFalse(other.contains("[12B]"))
        XCTAssertEqual(CopyText.toast(for: w), "Copied  ASCII wheel")
        XCTAssertEqual(CopyText.toast(for: "12B · E major"), "Copied  12B · E major")
    }

    func testTrackResultNamesEveryKeyChange() throws {
        let json = """
        {"type":"summary","duration_s":240,"sample_rate_in":44100,"frames":480,"analysis_frames":1,
         "chroma_mean":[0,0,0,0,0,0,0,0,0,0,0,0],"keys":[],"key":"8A","key_name":"Am","key_openkey":"1m",
         "confidence":0.8,"margin":0.1,"bpm":123.6,"tuning_cents":0,"latency_s":0,
         "segments":{"status":"ok","reason":null,"window":"bar","window_s":1.9,"segments":[
           {"start_bar":0,"end_bar":99,"start_s":0,"end_s":192,"key_camelot":"8A","key_openkey":"1m","key_name":"Am","confidence":0.8},
           {"start_bar":100,"end_bar":125,"start_s":192.4,"end_s":240,"key_camelot":"9A","key_openkey":"2m","key_name":"Em","confidence":0.7}]}}
        """
        guard case .summary(let s) = try EngineLine.decode(Data(json.utf8)) else { return XCTFail("not a summary") }
        XCTAssertEqual(CopyText.trackResult(s), "8A · A minor · 124 BPM · key change at 3:12 → 9A · E minor")
    }

    func testTrackResultOpensOnTheFirstSegmentNotTheSummaryKey() throws {
        // A short 8A intro, then a longer 9A body: mean chroma says 9A, but
        // the line must read 8A first, change to 9A once, and not repeat it.
        let json = """
        {"type":"summary","duration_s":240,"sample_rate_in":44100,"frames":480,"analysis_frames":1,
         "chroma_mean":[0,0,0,0,0,0,0,0,0,0,0,0],"keys":[],"key":"9A","key_name":"Em","key_openkey":"2m",
         "confidence":0.8,"margin":0.1,"bpm":124,"tuning_cents":0,"latency_s":0,
         "segments":{"status":"ok","reason":null,"window":"bar","window_s":1.9,"segments":[
           {"start_bar":0,"end_bar":15,"start_s":0,"end_s":31,"key_camelot":"8A","key_openkey":"1m","key_name":"Am","confidence":0.7},
           {"start_bar":16,"end_bar":80,"start_s":31,"end_s":160,"key_camelot":"9A","key_openkey":"2m","key_name":"Em","confidence":0.8},
           {"start_bar":81,"end_bar":125,"start_s":160,"end_s":240,"key_camelot":"9A","key_openkey":"2m","key_name":"Em","confidence":0.8}]}}
        """
        guard case .summary(let s) = try EngineLine.decode(Data(json.utf8)) else { return XCTFail("not a summary") }
        XCTAssertEqual(CopyText.trackResult(s), "8A · A minor · 124 BPM · key change at 0:31 → 9A · E minor")
    }

    func testTrackResultFallsBackToSummaryKeyWithoutSegments() throws {
        let json = """
        {"type":"summary","duration_s":20,"sample_rate_in":44100,"frames":40,"analysis_frames":1,
         "chroma_mean":[0,0,0,0,0,0,0,0,0,0,0,0],"keys":[],"key":"9A","key_name":"Em","key_openkey":"2m",
         "confidence":0.5,"margin":0.1,"bpm":null,"tuning_cents":0,"latency_s":0,
         "segments":{"status":"failed","reason":"too short","window":"fixed","window_s":2,"segments":[]}}
        """
        guard case .summary(let s) = try EngineLine.decode(Data(json.utf8)) else { return XCTFail("not a summary") }
        XCTAssertEqual(CopyText.trackResult(s), "9A · E minor")
    }

    func testSilentTrackSaysSo() throws {
        let json = """
        {"type":"summary","duration_s":20,"sample_rate_in":44100,"frames":40,"analysis_frames":1,
         "chroma_mean":[0,0,0,0,0,0,0,0,0,0,0,0],"keys":[],"key":null,"key_name":null,"key_openkey":null,
         "confidence":null,"margin":null,"bpm":null,"tuning_cents":null,"latency_s":0,
         "segments":{"status":"failed","reason":"silent","window":"fixed","window_s":2,"segments":[]}}
        """
        guard case .summary(let s) = try EngineLine.decode(Data(json.utf8)) else { return XCTFail("not a summary") }
        XCTAssertEqual(CopyText.trackResult(s), "No key found (silent)")
    }
}

final class EngineLineTests: XCTestCase {
    func testUnknownTypeIsAnErrorNotDropped() {
        XCTAssertThrowsError(try EngineLine.decode(Data(#"{"type":"bogus"}"#.utf8)))
        XCTAssertThrowsError(try EngineLine.decode(Data("not json".utf8)))
    }

    func testSplitterKeepsPartialLines() {
        var s = LineSplitter()
        XCTAssertEqual(s.push(Data("ab".utf8)), [])
        XCTAssertEqual(s.push(Data("c\nde\n\nf".utf8)), [Data("abc".utf8), Data("de".utf8)])
        XCTAssertEqual(s.push(Data("\n".utf8)), [Data("f".utf8)])
    }
}

final class TapAndHoldTests: XCTestCase {
    func testFourTapsAt124() {
        var t = TapTempo()
        XCTAssertEqual(t.label, "tap 4+ times")
        let beat = 60.0 / 124
        for i in 0..<3 { t.tap(at: Double(i) * beat) }
        XCTAssertNil(t.bpm)
        XCTAssertEqual(t.label, "3 taps…")
        t.tap(at: 3 * beat)
        XCTAssertEqual(t.bpm!, 124, accuracy: 0.01)
        XCTAssertEqual(t.label, "tapped 124.0")
        t.halve()
        XCTAssertEqual(t.bpm!, 62, accuracy: 0.01)
        // A further tap keeps the halving.
        t.tap(at: 4 * beat)
        XCTAssertEqual(t.bpm!, 62, accuracy: 0.01)
        // A 2 s gap starts again.
        t.tap(at: 4 * beat + 2.5)
        XCTAssertNil(t.bpm)
        XCTAssertEqual(t.taps.count, 1)
    }

    func testKeyFlipsOnlyAfterTheHold() {
        let e = MusicalKey(pc: 4, minor: false), fsm = MusicalKey(pc: 6, minor: true)
        var h = KeyHold(holdSeconds: 6)
        h.update(top: e, at: 0)
        h.update(top: fsm, at: 1)
        XCTAssertEqual(h.label, "E major · F♯m?")
        h.update(top: fsm, at: 6.9)
        XCTAssertEqual(h.shown, e)
        h.update(top: fsm, at: 7.0)
        XCTAssertEqual(h.shown, fsm)
        XCTAssertNil(h.pending)
        // Control: a blip back to the shown key clears the pending one.
        h.update(top: e, at: 8)
        h.update(top: fsm, at: 9)
        XCTAssertNil(h.pending)
    }
}

final class InboxAndNowPlayingTests: XCTestCase {
    func testRecentPlaylistsThrowsOnAMalformedInbox() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let inbox = Inbox(url: dir.appendingPathComponent("inbox.jsonl"))
        try inbox.append(CatchEntry(source: .shazam, title: "T1", artist: "A", playlist: "Friday gig"))
        let h = try FileHandle(forWritingTo: inbox.url)
        try h.seekToEnd()
        try h.write(contentsOf: Data("{\"half a line\n".utf8))
        try h.close()
        XCTAssertThrowsError(try inbox.recentPlaylists())
    }

    func testInboxAppendsLinesAndListsRecentPlaylists() throws {
        let dir = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: dir) }
        let inbox = Inbox(url: dir.appendingPathComponent("inbox.jsonl"))
        XCTAssertEqual(try inbox.recentPlaylists(), ["Inbox"])
        try inbox.append(CatchEntry(source: .shazam, title: "T1", artist: "A", playlist: "Friday gig", heardKey: "12B", heardBpm: 124))
        try inbox.append(CatchEntry(source: .spotify, title: "T2", artist: "B", playlist: "Wanted", spotifyID: "spotify:track:x"))
        try inbox.append(CatchEntry(source: .music, title: "T3", artist: "C"))
        let all = try inbox.entries()
        XCTAssertEqual(all.map(\.title), ["T1", "T2", "T3"])
        XCTAssertEqual(try inbox.recentPlaylists(), ["Inbox", "Wanted", "Friday gig"])
        XCTAssertEqual(all[0].resultLine, "from Shazam · heard 12B · 124 BPM · added to Friday gig")
        XCTAssertTrue(all[0].caughtAt.hasSuffix("Z"), all[0].caughtAt)
        let raw = try String(contentsOf: inbox.url, encoding: .utf8)
        XCTAssertEqual(raw.split(separator: "\n").count, 3)
        XCTAssertTrue(raw.contains(#""heard_key":"12B""#))
    }

    func testNowPlayingParse() {
        let t = NowPlaying.parse("playing\tSong\tArtist\tspotify:track:1\n", player: .spotify)
        XCTAssertEqual(t, NowPlaying.Track(player: .spotify, title: "Song", artist: "Artist", id: "spotify:track:1"))
        XCTAssertNil(NowPlaying.parse("", player: .music))
        XCTAssertNil(NowPlaying.parse("paused\tSong\tArtist\t1", player: .music))
        XCTAssertTrue(NowPlaying.script(for: .spotify).contains("if application \"Spotify\" is running"))
    }
}

final class EngineProcessTests: XCTestCase {
    /// Runs the real odj-live when ODJ_LIVE points at it (CI builds it first).
    /// TC_TEST_WAV: 24 s of C major from scripts/make_test_wav.py.
    func testRealEngineOnAFileWhenAvailable() throws {
        let env = ProcessInfo.processInfo.environment
        guard let exe = env["ODJ_LIVE"], let wav = env["TC_TEST_WAV"] else {
            // CI sets TC_REQUIRE_FILE_TEST so a missing WAV fails the job
            // instead of skipping the drop-analysis path.
            if env["TC_REQUIRE_FILE_TEST"] != nil { return XCTFail("TC_REQUIRE_FILE_TEST is set but ODJ_LIVE or TC_TEST_WAV is not") }
            throw XCTSkip("set ODJ_LIVE and TC_TEST_WAV to run the engine end to end")
        }
        var last = 0.0
        let s = try FileAnalysis.run(executable: URL(fileURLWithPath: exe), file: URL(fileURLWithPath: wav), duration: 24) { last = $0 }
        XCTAssertEqual(s.key, "8B")
        XCTAssertEqual(last, 1)
    }

    func testLiveEngineStreamsFramesFromStdin() throws {
        guard let exe = ProcessInfo.processInfo.environment["ODJ_LIVE"] else {
            throw XCTSkip("set ODJ_LIVE to run the engine end to end")
        }
        let engine = LiveEngine(executable: URL(fileURLWithPath: exe), sampleRate: 22050, hz: 10)
        let got = expectation(description: "summary")
        var frames: [EngineFrame] = []
        var summary: EngineSummary?
        let lock = NSLock()
        engine.onFrame = { f in lock.lock(); frames.append(f); lock.unlock() }
        engine.onSummary = { s in summary = s; got.fulfill() }
        try engine.start()
        // 12 s of C major triad (C4 E4 G4) at 22050 Hz.
        let sr = 22050.0
        let tones = [261.63, 329.63, 392.0]
        for chunk in 0..<120 {
            let samples = (0..<2205).map { i -> Float in
                let t = Double(chunk * 2205 + i) / sr
                return Float(tones.map { 0.2 * sin(2 * .pi * $0 * t) }.reduce(0, +))
            }
            engine.push(samples)
        }
        engine.finish()
        wait(for: [got], timeout: 60)
        lock.lock(); defer { lock.unlock() }
        XCTAssertGreaterThan(frames.count, 100)
        XCTAssertEqual(summary?.key, "8B")
    }

    /// Same stream through `enqueue`, the path the audio tap uses.
    func testLiveEngineEnqueueKeepsOrderOffTheCaller() throws {
        guard let exe = ProcessInfo.processInfo.environment["ODJ_LIVE"] else {
            throw XCTSkip("set ODJ_LIVE to run the engine end to end")
        }
        let engine = LiveEngine(executable: URL(fileURLWithPath: exe), sampleRate: 22050, hz: 10, maxQueued: 1000)
        let got = expectation(description: "summary")
        var summary: EngineSummary?
        engine.onSummary = { s in summary = s; got.fulfill() }
        try engine.start()
        let sr = 22050.0
        let tones = [261.63, 329.63, 392.0]
        for chunk in 0..<120 {
            engine.enqueue((0..<2205).map { i -> Float in
                let t = Double(chunk * 2205 + i) / sr
                return Float(tones.map { 0.2 * sin(2 * .pi * $0 * t) }.reduce(0, +))
            })
        }
        engine.finish()
        wait(for: [got], timeout: 60)
        XCTAssertEqual(engine.dropped, 0)
        XCTAssertEqual(summary?.key, "8B")
        XCTAssertEqual(summary?.durationS ?? 0, 12, accuracy: 0.2)
    }

    /// `onExit` reports the engine leaving, so the app can stop showing a
    /// stale readout: status 0 after a clean finish, nonzero when killed.
    func testLiveEngineReportsExit() throws {
        guard let exe = ProcessInfo.processInfo.environment["ODJ_LIVE"] else {
            throw XCTSkip("set ODJ_LIVE to run the engine end to end")
        }
        for kill in [false, true] {
            let engine = LiveEngine(executable: URL(fileURLWithPath: exe), sampleRate: 22050, hz: 10)
            let exited = expectation(description: "exit kill=\(kill)")
            var status: Int32?
            engine.onExit = { s in status = s; exited.fulfill() }
            try engine.start()
            engine.push([Float](repeating: 0, count: 22050))
            if kill { engine.stop() } else { engine.finish() }
            wait(for: [exited], timeout: 30)
            if kill { XCTAssertNotEqual(status, 0) } else { XCTAssertEqual(status, 0) }
        }
    }

    /// Past `maxQueued` the newest buffer is dropped and counted, never
    /// queued; under it nothing is dropped.
    func testLiveEngineEnqueueDropsPastTheBound() {
        let full = LiveEngine(executable: URL(fileURLWithPath: "/nonexistent/odj-live"), sampleRate: 22050, maxQueued: 0)
        var fullRuns = 0
        full.onDrop = { fullRuns += 1 }
        for _ in 0..<5 { full.enqueue([0.1, 0.2]) }
        XCTAssertEqual(full.dropped, 5)
        // One run of drops is one signal, so the app restarts once, not per buffer.
        XCTAssertEqual(fullRuns, 1)
        let roomy = LiveEngine(executable: URL(fileURLWithPath: "/nonexistent/odj-live"), sampleRate: 22050, maxQueued: 8)
        var roomyRuns = 0
        roomy.onDrop = { roomyRuns += 1 }
        for _ in 0..<5 { roomy.enqueue([0.1, 0.2]) }
        roomy.finish()
        XCTAssertEqual(roomy.dropped, 0)
        XCTAssertEqual(roomyRuns, 0)
    }
}

final class AppCommandTests: XCTestCase {
    /// One CLI name per button (TCAPP-6577h), each parsing back to itself.
    func testEveryButtonHasACommand() {
        let names = Set(AppCommand.allCases.map(\.rawValue))
        for n in ["catch", "now-playing", "listen", "stop", "tap", "copy", "settings", "setup", "quit",
                  "compact", "pin", "half", "double", "copy-notes", "copy-wheel", "copy-key", "bars", "ring"] {
            XCTAssertTrue(names.contains(n), n)
            XCTAssertEqual(AppCommand(rawValue: n)?.rawValue, n)
        }
    }
}

final class DemoSignalTests: XCTestCase {
    func testDemoScript() {
        XCTAssertEqual(DemoSignal.key(at: 10).camelot, "12B")
        XCTAssertEqual(DemoSignal.key(at: 40).camelot, "11A")
        XCTAssertEqual(DemoSignal.chord(at: 0.5).0, 4)
        XCTAssertTrue(DemoSignal.chord(at: 2.5).1)       // C#m
        XCTAssertEqual(DemoSignal.chord(at: 33).0, 6)    // F#m
        var d = DemoSignal()
        let a = d.next(100), b = d.next(100)
        XCTAssertEqual(d.position, 200)
        XCTAssertNotEqual(a, b)
        XCTAssertLessThanOrEqual(a.map(abs).max()!, 1)
    }

    /// The real engine on the demo: the live key readout, with BPM and
    /// segments withheld by the engine (see the end of the test).
    func testEngineReadsTheDemoKeys() throws {
        guard let exe = ProcessInfo.processInfo.environment["ODJ_LIVE"] else {
            throw XCTSkip("set ODJ_LIVE to run the engine end to end")
        }
        let engine = LiveEngine(executable: URL(fileURLWithPath: exe), sampleRate: 22050, hz: 2)
        let done = expectation(description: "summary")
        var frames: [EngineFrame] = []
        var summary: EngineSummary?
        let lock = NSLock()
        engine.onFrame = { f in lock.lock(); frames.append(f); lock.unlock() }
        engine.onSummary = { s in summary = s; done.fulfill() }
        try engine.start()
        var demo = DemoSignal()
        for _ in 0..<96 { engine.push(demo.next(22050)) }
        engine.finish()
        wait(for: [done], timeout: 120)
        lock.lock(); defer { lock.unlock() }
        XCTAssertEqual(frames.first { $0.t >= 30 }?.key, "12B")
        // Main's engine withholds BPM and marks whole-file segments pending
        // until both are calibrated (TCLIVE-6567).
        XCTAssertNil(summary?.bpm)
        XCTAssertEqual(summary?.segments.status, "pending")
        // Known engine gap, measured 13 Oct 2026: with the 6 s hold the live
        // readout goes 12B -> 12A -> 12B. It settles on C♯ minor, the
        // dominant's key, for the F♯ minor half, which the whole-file
        // segments got right before they were withheld. This pins what the window shows today,
        // so a live-key fix turns this line red and must update it to 11A.
        var hold = KeyHold()
        var shownChanges: [String] = []
        for f in frames {
            let before = hold.shown
            hold.update(top: f.key.flatMap(MusicalKey.init(camelot:)), at: f.t)
            if hold.shown != before, let k = hold.shown { shownChanges.append(k.camelot) }
        }
        XCTAssertEqual(shownChanges, ["12B", "12A", "12B"])
    }

    /// [if] a main-shaped frame carries three keys with no p [then] it decodes and bars follow the kept score, [else stop].
    func testKeyBarsDrawFromTheKeptScore() throws {
        let line = """
        {"type":"frame","t":1.0,"chroma":[0,0,0,0,0,0,0,0,0,0,0,0],"chroma_fast":[0,0,0,0,0,0,0,0,0,0,0,0],
         "chroma_slow":[0,0,0,0,0,0,0,0,0,0,0,0],"keys":[
          {"camelot":"8A","openkey":"1m","name":"A minor","score":0.82},
          {"camelot":"9A","openkey":"2m","name":"E minor","score":1.4},
          {"camelot":"12B","openkey":"5d","name":"E major","score":-0.1}],
         "key":"8A","key_name":"A minor","key_openkey":"1m","confidence":0.82,"margin":0.1,"bpm":null,"tuning_cents":null}
        """.replacingOccurrences(of: "\n", with: "")
        guard case .frame(let f) = try EngineLine.decode(Data(line.utf8)) else { return XCTFail("not a frame") }
        XCTAssertEqual(f.keys.count, 3)
        XCTAssertEqual(f.keys.map(\.barFraction), [0.82, 1.0, 0.0])
        XCTAssertNil(f.bpm)
    }
}
