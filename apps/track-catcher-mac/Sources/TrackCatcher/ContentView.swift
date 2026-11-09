import AppKit
import SwiftUI
import UniformTypeIdentifiers
import TrackCatcherCore

/// The Track Catcher window, laid out like the design's `.win`.
struct ContentView: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 0) {
            TitleBar(model: model)
            ZStack(alignment: .bottom) {
                bodyRow
                    .opacity(bodyOpacity)
                overlays
            }
            if !model.compact {
                DropZone(model: model)
                Timeline(model: model)
            }
        }
        .frame(width: model.compact ? 300 : 830)
        .background(Theme.bg)
        .overlay(alignment: .top) { toast }
        .overlay(alignment: .topTrailing) {
            if model.settingsOpen { SettingsPanel(model: model).padding(.top, 49).padding(.trailing, 22) }
        }
        // The title bar row is the window's title bar: draw from the very top.
        .ignoresSafeArea()
        .preferredColorScheme(.dark)
    }

    private var bodyOpacity: Double {
        if gated { return 0.35 }
        if model.mic == .denied || model.mic == .firstLaunch { return 0.55 }
        if model.mic == .silent { return 0.35 }
        return model.listening ? 1 : 0.35
    }

    private var gated: Bool { model.listening && model.mic == .notAsked }

    @ViewBuilder private var bodyRow: some View {
        if model.compact {
            VStack(spacing: 8) { WheelView(model: model) }
                .padding(10)
                .frame(maxWidth: .infinity)
        } else {
            HStack(alignment: .top, spacing: 16) {
                VStack(spacing: 8) {
                    WheelView(model: model)
                    HStack(spacing: 6) {
                        Text("Chord now").foregroundStyle(Theme.dim)
                        Text(model.chord ?? "-").font(Theme.mono(14, .medium)).foregroundStyle(.white)
                    }
                    .font(Theme.mono(12))
                    // The design's line box: 14 px at line-height 1.5.
                    .frame(height: 21)
                }
                .frame(width: 260)
                Readout(model: model)
            }
            .padding(16)
        }
    }

    @ViewBuilder private var overlays: some View {
        if gated {
            Gate(model: model).frame(maxWidth: .infinity, maxHeight: .infinity)
        } else if model.mic == .denied || model.mic == .firstLaunch {
            NoMicBanner(model: model).padding(.bottom, 8)
        } else if model.mic == .silent && model.listening {
            // Inside the idle body in the design, so it dims with it.
            SilentLine().padding(.horizontal, 16).padding(.bottom, 8).opacity(0.35)
        }
    }

    @ViewBuilder private var toast: some View {
        if let t = model.toast {
            Text(t)
                .font(Theme.mono(12))
                .foregroundStyle(.white)
                .lineLimit(1)
                .truncationMode(.tail)
                .padding(.vertical, 6).padding(.horizontal, 12)
                .background(RoundedRectangle(cornerRadius: 8).fill(Theme.panel))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.orange))
                .shadow(color: .black.opacity(0.5), radius: 10, y: 8)
                .padding(.top, 46)
        }
    }
}

// MARK: Title bar

struct TitleBar: View {
    @ObservedObject var model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            // The real traffic lights sit here (full-size content view).
            Color.clear.frame(width: 52, height: 1)
            if !model.compact {
                HStack(spacing: 6) {
                    Text("OPEN DJ")
                    ODJLogo().frame(width: 14, height: 14)
                    Text("TRACK CATCHER")
                }
                .foregroundStyle(Theme.text)
            }
            ListenPill(model: model)
            Spacer()
            HStack(spacing: 2) {
                IconButton(symbol: model.compact ? "arrow.up.left.and.arrow.down.right" : "arrow.down.right.and.arrow.up.left",
                           help: model.compact ? "Larger" : "Smaller", tint: Theme.dim) { model.compact.toggle() }
                IconButton(symbol: "pin.fill", help: "Keep on top: \(model.keepOnTop ? "on" : "off")",
                           tint: model.keepOnTop ? Theme.orange : Theme.dim) { model.keepOnTop.toggle() }
                IconButton(symbol: "gearshape.fill", help: "Settings", tint: model.settingsOpen ? .white : Theme.dim,
                           active: model.settingsOpen) {
                    model.refreshInputs()
                    model.settingsOpen.toggle()
                }
            }
        }
        .font(Theme.mono(11))
        .tracking(0.66)
        .padding(.horizontal, 12)
        // The design's 41 px bar sits under its 1 px window border, so its
        // bottom line falls at y = 41.
        .frame(height: 42)
        .background(Theme.panel)
        .overlay(alignment: .bottom) { Theme.line.frame(height: 1) }
    }
}

