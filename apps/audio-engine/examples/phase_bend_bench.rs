//! How hard can a key-locked follower bend its tempo to fix a phase error,
//! and how does that compare with jumping (seeking) the same distance?
//!
//! Renders real audio through the engine on the fake clock and reports:
//!
//! 1. Steady stretcher quality against tempo ratio (Master Tempo on): the
//!    THD+N of a 1 kHz sine, and its pitch in cents. Varispeed pitch for the
//!    same ratio is printed beside it for comparison.
//! 2. What one playing deck costs to render on each path.
//! 3. The click a correction leaves: high-band energy in 2.7 ms windows in
//!    the 40 ms after the event, in dB over the loudest high-band window the
//!    uncorrected render has over the same stretch of track (20 ms either
//!    side). Window alignment alone moves it by about +-6 dB. A bend
//!    of `x` percent held for `shift / x` seconds moves the playhead `shift`
//!    ms, the same as a jump of `shift` ms.
//!
//! `cargo run --release --example phase_bend_bench -- <audio file>...`
//! With no file it uses the tracked bench clip and two acceptance fixtures.

use std::path::PathBuf;
use std::sync::Arc;

use odj_audio::deck::Track;
use odj_audio::decode::decode_at;
use odj_audio::engine::{Engine, EngineCmd};

const SR: u32 = 48000;
const BLOCK: usize = 128;
/// Where the correction starts, in output seconds.
const EVENT_SEC: f64 = 8.0;

fn track(path: &PathBuf) -> Arc<Track> {
    let d = decode_at(path, SR).unwrap_or_else(|e| panic!("{}: {e:?}", path.display()));
    Arc::new(Track::new(SR, d.pcm, Vec::new(), None))
}

fn sine(hz: f64, secs: f64) -> Arc<Track> {
    let n = (SR as f64 * secs) as usize;
    let mut pcm = Vec::with_capacity(n * 2);
    for i in 0..n {
        let v = (2.0 * std::f64::consts::PI * hz * i as f64 / SR as f64).sin() as f32 * 0.5;
        pcm.push(v);
        pcm.push(v);
    }
    Arc::new(Track::new(SR, pcm, Vec::new(), None))
}

/// Render `secs` of deck 1 playing `t`, applying each `(output frame, cmd)`
/// at exactly that frame. Left channel only.
fn render(t: &Arc<Track>, mt: bool, secs: f64, events: &[(usize, EngineCmd)]) -> Vec<f32> {
    let mut e = Engine::new(SR);
    e.apply(EngineCmd::Load { deck: 1, track: t.clone() }).unwrap();
    e.apply(EngineCmd::MasterTempo { deck: 1, enabled: mt }).unwrap();
    e.apply(EngineCmd::Play { deck: 1, playing: true }).unwrap();
    let total = (secs * SR as f64) as usize;
    let mut out = Vec::with_capacity(total);
    let mut buf = vec![0.0f32; BLOCK * 2];
    let mut next = 0;
    let mut at = 0;
    while at < total {
        while next < events.len() && events[next].0 <= at {
            e.apply(events[next].1.clone()).unwrap();
            next += 1;
        }
        let until = events.get(next).map_or(total, |ev| ev.0).min(total);
        let n = BLOCK.min(until - at).max(1);
        e.render(&mut buf[..n * 2]);
        out.extend(buf[..n * 2].chunks_exact(2).map(|f| f[0]));
        at += n;
    }
    out
}

/// High-band (second difference) energy per 128-frame window, in dB.
fn hf_windows(x: &[f32]) -> Vec<f64> {
    x.windows(3)
        .map(|w| (w[2] - 2.0 * w[1] + w[0]) as f64)
        .collect::<Vec<_>>()
        .chunks(BLOCK)
        .map(|c| 10.0 * (c.iter().map(|v| v * v).sum::<f64>() / c.len() as f64 + 1e-20).log10())
        .collect()
}

/// dB by which the loudest high-band window in the 40 ms after each event
/// exceeds the loudest one the reference has over the same stretch of TRACK:
/// `(output frame, frames the corrected render is ahead of the reference
/// there, frames it moves ahead over the next 40 ms)`, with 20 ms either side.
/// So a click is measured against the music it lands on, not a whole second
/// of it.
fn click_db(test: &[f32], reference: &[f32], events: &[(usize, usize, usize)]) -> f64 {
    let (a, b) = (hf_windows(test), hf_windows(reference));
    let w = |f: usize| f / BLOCK;
    let near = (0.04 * SR as f64) as usize;
    let side = (0.02 * SR as f64) as usize;
    let max = |v: &[f64], lo: usize, hi: usize| v[w(lo).min(v.len() - 1)..w(hi).min(v.len())].iter().cloned().fold(f64::MIN, f64::max);
    events
        .iter()
        .map(|&(f, ahead, moves)| {
            let peak = max(&a, f.saturating_sub(2 * BLOCK), f + near);
            let floor = max(&b, (f + ahead).saturating_sub(side), f + ahead + moves + near + side);
            peak - floor
        })
        .fold(f64::MIN, f64::max)
}

