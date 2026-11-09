import CoreText
import SwiftUI
import TrackCatcherCore

/// The design's dev skin, value for value (track-catcher-design.html :root).
enum Theme {
    static let bg = Color(hex: 0x0d0f12)
    static let panel = Color(hex: 0x14171d)
    static let line = Color(hex: 0x23282f)
    static let text = Color(hex: 0xc8cdd2)
    static let dim = Color(hex: 0x838990)
    static let master = Color(hex: 0xc9b35a)
    static let accent = Color(hex: 0x4f8ff0)
    static let bpm = Color(hex: 0xF5C9B6)
    static let orange = Color(hex: 0xD97757)
    static let orangeDark = Color(hex: 0x9c4b34)
    static let red = Color(hex: 0xd0342c)
    static let redText = Color(hex: 0xff6b62)
    static let barOff = Color(hex: 0x3b4250)
    static let track = Color(hex: 0x22272f)
    static let switchOff = Color(hex: 0x3a4048)
    static let selectBg = Color(hex: 0x0f1216)

    /// IBM Plex Mono, as in the design: 400, and 500 for anything heavier.
    /// The faces ship in Contents/Resources/Fonts (ATSApplicationFontsPath).
    static func mono(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .custom(weight == .regular ? "IBMPlexMono-Regular" : "IBMPlexMono-Medium", fixedSize: size)
    }

    /// IBM Plex Sans, as in the design: 400, and 600 for anything heavier.
    static func body(_ size: CGFloat, _ weight: Font.Weight = .regular) -> Font {
        .custom(weight == .regular ? "IBMPlexSans" : "IBMPlexSans-SmBld", fixedSize: size)
    }

    /// Anybody 800, as in the design: the bundled variable face with its
    /// wght and wdth axes set. The big key is 130% wide; the ring's center
    /// label has no font-stretch in the design, so it passes 100. Falls back
    /// to the system's heavy expanded face if Anybody did not load.
    static func display(_ size: CGFloat, width: CGFloat = 130) -> Font {
        let base = CTFontDescriptorCreateWithNameAndSize("Anybody-Thin" as CFString, size)
        // CoreText substitutes a default face for a name it can't find.
        guard CTFontCopyFamilyName(CTFontCreateWithFontDescriptor(base, size, nil)) as String == "Anybody" else {
            return .system(size: size, weight: .heavy).width(.expanded)
        }
        func axis(_ tag: String) -> Int { tag.utf8.reduce(0) { $0 << 8 | Int($1) } }
        let varied = CTFontDescriptorCreateCopyWithAttributes(
            base, [kCTFontVariationAttribute: [axis("wght"): 800, axis("wdth"): width]] as CFDictionary)
        return Font(CTFontCreateWithFontDescriptor(varied, size, nil))
    }

    /// hsl(camelot*30, 55%, 56% major / 46% minor), the wheel's key colors.
    static func keyColor(_ k: MusicalKey) -> Color {
        let hue = Double((k.camelotNumber + 4) % 12) * 30
        return Color(hsl: hue, 0.55, k.minor ? 0.46 : 0.56)
    }
}

extension Color {
    init(hex: UInt32, opacity: Double = 1) {
        self.init(.sRGB, red: Double((hex >> 16) & 0xff) / 255, green: Double((hex >> 8) & 0xff) / 255,
                  blue: Double(hex & 0xff) / 255, opacity: opacity)
    }

    init(hsl h: Double, _ s: Double, _ l: Double) {
        let c = (1 - abs(2 * l - 1)) * s
        let hp = h.truncatingRemainder(dividingBy: 360) / 60
        let x = c * (1 - abs(hp.truncatingRemainder(dividingBy: 2) - 1))
        let (r, g, b): (Double, Double, Double)
        switch hp {
        case ..<1: (r, g, b) = (c, x, 0)
        case ..<2: (r, g, b) = (x, c, 0)
        case ..<3: (r, g, b) = (0, c, x)
        case ..<4: (r, g, b) = (0, x, c)
        case ..<5: (r, g, b) = (x, 0, c)
        default: (r, g, b) = (c, 0, x)
        }
        let m = l - c / 2
        self.init(.sRGB, red: r + m, green: g + m, blue: b + m)
    }
}

/// The design's `.dbtn`.
struct DevButtonStyle: ButtonStyle {
    var catchStyle = false
    var small = false
    var corners: RectangleCornerRadii = .init(topLeading: 6, bottomLeading: 6, bottomTrailing: 6, topTrailing: 6)

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(Theme.mono(12, .medium))
            .foregroundStyle(catchStyle ? Color.white : Theme.text)
            // The design's 29 px button: a 1 px border and 6 px padding around
            // Plex Mono's 15.6 px line; the border here is drawn inside.
            .frame(minHeight: 17)
            .padding(.vertical, 6)
            .padding(.horizontal, small ? 8 : 10)
            .background(UnevenRoundedRectangle(cornerRadii: corners).fill(Theme.panel))
            .overlay(UnevenRoundedRectangle(cornerRadii: corners)
                .strokeBorder(catchStyle ? Theme.accent : Theme.line, lineWidth: 1))
            .opacity(configuration.isPressed ? 0.8 : 1)
            .contentShape(Rectangle())
    }
}

/// The ODJ logo: two half discs.
struct ODJLogo: View {
    var body: some View {
        Canvas { ctx, size in
            let s = size.width / 512
            var a = Path()
            a.move(to: CGPoint(x: 367.72 * s, y: 144.28 * s))
            a.addArc(center: CGPoint(x: 234.785 * s, y: 277.215 * s), radius: 188 * s,
                     startAngle: .degrees(-45), endAngle: .degrees(135), clockwise: true)
            a.closeSubpath()
            ctx.fill(a, with: .color(Theme.orangeDark))
            var b = Path()
            b.move(to: CGPoint(x: 410.15 * s, y: 101.85 * s))
            b.addArc(center: CGPoint(x: 277.215 * s, y: 234.785 * s), radius: 188 * s,
                     startAngle: .degrees(-45), endAngle: .degrees(135), clockwise: false)
            b.closeSubpath()
            ctx.fill(b, with: .color(Theme.orange))
        }
    }
}
