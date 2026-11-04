//! Bounded retained history for streams: a cap smaller than the stream
//! keeps memory flat, the summary describes the retained window, and the
//! live frames are exactly those of an unbounded analyzer.

mod common;

use common::*;
use odj_live_core::{Analyzer, Frame, Summary, HOP, LATENCY_S, LIVE_BPM_WINDOW_S, MAX_PENDING_S, SR, STREAM_HISTORY_S};

const E_MAJOR: &[(u8, bool)] = &[(4, false), (1, true), (9, false), (11, false)];
const C_MAJOR: &[(u8, bool)] = &[(0, false), (5, false), (7, false), (0, false)];
/// A small cap keeps the test fast; the stream below is 50 s.
const CAP_S: f64 = 15.0;

struct Run {
    frames: Vec<Frame>,
    summary: Summary,
    max_columns: usize,
    max_onsets: usize,
}

fn feed(mut a: Analyzer, audio: &[f32]) -> Run {
    let mut frames = Vec::new();
    let (mut max_columns, mut max_onsets) = (0, 0);
    for block in audio.chunks(4096) {
        a.push(block).unwrap();
        max_columns = max_columns.max(a.retained_columns());
        max_onsets = max_onsets.max(a.retained_onsets());
        while let Some(f) = a.next_frame() {
            frames.push(f);
        }
    }
    let summary = a.finish().unwrap();
    while let Some(f) = a.next_frame() {
        frames.push(f);
    }
    Run {
        frames,
        summary,
        max_columns,
        max_onsets,
    }
}

/// 35 s of an E major loop, then 15 s of C major: the last 15 s are in a
/// different key from the stream as a whole.
fn e_then_c() -> Vec<f32> {
    let mut buf = silence(50.0);
    add_progression(&mut buf, E_MAJOR, 2.0, 0.0, 35.0);
    add_progression(&mut buf, C_MAJOR, 2.0, 35.0, 15.0);
    add_clicks(&mut buf, 120.0, 0.3);
    normalize(&mut buf);
    buf
}

#[test]
fn a_stream_past_its_cap_keeps_a_bounded_window_and_identical_live_frames() {
    let audio = e_then_c();
    let bounded = feed(Analyzer::with_history(RATE, 10.0, Some(CAP_S)).unwrap(), &audio);
    let whole = feed(Analyzer::new(RATE, 10.0).unwrap(), &audio);

    let hops_per_s = f64::from(SR) / HOP as f64;
    let cap_hops = (CAP_S * hops_per_s).ceil() as usize;
    // The control: unbounded, the history grows with the 50 s stream, so the
    // stream really is longer than the cap.
    assert!(whole.max_columns as f64 > 49.0 * hops_per_s, "{}", whole.max_columns);
    assert!(whole.max_onsets as f64 > 49.0 * hops_per_s, "{}", whole.max_onsets);
    // Bounded: never more than the cap (plus the half frame of padding
    // columns finish adds past the end, which is not reached during push).
    assert!(bounded.max_onsets <= cap_hops, "{} > {cap_hops}", bounded.max_onsets);
    assert!(bounded.max_columns <= cap_hops + 1, "{} > {}", bounded.max_columns, cap_hops + 1);
    // ...and it is full: while pushing, the newest column is centered
    // LATENCY_S behind the input clock.
    assert!(bounded.max_columns as f64 >= (CAP_S - LATENCY_S - 0.05) * hops_per_s, "{}", bounded.max_columns);

    // The summary describes the last 15 s.
    let s = &bounded.summary;
    assert_eq!(s.window_start_s, 35.0, "{s:?}");
    assert_eq!(s.duration_s, CAP_S, "{s:?}");
    assert!((s.analysis_frames as f64 - CAP_S * hops_per_s).abs() <= 2.0, "{}", s.analysis_frames);
    assert_eq!(s.key.as_deref(), Some("8B"), "the window is C major: {s:?}");
    // Segments are pending until TCLIVE-6567e is calibrated, so none are published.
    assert_eq!(s.segments.status, "pending", "{:?}", s.segments);
    assert!(s.segments.segments.is_empty(), "{:?}", s.segments);
    // ...while the unbounded summary of the same audio is the whole 50 s,
    // mostly E major.
    assert_eq!((whole.summary.window_start_s, whole.summary.duration_s), (0.0, 50.0));
    assert_ne!(whole.summary.key, s.key, "{:?}", whole.summary);
    assert_eq!(whole.summary.frames, s.frames);

    // Live frames do not depend on the cap.
    assert_eq!(bounded.frames.len(), 500);
    assert_eq!(bounded.frames, whole.frames);
}

#[test]
fn a_stream_inside_its_cap_is_summarized_whole() {
    // Control for the overshoot: a cap the stream never reaches changes
    // nothing, summary included.
    let audio = e_then_c();
    let bounded = feed(Analyzer::with_history(RATE, 10.0, Some(60.0)).unwrap(), &audio);
    let whole = feed(Analyzer::new(RATE, 10.0).unwrap(), &audio);
    assert_eq!(bounded.summary, whole.summary);
    assert_eq!(bounded.frames, whole.frames);
}

#[test]
fn a_cap_shorter_than_the_live_bpm_window_is_refused() {
    assert!(Analyzer::with_history(RATE, 10.0, Some(LIVE_BPM_WINDOW_S - 0.1)).is_err());
    assert!(Analyzer::with_history(RATE, 10.0, Some(f64::NAN)).is_err());
    assert!(Analyzer::with_history(RATE, 10.0, Some(LIVE_BPM_WINDOW_S)).is_ok());
    assert_eq!(STREAM_HISTORY_S, 1800.0);
}

#[test]
fn undrained_frames_are_capped_and_counted() {
    // 90 s fed without draining at 10 Hz: the queue holds the newest 60 s
    // and counts the rest dropped, so memory stays flat.
    let mut a = Analyzer::new(RATE, 10.0).unwrap();
    for block in silence(90.0).chunks(4096) {
        a.push(block).unwrap();
    }
    let cap = (MAX_PENDING_S * 10.0) as usize;
    assert_eq!(a.pending_frames(), cap);
    let made = a.dropped_frames() as usize + cap;
    assert!((899..=900).contains(&made), "{made} frames made");
    // The oldest went: what waits starts about 60 s back from the end.
    let first = a.next_frame().unwrap();
    assert!((first.t - (made - cap + 1) as f64 / 10.0).abs() < 1e-6, "{}", first.t);
}

#[test]
fn drained_frames_are_never_dropped() {
    // Control: the same stream drained after every block drops nothing.
    let mut a = Analyzer::new(RATE, 10.0).unwrap();
    let mut n = 0;
    for block in silence(90.0).chunks(4096) {
        a.push(block).unwrap();
        while a.next_frame().is_some() {
            n += 1;
        }
    }
    assert_eq!(a.dropped_frames(), 0);
    assert!((899..=900).contains(&n), "{n}");
}
