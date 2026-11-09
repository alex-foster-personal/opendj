import AppKit
import SwiftUI
import TrackCatcherCore

/// The ring: notes heard outside, major keys (B) in the middle, minor keys
/// (A) inside, all at the Camelot clock positions. Geometry follows the
/// design's 260-unit viewBox. The piano layout shows notes only.
struct WheelView: View {
    @ObservedObject var model: AppModel

    private let step = 2 * Double.pi / 12
    /// 260 points, 220 in the compact window.
    private var side: CGFloat { model.compact ? 220 : 260 }

    var body: some View {
        Canvas { ctx, size in
            let s = min(size.width, size.height) / 260
            ctx.scaleBy(x: s, y: s)
            draw(&ctx)
        }
        .frame(width: side, height: side)
        .contentShape(Rectangle())
        .gesture(SpatialTapGesture().onEnded { e in
            tap(at: CGPoint(x: e.location.x * 260 / side, y: e.location.y * 260 / side))
        })
        .help("Click a key to copy it; shift-click for an ASCII wheel; right-click for the piano ring")
        .contextMenu {
            Toggle("Camelot ring", isOn: Binding(get: { model.camelotRing }, set: { if $0 { model.camelotRing = true } }))
            Toggle("Piano ring, notes only", isOn: Binding(get: { !model.camelotRing }, set: { if $0 { model.camelotRing = false } }))
        }
    }

    private struct Layout { let notes0: Double, notesLen: Double }
    private var layout: Layout { model.camelotRing ? .init(notes0: 92, notesLen: 22) : .init(notes0: 62, notesLen: 52) }

    private func arc(_ a0: Double, _ a1: Double, _ r0: Double, _ r1: Double) -> Path {
        var p = Path()
        // Angles from 12 o'clock, clockwise on screen.
        func pt(_ a: Double, _ r: Double) -> CGPoint { CGPoint(x: 130 + r * sin(a), y: 130 - r * cos(a)) }
        p.move(to: pt(a0, r1))
        p.addArc(center: CGPoint(x: 130, y: 130), radius: r1, startAngle: .radians(a0 - .pi / 2),
                 endAngle: .radians(a1 - .pi / 2), clockwise: false)
        p.addLine(to: pt(a1, r0))
        p.addArc(center: CGPoint(x: 130, y: 130), radius: r0, startAngle: .radians(a1 - .pi / 2),
                 endAngle: .radians(a0 - .pi / 2), clockwise: true)
        p.closeSubpath()
        return p
    }

    private func slot(_ k: MusicalKey) -> Int { k.camelotNumber % 12 }

    private func draw(_ ctx: inout GraphicsContext) {
        let shown = model.hold.shown
        let scaleSet = Set(shown?.scale ?? [])
        let fast = model.chromaFast
        let fmax = max(fast.max() ?? 0, 1e-9)
        if model.camelotRing {
            let probs = Dictionary(model.keyScores.map { ($0.camelot, $0.barFraction) }, uniquingKeysWith: max)
            let pmax = max(probs.values.max() ?? 0, 1e-9)
            for k in MusicalKey.all {
                let sl = Double(slot(k))
                let (r0, r1): (Double, Double) = k.minor ? (38, 62) : (64, 88)
                let path = arc(sl * step - step / 2 + 0.02, sl * step + step / 2 - 0.02, r0, r1)
                let isShown = k == shown
                let share = (probs[k.camelot] ?? 0) / pmax
                ctx.fill(path, with: .color(isShown ? Theme.master : Theme.keyColor(k).opacity(0.1 + 0.8 * share)))
                if isShown { ctx.stroke(path, with: .color(.white), lineWidth: 1.5) }
                let rm = (r0 + r1) / 2, am = sl * step
                let color: Color = isShown ? Theme.bg : share > 0.5 ? .white : Theme.dim
                ctx.draw(Text(k.camelot).font(Theme.mono(8.5)).foregroundColor(color),
                         at: CGPoint(x: 130 + rm * sin(am), y: 130 - rm * cos(am)))
            }
        }
        let l = layout
        for slot in 0..<12 {
            // Camelot layout: each note sits where the major key it is tonic of sits.
            let pc = model.camelotRing
                ? (0..<12).first(where: { MusicalKey(pc: $0, minor: false).camelotNumber % 12 == slot }) ?? slot
                : slot
            let v = fast.count == 12 ? fast[pc] / fmax : 0
            let a = Double(slot) * step
            let path = arc(a - step / 2 + 0.02, a + step / 2 - 0.02, l.notes0, l.notes0 + l.notesLen * max(0.08, v))
            let base: Color = pc == shown?.pc ? Theme.master : scaleSet.contains(pc) ? Theme.accent : Theme.barOff
            ctx.fill(path, with: .color(base.opacity(0.35 + 0.65 * v)))
            let r = 124 * 0.98
            ctx.draw(Text(MusicalKey.notes[pc]).font(Theme.mono(11)).foregroundColor(pc == shown?.pc ? .white : Theme.dim),
                     at: CGPoint(x: 130 + r * sin(a), y: 130 - r * cos(a)))
        }
        if let shown {
            ctx.draw(Text(shown.camelot).font(Theme.display(model.camelotRing ? 20 : 30, width: 100)).foregroundColor(.white),
                     at: CGPoint(x: 130, y: model.camelotRing ? 132 : 134))
        }
    }

    private func tap(at p: CGPoint) {
        let shift = NSEvent.modifierFlags.contains(.shift)
        if shift, let k = model.hold.shown { model.copy(CopyText.asciiWheel(current: k)); return }
        if let k = key(at: p) { model.copy(CopyText.wheelKey(k)); return }
        model.copyReadout()
    }

    /// Which key segment a point in the 260-unit wheel space hits.
    func key(at p: CGPoint) -> MusicalKey? {
        guard model.camelotRing else { return nil }
        let dx = p.x - 130, dy = 130 - p.y
        let r = (dx * dx + dy * dy).squareRoot()
        var a = atan2(dx, dy)
        if a < -step / 2 { a += 2 * .pi }
        let slot = Int(((a + step / 2) / step).rounded(.down)) % 12
        let minor: Bool
        switch r {
        case 38..<63: minor = true
        case 63..<88: minor = false
        default: return nil
        }
        return MusicalKey.all.first { $0.minor == minor && $0.camelotNumber % 12 == slot }
    }
}