struct ListenPill: View {
    @ObservedObject var model: AppModel

    /// Red whenever listening was asked for, as in the design's setup, no-mic
    /// and silent states; the label still says whether the mic is granted.
    private var on: Bool { model.listening && model.mic != .firstLaunch }
    private var label: String {
        if model.mic == .firstLaunch { return "DEMO" }
        if !model.listening { return "NOT LISTENING" }
        return model.mic == .granted ? "LISTENING (\(inputName.uppercased()))" : "LISTENING"
    }
    private var inputName: String {
        model.inputs.first { $0.uid == model.inputUID }?.name ?? "Mic"
    }

    var body: some View {
        Button(action: model.toggleListening) {
            HStack(spacing: 6) {
                Circle()
                    .fill(on ? Theme.red : Color.clear)
                    .overlay(Circle().strokeBorder(on ? Theme.red : Theme.dim, lineWidth: 1.5))
                    .frame(width: 8, height: 8)
                if !model.compact { Text(label) }
            }
            .font(Theme.mono(11, .medium))
            .foregroundStyle(on ? Color.white : Theme.dim)
            .padding(.vertical, 2).padding(.horizontal, 10)
            .overlay(Capsule().strokeBorder(on ? Theme.red : Theme.line))
            .contentShape(Capsule())
        }
        .buttonStyle(.plain)
        .padding(.leading, 10)
        .help("Start or stop listening (L)")
        .keyboardShortcut("l", modifiers: [])
    }
}

struct IconButton: View {
    let symbol: String
    let help: String
    let tint: Color
    var active = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: symbol)
                .font(.system(size: 12))
                .foregroundStyle(tint)
                .frame(width: 26, height: 24)
                .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(active ? Theme.line : .clear))
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help(help)
        .accessibilityLabel(help)
    }
}

// MARK: Readout

struct Readout: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            bigKey
            confidence
            Group { if model.showKeys { KeyScores(model: model) } else { NoteBars(model: model) } }
                .contextMenu {
                    Toggle("Notes heard", isOn: Binding(get: { !model.showKeys }, set: { if $0 { model.showKeys = false } }))
                    Toggle("Key scores", isOn: Binding(get: { model.showKeys }, set: { if $0 { model.showKeys = true } }))
                }
            tapRow
            CaptureButtons(model: model)
            if let c = model.caught { CaughtBox(title: c.title, detail: c.detail) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var bigKey: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Text(model.hold.shown?.camelot ?? "-")
                .font(Theme.display(44))
                // The design's line box: line-height 1.
                .frame(height: 44)
                .foregroundStyle(.white)
            Text(model.hold.label)
                .font(Theme.mono(16))
                .foregroundStyle(Theme.text)
            Spacer()
            HStack(alignment: .firstTextBaseline, spacing: 13) {
                Text(model.shownBpm.map(String.init) ?? "-")
                    .font(Theme.mono(22))
                    .monospacedDigit()
                    .foregroundStyle(Theme.bpm)
                    .help("Beats per minute, rounded to a whole number\(model.tap.bpm != nil ? " (tapped)" : "")")
                Text("BPM").font(Theme.mono(11)).foregroundStyle(Theme.dim)
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { model.copyReadout() }
        .help("Click to copy key, chord and BPM")
    }

    private var confidence: some View {
        HStack(spacing: 0) {
            Text("Confidence ")
            Text(model.confidence.map { String(format: "%.2f", $0) } ?? "-").foregroundStyle(Theme.text)
                .help("Correlation of the heard notes with the shown key's profile (0 to 1)")
            Text(" · margin over next key ")
            Text(model.margin.map { String(format: "%.2f", $0) } ?? "-").foregroundStyle(Theme.text)
                .help("How far the shown key's score leads the runner-up")
        }
        .font(Theme.mono(11, .regular))
        .tracking(0.44)
        .foregroundStyle(Theme.dim)
    }

    private var tapRow: some View {
        HStack(spacing: 6) {
            Button(action: model.doTap) {
                HStack(spacing: 6) {
                    Text("Tap")
                    Text("T").font(Theme.mono(10)).foregroundStyle(Theme.dim)
                        .padding(.horizontal, 5)
                        .overlay(RoundedRectangle(cornerRadius: 4).strokeBorder(Theme.line))
                }
            }
            .buttonStyle(DevButtonStyle())
            .keyboardShortcut("t", modifiers: [])
            .help("Tap on the beat (T)")
            Text(model.tap.label)
                .font(Theme.mono(11)).foregroundStyle(Theme.dim).monospacedDigit()
                .frame(minWidth: 80, alignment: .leading)
                .help("Tempo from your taps: the average of the last eight intervals")
            Button("½") { model.tap.halve() }.buttonStyle(DevButtonStyle(small: true)).help("Halve the BPM")
            Button("×2") { model.tap.double() }.buttonStyle(DevButtonStyle(small: true)).help("Double the BPM")
        }
    }
}

