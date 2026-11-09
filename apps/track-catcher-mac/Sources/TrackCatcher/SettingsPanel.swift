import AppKit
import SwiftUI
import TrackCatcherCore

/// The cog's panel: Input, Level, mic note, Now playing sources, three
/// switches, Run setup again and Quit, row for row the design's `.settings`.
/// The bars and ring views are picked by right-clicking them (or
/// `track-catcher bars|ring`), since the design keeps them out of Settings.
struct SettingsPanel: View {
    @ObservedObject var model: AppModel
    @State private var inputsOpen = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Settings").font(Theme.body(13, .semibold)).foregroundStyle(.white)
                .padding(.bottom, 2)
                .frame(maxWidth: .infinity, minHeight: 32, alignment: .leading)
                .overlay(alignment: .bottom) { Theme.line.frame(height: 1) }
            row("Input", height: 44) { inputPicker }
            row("Level", height: 32) { meter }
            Text(model.micNote).font(Theme.body(11)).foregroundStyle(model.mic == .silent ? Theme.redText : Theme.dim)
                .frame(maxWidth: .infinity, minHeight: 27, alignment: .leading)
                .overlay(alignment: .bottom) { Theme.line.frame(height: 1) }
            row("Now playing from", height: 37) {
                HStack(spacing: 6) {
                    Chip(title: "Spotify", on: $model.nowPlayingSpotify)
                    Chip(title: "Apple Music", on: $model.nowPlayingMusic)
                }
            }
            row("Keep on top of other windows", height: 41) { DevSwitch(on: $model.keepOnTop) }
            row("Launch at login", height: 41) { DevSwitch(on: $model.launchAtLogin) }
            row("Keep running after Open DJ quits", height: 41) { DevSwitch(on: $model.keepRunning) }
            HStack {
                Button("Run setup again", action: model.runSetupAgain).buttonStyle(DevButtonStyle(small: true))
                Spacer()
                Button("Quit Track Catcher") { NSApp.terminate(nil) }.buttonStyle(DevButtonStyle(small: true))
            }
            .padding(.top, 11)
        }
        .font(Theme.body(12))
        .foregroundStyle(Theme.text)
        .padding(.horizontal, 12).padding(.top, 6).padding(.bottom, 10)
        .frame(width: 340)
        .background(RoundedRectangle(cornerRadius: 10).fill(Theme.panel))
        .overlay(RoundedRectangle(cornerRadius: 10).strokeBorder(Theme.line))
        .shadow(color: .black.opacity(0.6), radius: 16, y: 14)
        .overlay(alignment: .topTrailing) {
            if inputsOpen { inputList.padding(.top, 74).padding(.trailing, 12) }
        }
    }

    /// One `.st-row`; `height` is the design's measured row, separator included.
    private func row<Content: View>(_ title: String, height: CGFloat,
                                    @ViewBuilder content: () -> Content) -> some View {
        HStack(spacing: 10) {
            Text(title)
            Spacer(minLength: 0)
            content()
        }
        .frame(minHeight: height)
        .overlay(alignment: .bottom) { Theme.line.frame(height: 1) }
    }

    private var selectedName: String {
        if let uid = model.inputUID, let i = model.inputs.first(where: { $0.uid == uid }) { return i.name }
        return "Default (\(AudioInput.defaultName() ?? "system"))"
    }

    private var inputPicker: some View {
        Button { inputsOpen.toggle() } label: {
            HStack(spacing: 6) {
                Text(selectedName).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 4)
                Image(systemName: "chevron.down").font(.system(size: 9, weight: .semibold))
            }
            .font(Theme.body(12))
            .foregroundStyle(Theme.text)
            .padding(.vertical, 4).padding(.horizontal, 6)
            .frame(width: 172)
            .background(RoundedRectangle(cornerRadius: 6).fill(Theme.selectBg))
            .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(Theme.line))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .help("The input Track Catcher listens to. Default follows the system's default input")
    }

    private var inputList: some View {
        VStack(alignment: .leading, spacing: 0) {
            listRow("Default (\(AudioInput.defaultName() ?? "system"))", selected: model.inputUID == nil) { model.inputUID = nil }
            ForEach(model.inputs) { i in
                listRow(i.name, selected: model.inputUID == i.uid) { model.inputUID = i.uid }
            }
            if let why = model.inputsError {
                Text(why)
                    .font(Theme.body(12)).foregroundStyle(Theme.redText)
                    .padding(.vertical, 6).padding(.horizontal, 8)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(4)
        .frame(width: 200)
        .background(RoundedRectangle(cornerRadius: 8).fill(Theme.panel))
        .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Theme.line))
        .shadow(color: .black.opacity(0.55), radius: 12, y: 10)
    }

    private func listRow(_ title: String, selected: Bool, action: @escaping () -> Void) -> some View {
        Button {
            action()
            inputsOpen = false
        } label: {
            HStack {
                Text(title).lineLimit(1)
                Spacer()
                if selected { Image(systemName: "checkmark").font(.system(size: 10)) }
            }
            .font(Theme.body(12)).foregroundStyle(Theme.text)
            .padding(.vertical, 6).padding(.horizontal, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }

    private var meter: some View {
        // RMS mapped to -60...0 dBFS.
        let db = 20 * log10(max(Double(model.level), 1e-6))
        let fill = max(0.02, min(1, (db + 60) / 60))
        return GeometryReader { g in
            ZStack(alignment: .leading) {
                Capsule().fill(Theme.track)
                Capsule()
                    .fill(LinearGradient(stops: [.init(color: Color(hex: 0x3fae5a), location: 0),
                                                 .init(color: Theme.master, location: 0.7),
                                                 .init(color: Theme.orange, location: 1)],
                                         startPoint: .leading, endPoint: .trailing))
                    .frame(width: g.size.width * fill)
            }
        }
        .frame(width: 200, height: 6)
        .help(String(format: "Input level %.0f dBFS", db))
    }
}

/// The design's `.sw` switch.
struct DevSwitch: View {
    @Binding var on: Bool

    var body: some View {
        Button { on.toggle() } label: {
            ZStack(alignment: on ? .trailing : .leading) {
                Capsule().fill(on ? Theme.accent : Theme.switchOff).frame(width: 32, height: 18)
                Circle().fill(.white).frame(width: 14, height: 14).padding(2)
            }
            .animation(.easeOut(duration: 0.12), value: on)
        }
        .buttonStyle(.plain)
        .accessibilityValue(on ? "on" : "off")
    }
}

/// The design's `.chip`.
struct Chip: View {
    let title: String
    @Binding var on: Bool

    var body: some View {
        Button { on.toggle() } label: {
            Text(title).font(Theme.body(11))
                .foregroundStyle(on ? Color.white : Theme.text)
                .padding(.vertical, 2).padding(.horizontal, 8)
                .overlay(Capsule().strokeBorder(on ? Theme.accent : Theme.line))
                .contentShape(Capsule())
        }
        .buttonStyle(.plain)
    }
}