/// THD+N of a 1 kHz-ish sine in dB, and its frequency, over `x`.
fn thd_n(x: &[f32]) -> (f64, f64) {
    // Frequency from zero crossings, then a least-squares fit at that frequency.
    let (mut first, mut last, mut n) = (None, 0, 0usize);
    for i in 1..x.len() {
        if x[i - 1] < 0.0 && x[i] >= 0.0 {
            if first.is_none() {
                first = Some(i);
            } else {
                n += 1;
            }
            last = i;
        }
    }
    let hz = n as f64 * SR as f64 / (last - first.unwrap()) as f64;
    let w = 2.0 * std::f64::consts::PI * hz / SR as f64;
    let (mut ss, mut sc, mut cc, mut ys, mut yc) = (0.0, 0.0, 0.0, 0.0, 0.0);
    for (i, &v) in x.iter().enumerate() {
        let (s, c) = ((w * i as f64).sin(), (w * i as f64).cos());
        ss += s * s;
        sc += s * c;
        cc += c * c;
        ys += v as f64 * s;
        yc += v as f64 * c;
    }
    let det = ss * cc - sc * sc;
    let (a, b) = ((ys * cc - yc * sc) / det, (yc * ss - ys * sc) / det);
    let (mut sig, mut res) = (0.0, 0.0);
    for (i, &v) in x.iter().enumerate() {
        let fit = a * (w * i as f64).sin() + b * (w * i as f64).cos();
        sig += fit * fit;
        res += (v as f64 - fit).powi(2);
    }
    (10.0 * (res / sig).log10(), hz)
}

/// Frames a bend of `pct` moves the playhead ahead in the 40 ms after it starts.
fn near_moves(pct: f64) -> usize {
    (0.04 * SR as f64 * pct / 100.0).ceil() as usize
}

fn cents(r: f64) -> f64 {
    1200.0 * r.log2()
}

fn main() {
    let root = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../..");
    // Audio paths from a local bench run; argv[0] is skipped and never trusted.
    // nosemgrep: rust.lang.security.args.args
    let mut files: Vec<PathBuf> = std::env::args().skip(1).map(PathBuf::from).collect();
    if files.is_empty() {
        files = vec![
            root.join("(private path)"),
            root.join("tests/fixtures/app-acceptance/library/04-acceptance-kick-124.mp3"),
            root.join("tests/fixtures/app-acceptance/library/05-acceptance-a-minor.mp3"),
        ];
    }

    println!("## 1. Steady quality against tempo ratio (1 kHz sine)\n");
    println!("| ratio | MT on THD+N dB | MT on pitch cents | MT off pitch cents |");
    println!("|---|---|---|---|");
    let tone = sine(1000.0, 4.0);
    for pct in [0.0, 0.3, 1.0, 2.0, 3.0, 5.0, 8.0] {
        let r = 1.0 + pct / 100.0;
        let out = render(&tone, true, 3.0, &[(0, EngineCmd::Tempo { deck: 1, ratio: r })]);
        let (thd, hz) = thd_n(&out[SR as usize..]);
        println!("| {r:.3} | {thd:.1} | {:+.2} | {:+.1} |", cents(hz / 1000.0), cents(r));
    }

    println!("\n## 2. Render cost of one playing deck (60 s of a 1 kHz sine, block 128)\n");
    println!("| path | ms of CPU per s of audio | x realtime |");
    println!("|---|---|---|");
    let long = sine(1000.0, 61.0);
    for (name, mt) in [("varispeed (MT off)", false), ("stretcher (MT on)", true)] {
        let t0 = std::time::Instant::now();
        let out = render(&long, mt, 60.0, &[(0, EngineCmd::Tempo { deck: 1, ratio: 1.02 })]);
        let per_s = t0.elapsed().as_secs_f64() * 1000.0 / 60.0;
        assert_eq!(out.len(), 60 * SR as usize);
        println!("| {name} | {per_s:.3} | {:.0} |", 1000.0 / per_s);
    }

    let ev = (EVENT_SEC * SR as f64) as usize;
    let mut sources: Vec<(String, Arc<Track>)> = vec![("997 Hz sine".into(), sine(997.0, 40.0))];
    for f in &files {
        sources.push((f.file_name().unwrap().to_string_lossy().into_owned(), track(f)));
    }
    for shift_ms in [15.0, 30.0, 60.0] {
        println!("\n## 3. Correcting {shift_ms} ms: click over the uncorrected render, dB (lower is cleaner; <= 0 is under the music's own peaks)\n");
        print!("| source | jump MT off | jump MT on |");
        let bends = [0.3, 1.0, 2.0, 3.0, 5.0, 8.0];
        for b in bends {
            print!(" bend {b}% ({:.2} s) |", shift_ms / 1000.0 / (b / 100.0));
        }
        println!();
        println!("|---|---|---|{}", "---|".repeat(bends.len()));
        for (name, t) in &sources {
            let secs = EVENT_SEC + 2.0;
            let mut row = format!("| {name} |");
            for mt in [false, true] {
                let reference = render(t, mt, secs + 1.0, &[]);
                let to = EVENT_SEC * 1000.0 + shift_ms;
                let jumped = render(t, mt, secs, &[(ev, EngineCmd::Seek { deck: 1, position_ms: to, quantize: false })]);
                let shift = (shift_ms / 1000.0 * SR as f64) as usize;
                row += &format!(" {:+.1} |", click_db(&jumped, &reference, &[(ev, 0, shift)]));
            }
            let longest = shift_ms / 1000.0 / (bends[0] / 100.0);
            let reference = render(t, true, secs + longest + 1.0, &[]);
            for b in bends {
                let hold = ((shift_ms / 1000.0) / (b / 100.0) * SR as f64) as usize;
                let back = ev + hold;
                let shift = (shift_ms / 1000.0 * SR as f64) as usize;
                let bent = render(
                    t,
                    true,
                    secs + hold as f64 / SR as f64,
                    &[
                        (ev, EngineCmd::Tempo { deck: 1, ratio: 1.0 + b / 100.0 }),
                        (back, EngineCmd::Tempo { deck: 1, ratio: 1.0 }),
                    ],
                );
                row += &format!(" {:+.1} |", click_db(&bent, &reference, &[(ev, 0, (near_moves(b))), (back, shift, 0)]));
            }
            println!("{row}");
        }
    }
}