struct NoteBars: View {
    @ObservedObject var model: AppModel

    var body: some View {
        let fast = model.chromaFast
        let fmax = max(fast.max() ?? 0, 1e-9)
        let scale = Set(model.hold.shown?.scale ?? [])
        VStack(spacing: 4) {
            HStack(alignment: .bottom, spacing: 3) {
                ForEach(0..<12, id: \.self) { i in
                    let v = fast.count == 12 ? fast[i] / fmax : 0
                    UnevenRoundedRectangle(cornerRadii: .init(topLeading: 2, topTrailing: 2))
                        .fill(i == model.hold.shown?.pc ? Theme.master : scale.contains(i) ? Theme.accent : Theme.barOff)
                        .frame(maxWidth: .infinity)
                        .frame(height: max(92 * 0.03, 92 * v))
                }
            }
            .frame(height: 92, alignment: .bottom)
            HStack(spacing: 3) {
                ForEach(0..<12, id: \.self) { i in
                    Text(MusicalKey.notes[i]).font(Theme.mono(10)).foregroundStyle(Theme.dim).frame(maxWidth: .infinity)
                }
            }
        }
        .contentShape(Rectangle())
        .onTapGesture { model.copy(CopyText.notesHeard(fast)) }
        .help("Notes heard, loudest bar tallest. Click to copy them")
    }
}

struct KeyScores: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 4) {
            ForEach(Array(model.keyScores.prefix(6).enumerated()), id: \.offset) { i, k in
                HStack(spacing: 8) {
                    Text(k.camelot).frame(width: 44, alignment: .leading)
                    Text(MusicalKey(camelot: k.camelot)?.shortName ?? k.name).frame(width: 72, alignment: .leading)
                    GeometryReader { g in
                        ZStack(alignment: .leading) {
                            Capsule().fill(Theme.track)
                            Capsule().fill(i == 0 ? Theme.master : Theme.accent).frame(width: g.size.width * k.barFraction)
                        }
                    }
                    .frame(height: 6)
                    Text(String(format: "%.2f", k.score)).foregroundStyle(Theme.dim).frame(width: 40, alignment: .trailing)
                        .help("Key score: how well the notes heard match this key")
                }
                .font(Theme.mono(11))
                .frame(height: 16.5)
            }
        }
    }
}

// MARK: Capture buttons

struct CaptureButtons: View {
    @ObservedObject var model: AppModel

    var body: some View {
        HStack(spacing: 8) {
            split(.catchTrack, title: "Catch → Inbox", catchStyle: true,
                  help: "Identify what's playing out loud (ShazamKit) and send it to your Inbox") { model.catchTrack() }
            split(.nowPlaying, title: "Now playing → Inbox", catchStyle: false,
                  help: "Send the song Spotify or Apple Music is playing to your Inbox") { model.nowPlaying() }
        }
        .padding(.top, 8)
    }

    private func split(_ kind: AppModel.CaptureKind, title: String, catchStyle: Bool, help: String,
                       action: @escaping () -> Void) -> some View {
        HStack(spacing: -1) {
            Button(action: action) { Text(title).frame(maxWidth: .infinity) }
                .buttonStyle(DevButtonStyle(catchStyle: catchStyle,
                                            corners: .init(topLeading: 6, bottomLeading: 6, bottomTrailing: 0, topTrailing: 0)))
                .help(help)
            Button { model.splitMenu = model.splitMenu == kind ? nil : kind } label: { Text("▾") }
                .buttonStyle(DevButtonStyle(catchStyle: catchStyle, small: true,
                                            corners: .init(topLeading: 0, bottomLeading: 0, bottomTrailing: 6, topTrailing: 6)))
                .help(kind == .catchTrack ? "Catch to playlist" : "Capture to playlist")
                .accessibilityLabel(kind == .catchTrack ? "Catch to a playlist" : "Now playing to a playlist")
        }
        .frame(maxWidth: .infinity)
        .overlay(alignment: .bottomTrailing) {
            if model.splitMenu == kind {
                // Opens upward, 6 points above the button, like the design's .splitmenu.
                PlaylistMenu(model: model, kind: kind).offset(y: -34)
            }
        }
        .zIndex(model.splitMenu == kind ? 1 : 0)
    }
}

struct PlaylistMenu: View {
    @ObservedObject var model: AppModel
    let kind: AppModel.CaptureKind

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(kind == .catchTrack ? "CATCH TO PLAYLIST" : "NOW PLAYING TO PLAYLIST")
                .font(Theme.mono(10)).tracking(0.8).foregroundStyle(Theme.dim)
                .padding(.horizontal, 8).padding(.top, 6).padding(.bottom, 4)
            switch model.playlists {
            case .success(let names):
                ForEach(names, id: \.self) { name in row(name) { go(name) } }
            case .failure(let error):
                Text("Inbox unreadable: \(error.localizedDescription)")
                    .font(Theme.body(12)).foregroundStyle(Theme.redText)
                    .padding(.vertical, 6).padding(.horizontal, 8)
                    .frame(maxWidth: 260, alignment: .leading)
                    .fixedSize(horizontal: false, vertical: true)
            }
            row("New playlist…") { go(AppModel.newPlaylistName()) }
        }
        .padding(4)
        .frame(minWidth: 210, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.panel))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.line))
        .shadow(color: .black.opacity(0.55), radius: 12, y: 10)
        .fixedSize()
    }

    private func row(_ title: String, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title).font(Theme.body(13)).foregroundStyle(Theme.text)
                .padding(.vertical, 6).padding(.horizontal, 8)
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private func go(_ playlist: String) {
        kind == .catchTrack ? model.catchTrack(to: playlist) : model.nowPlaying(to: playlist)
    }
}

struct CaughtBox: View {
    let title: String
    let detail: String

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            if !title.isEmpty { Text(title).font(Theme.mono(12, .medium)).foregroundStyle(.white) }
            Text(detail).font(Theme.mono(11)).foregroundStyle(Theme.dim)
        }
        .padding(.vertical, 8).padding(.horizontal, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.line))
    }
}

// MARK: Drop zone and timeline

struct DropZone: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 6) {
            switch model.drop {
            case .idle, .over:
                Text("Drop a track here to analyze it")
            case .analyzing(let name, let p):
                Text("Analyzing \(name)…")
                GeometryReader { g in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Theme.track)
                        Capsule().fill(Theme.orange).frame(width: g.size.width * p)
                    }
                }
                .frame(height: 5)
                .help("\(Int((p * 100).rounded()))% of the track analyzed")
            case .done(let line):
                (Text("Result: ").foregroundColor(.white) + Text(line))
                    .onTapGesture { model.copy(line) }
            case .failed(let why):
                Text("Couldn't analyze: \(why)").foregroundStyle(Theme.redText)
            }
        }
        .font(Theme.mono(12))
        .foregroundStyle(model.drop == .over ? Color.white : Theme.dim)
        .multilineTextAlignment(.center)
        .padding(.vertical, 8).padding(.horizontal, 12)
        .frame(maxWidth: .infinity)
        .background(RoundedRectangle(cornerRadius: 8).fill(model.drop == .over ? Theme.orange.opacity(0.08) : .clear))
        .overlay(RoundedRectangle(cornerRadius: 8)
            .strokeBorder(model.drop == .over ? Theme.orange : Theme.line, style: StrokeStyle(lineWidth: 1, dash: [4, 3])))
        .padding(.horizontal, 16).padding(.bottom, 10)
        .help("Drop an audio file to analyze the whole track")
        .onDrop(of: [.fileURL], isTargeted: Binding(get: { model.drop == .over },
                                                    set: { over in
                                                        if over { model.drop = .over } else if model.drop == .over { model.drop = .idle }
                                                    })) { providers in
            guard let p = providers.first else { return false }
            _ = p.loadObject(ofClass: URL.self) { url, _ in
                if let url { DispatchQueue.main.async { model.analyze(url) } }
            }
            return true
        }
    }
}

struct Timeline: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(spacing: 4) {
            HStack(spacing: 0) {
                if model.history.isEmpty {
                    Theme.bg
                } else {
                    ForEach(Array(model.history.enumerated()), id: \.offset) { _, k in
                        Theme.keyColor(k).help(k.camelot)
                    }
                }
            }
            .frame(height: 16)
            .clipShape(RoundedRectangle(cornerRadius: 3))
            .overlay(RoundedRectangle(cornerRadius: 3).strokeBorder(Theme.line))
            HStack {
                Text("2 min ago")
                Spacer()
                Text("Key over time")
                Spacer()
                Text("now")
            }
            .font(Theme.mono(10)).foregroundStyle(Theme.dim)
            // The design's line box: 10 px at line-height 1.5.
            .frame(height: 15)
        }
        .padding(.horizontal, 16).padding(.bottom, 14)
    }
}

// MARK: Setup, no mic, silent

struct Gate: View {
    @ObservedObject var model: AppModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            // The card's text keeps the page's line-height 1.5 (the buttons'
            // font shorthand resets theirs): 15, 21 and 18 px lines.
            Text("\(model.gateStep) OF 2").font(Theme.mono(10)).tracking(0.8).frame(height: 15)
            if model.gateStep == 1 {
                HStack(spacing: 7) {
                    Text("Open DJ")
                    ODJLogo().frame(width: 18, height: 18)
                    Text("Track Catcher")
                }
                .font(Theme.mono(14, .medium)).foregroundStyle(.white).frame(height: 21)
                gateBody("Shows the key of whatever's playing and catches the track into your Inbox in one click. Light on memory. Audio never leaves this Mac.")
            } else {
                Text("Listen with your mic").font(Theme.mono(14, .medium)).foregroundStyle(.white).frame(height: 21)
                gateBody("Track Catcher uses your mic to show the key live. Nothing is recorded.")
            }
            HStack(spacing: 8) {
                Button(model.gateStep == 1 ? "Next" : "Listen", action: model.gateNext)
                    .buttonStyle(DevButtonStyle(catchStyle: true))
                    .keyboardShortcut(.return, modifiers: .command)
                if model.gateStep == 2 { Button("Later", action: model.gateLater).buttonStyle(DevButtonStyle()) }
            }
        }
        .font(Theme.mono(12))
        .foregroundStyle(Theme.dim)
        .padding(16)
        .frame(maxWidth: 460, alignment: .leading)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.line))
        .shadow(color: .black.opacity(0.4), radius: 15, y: 10)
        .padding(16)
    }

    /// Plex Mono 12's 15.6 px line opened up to the design's 18 px.
    private func gateBody(_ s: String) -> some View {
        Text(s).lineSpacing(2.4).padding(.vertical, 1.2)
    }
}

struct NoMicBanner: View {
    @ObservedObject var model: AppModel

    private var text: String {
        if model.mic == .firstLaunch { return "NO AUDIO" }
        return model.noMicTries >= 2 ? "TURN ON TRACK CATCHER IN SETTINGS" : "NO MIC ACCESS"
    }
    private var button: String {
        if model.mic == .firstLaunch { return "Set up →" }
        switch model.noMicTries {
        case 0: return "Try again"
        case 1: return "Open Microphone settings"
        default: return "Opened Microphone settings"
        }
    }

    var body: some View {
        HStack(spacing: 10) {
            Text(text).font(Theme.mono(13, .semibold)).tracking(1).foregroundStyle(Theme.redText)
            Button(button, action: model.noMicRetry).buttonStyle(DevButtonStyle(catchStyle: model.mic == .firstLaunch))
        }
        .padding(.vertical, 6).padding(.leading, 12).padding(.trailing, 8)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.panel))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.red))
        .shadow(color: .black.opacity(0.5), radius: 9, y: 6)
    }
}

struct SilentLine: View {
    private static let lines = ["I will follow you into that black night", "Do not go softly"]

    var body: some View {
        TimelineView(.periodic(from: .now, by: 5)) { ctx in
            let i = Int(ctx.date.timeIntervalSinceReferenceDate / 5) % SilentLine.lines.count
            // The design's italic is the browser's synthesized oblique (only
            // upright Plex Mono is loaded), a 0.25 skew; the bundled face has
            // no italic for .italic() to pick.
            HStack(spacing: 0) {
                Text(SilentLine.lines[i]).foregroundColor(Theme.redText)
                    .transformEffect(CGAffineTransform(a: 1, b: 0, c: -0.25, d: 1, tx: 2, ty: 0))
                Text(" (can't hear anything)").foregroundColor(Theme.dim)
            }
            .font(Theme.mono(12))
            .frame(maxWidth: .infinity)
        }
    }
}
