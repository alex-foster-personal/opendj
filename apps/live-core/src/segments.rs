//! Key-change segments: windowed chroma, HMM/Viterbi over windows.
//!
//! A port of `apps/analysis_key/segments.py` with one difference, the bar
//! grid. That module takes downbeats from the library's own beatgrid; a live
//! stream has none, so a window here is one bar of four beats at the
//! track's estimated BPM ("bar-ish"), or a fixed 2 s when there is no BPM.
//! Everything after the grid is the same decision, with the same constants:
//!
//! - emissions are the shipping key profile's own scores per window
//!   ([`crate::keys::scores`]), scaled by `EMISSION_GAIN = 24` from their
//!   window maximum and log-softmaxed;
//! - the chain stays put with probability `SELF_TRANSITION_PROB = 0.98` and
//!   moves to each of the other 23 keys with the rest split evenly, which is
//!   the switch penalty;
//! - a run shorter than [`MIN_SEGMENT_WINDOWS`] is merged into its wider
//!   neighbor (the previous one on a tie), then same-key neighbors are
//!   joined;
//! - fewer windows than one minimum segment is `failed` / `too_few_bars`,
//!   never an `ok` block with an empty list;
//! - a segment's confidence is the mean score of its key over its windows,
//!   clamped to 0..1, not a Viterbi score inflated by run length.
//!
//! Both HMM constants are the Python module's uncalibrated placeholders.

use serde::Serialize;

use crate::keys::{self, Key, EMISSION_GAIN, N_KEYS};

pub const MIN_SEGMENT_WINDOWS: usize = 8;
pub const SELF_TRANSITION_PROB: f64 = 0.98;
pub const FALLBACK_WINDOW_S: f64 = 2.0;
pub const REASON_TOO_FEW_BARS: &str = "too_few_bars";
/// Every window is silent, so there is no key to segment (all 24 scores tie
/// and Viterbi would otherwise label it C major).
pub const REASON_SILENT: &str = "silent";

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Segment {
    pub start_bar: usize,
    pub end_bar: usize,
    pub start_s: f64,
    pub end_s: f64,
    pub key_camelot: String,
    pub key_openkey: String,
    pub key_name: String,
    pub confidence: f64,
}

#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct SegmentBlock {
    pub status: &'static str,
    pub reason: Option<&'static str>,
    /// `bar` (four beats at the summary BPM) or `fixed` (2 s, no BPM).
    pub window: &'static str,
    pub window_s: f64,
    pub segments: Vec<Segment>,
}

impl SegmentBlock {
    /// The block while the segment constants are uncalibrated (TCLIVE-6567e):
    /// no segments, and a status that says so rather than an `ok` answer.
    pub fn pending() -> SegmentBlock {
        SegmentBlock { status: "pending", reason: Some("uncalibrated"), window: "fixed", window_s: FALLBACK_WINDOW_S, segments: vec![] }
    }
}

#[derive(Clone, Copy, Debug)]
struct Run {
    key: usize,
    start: usize,
    end: usize,
}

impl Run {
    fn width(&self) -> usize {
        self.end - self.start
    }
}

/// Segment chroma frames (`times[i]` is the center of `chroma[i]`) over
/// `duration_s`, with bars from `bpm` when there is one.
pub fn segment(chroma: &[[f32; 12]], times: &[f64], duration_s: f64, bpm: Option<f64>) -> SegmentBlock {
    assert_eq!(chroma.len(), times.len());
    let (window, window_s) = match bpm {
        Some(b) if b > 0.0 => ("bar", 4.0 * 60.0 / b),
        _ => ("fixed", FALLBACK_WINDOW_S),
    };
    let n_windows = window_count(times, duration_s, window_s);
    let failed = |reason| SegmentBlock {
        status: "failed",
        reason: Some(reason),
        window,
        window_s,
        segments: vec![],
    };
    if n_windows < MIN_SEGMENT_WINDOWS {
        return failed(REASON_TOO_FEW_BARS);
    }
    // One mean chroma column per window; a window with no frame borrows the
    // nearest frame, as `bar_chroma` does, rather than inventing a column.
    let mut sums = vec![[0f64; 12]; n_windows];
    let mut counts = vec![0usize; n_windows];
    for (c, &t) in chroma.iter().zip(times) {
        if t < 0.0 {
            continue;
        }
        let w = (t / window_s).floor() as usize;
        if w < n_windows {
            for (s, x) in sums[w].iter_mut().zip(c) {
                *s += f64::from(*x);
            }
            counts[w] += 1;
        }
    }
    let columns: Vec<[f64; 12]> = (0..n_windows)
        .map(|w| {
            if counts[w] > 0 {
                sums[w].map(|s| s / counts[w] as f64)
            } else if times.is_empty() {
                [0.0; 12]
            } else {
                let start = w as f64 * window_s;
                let nearest = (0..times.len())
                    .min_by(|&a, &b| (times[a] - start).abs().partial_cmp(&(times[b] - start).abs()).unwrap())
                    .unwrap();
                chroma[nearest].map(f64::from)
            }
        })
        .collect();
    if columns.iter().all(|c| c.iter().all(|&x| x.abs() < 1e-12)) {
        return failed(REASON_SILENT);
    }
    let scores: Vec<[f64; N_KEYS]> = columns.iter().map(keys::scores).collect();
    let path = viterbi(&scores);
    let runs = merge_short_runs(runs(&path), MIN_SEGMENT_WINDOWS);
    let segments = runs
        .iter()
        .map(|r| {
            let conf = (r.start..r.end).map(|w| scores[w][r.key]).sum::<f64>() / r.width() as f64;
            let key = Key::from_index(r.key);
            Segment {
                start_bar: r.start,
                end_bar: r.end,
                start_s: r.start as f64 * window_s,
                // The trailing window is partial: the stream's own length
                // closes it, as the record's duration closes segments.py's
                // final bar.
                end_s: (r.end as f64 * window_s).min(duration_s),
                key_camelot: key.camelot(),
                key_openkey: key.open_key(),
                key_name: key.name(),
                confidence: conf.clamp(0.0, 1.0),
            }
        })
        .collect();
    SegmentBlock {
        status: "ok",
        reason: None,
        window,
        window_s,
        segments,
    }
}

/// Whole windows, plus the trailing partial one when a chroma column is
/// centered inside it. `segments.py` closes its final bar with the record's
/// duration rather than dropping it; a window with no column of its own here
/// would only copy its neighbor's, so a sliver shorter than one hop is left
/// out instead of counted as a bar.
fn window_count(times: &[f64], duration_s: f64, window_s: f64) -> usize {
    let whole = (duration_s / window_s).floor() as usize;
    let tail_start = whole as f64 * window_s;
    let tail_has_column = times.iter().any(|&t| t >= tail_start && t < duration_s);
    whole + usize::from(tail_start < duration_s && tail_has_column)
}

fn log_emissions(scores: &[f64; N_KEYS]) -> [f64; N_KEYS] {
    let max = scores.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let scaled = scores.map(|s| (s - max) * EMISSION_GAIN);
    let peak = scaled.iter().cloned().fold(f64::NEG_INFINITY, f64::max);
    let total = (scaled.iter().map(|s| (s - peak).exp()).sum::<f64>() + 1e-12).ln();
    scaled.map(|s| s - (peak + total))
}

fn viterbi(scores: &[[f64; N_KEYS]]) -> Vec<usize> {
    let stay = SELF_TRANSITION_PROB.ln();
    let mv = ((1.0 - SELF_TRANSITION_PROB) / (N_KEYS - 1) as f64).ln();
    let mut acc = log_emissions(&scores[0]);
    let mut back: Vec<[usize; N_KEYS]> = Vec::with_capacity(scores.len());
    for s in &scores[1..] {
        let e = log_emissions(s);
        let mut next = [0f64; N_KEYS];
        let mut bp = [0usize; N_KEYS];
        for to in 0..N_KEYS {
            // np.argmax keeps the first maximum: iterate from 0, strict >.
            let mut best = f64::NEG_INFINITY;
            for (from, a) in acc.iter().enumerate() {
                let v = a + if from == to { stay } else { mv };
                if v > best {
                    best = v;
                    bp[to] = from;
                }
            }
            next[to] = best + e[to];
        }
        acc = next;
        back.push(bp);
    }
    let mut last = 0;
    for k in 1..N_KEYS {
        if acc[k] > acc[last] {
            last = k;
        }
    }
    let mut path = vec![last];
    for bp in back.iter().rev() {
        let prev = bp[*path.last().unwrap()];
        path.push(prev);
    }
    path.reverse();
    path
}

fn runs(path: &[usize]) -> Vec<Run> {
    let mut out: Vec<Run> = Vec::new();
    for (w, &k) in path.iter().enumerate() {
        match out.last_mut() {
            Some(r) if r.key == k => r.end = w + 1,
            _ => out.push(Run {
                key: k,
                start: w,
                end: w + 1,
            }),
        }
    }
    out
}

fn coalesce(runs: Vec<Run>) -> Vec<Run> {
    let mut out: Vec<Run> = Vec::new();
    for r in runs {
        match out.last_mut() {
            Some(p) if p.key == r.key => p.end = r.end,
            _ => out.push(r),
        }
    }
    out
}

#[allow(clippy::if_same_then_else)] // the branches mirror segments.py one for one
fn merge_short_runs(runs: Vec<Run>, floor: usize) -> Vec<Run> {
    let mut merged = runs;
    while merged.len() > 1 {
        let Some(index) = (0..merged.len())
            .filter(|&i| merged[i].width() < floor)
            .min_by_key(|&i| (merged[i].width(), i))
        else {
            break;
        };
        let target = if index == 0 {
            1
        } else if index == merged.len() - 1 {
            index - 1
        } else if merged[index - 1].width() >= merged[index + 1].width() {
            index - 1
        } else {
            index + 1
        };
        let (first, second) = (index.min(target), index.max(target));
        let joined = Run {
            key: merged[target].key,
            start: merged[first].start,
            end: merged[second].end,
        };
        merged.splice(first..=second, [joined]);
    }
    coalesce(merged)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn triad(pcs: [usize; 3]) -> [f32; 12] {
        let mut c = [0f32; 12];
        for p in pcs {
            c[p] = 1.0;
        }
        c
    }

    #[test]
    fn a_short_excursion_is_merged_and_a_long_change_is_kept() {
        let mut runs_in = vec![
            Run { key: 0, start: 0, end: 10 },
            Run {
                key: 5,
                start: 10,
                end: 13,
            },
            Run {
                key: 0,
                start: 13,
                end: 30,
            },
        ];
        let out = merge_short_runs(runs_in.clone(), 8);
        assert_eq!(out.len(), 1);
        assert_eq!((out[0].start, out[0].end, out[0].key), (0, 30, 0));
        runs_in[1].end = 20;
        runs_in[2].start = 20;
        assert_eq!(merge_short_runs(runs_in, 8).len(), 3);
    }

    #[test]
    fn a_key_change_halfway_is_two_segments() {
        // 20 windows of C major triads then 20 of F# minor, one frame per 0.5 s.
        let mut chroma = vec![];
        let mut times = vec![];
        for i in 0..160 {
            chroma.push(if i < 80 { triad([0, 4, 7]) } else { triad([6, 9, 1]) });
            times.push(i as f64 * 0.5);
        }
        let block = segment(&chroma, &times, 80.0, None);
        assert_eq!(block.status, "ok");
        assert_eq!(block.segments.len(), 2, "{block:?}");
        assert_eq!(block.segments[0].key_camelot, "8B");
        assert_eq!(block.segments[1].key_camelot, "11A");
        assert_eq!(block.segments[1].start_bar, 20);
        // Too short for one minimum segment.
        assert_eq!(segment(&chroma[..20], &times[..20], 10.0, None).reason, Some(REASON_TOO_FEW_BARS));
    }

    fn c_major_frames(duration_s: f64, step_s: f64) -> (Vec<[f32; 12]>, Vec<f64>) {
        let n = (duration_s / step_s).ceil() as usize;
        let times: Vec<f64> = (0..n).map(|i| i as f64 * step_s).filter(|&t| t < duration_s).collect();
        (vec![triad([0, 4, 7]); times.len()], times)
    }

    #[test]
    fn the_trailing_partial_window_is_kept_and_ends_at_the_stream() {
        // 81 s at 2 s windows: 40 whole windows and a 1 s tail with frames in it.
        let (chroma, times) = c_major_frames(81.0, 0.5);
        let block = segment(&chroma, &times, 81.0, None);
        assert_eq!(block.status, "ok");
        let last = block.segments.last().unwrap();
        assert_eq!(last.end_bar, 41, "{block:?}");
        assert_eq!(last.end_s, 81.0, "{block:?}");
        // Control, the overshoot: a stream that fills its windows exactly
        // gets no extra window and does not end past itself.
        let (chroma, times) = c_major_frames(80.0, 0.5);
        let block = segment(&chroma, &times, 80.0, None);
        let last = block.segments.last().unwrap();
        assert_eq!((last.end_bar, last.end_s), (40, 80.0), "{block:?}");
        // A tail no frame is centered in is not a window of its own.
        let (chroma, times) = c_major_frames(80.0, 0.5);
        let block = segment(&chroma, &times, 80.3, None);
        assert_eq!(block.segments.last().unwrap().end_bar, 40, "{block:?}");
    }

    #[test]
    fn the_partial_window_counts_toward_the_minimum() {
        // 7 whole windows and a partial eighth: segments.py counts the
        // final bar, so this is enough to segment; 14 s (7 windows) is not.
        let (chroma, times) = c_major_frames(15.0, 0.5);
        assert_eq!(segment(&chroma, &times, 15.0, None).status, "ok");
        let (chroma, times) = c_major_frames(14.0, 0.5);
        assert_eq!(segment(&chroma, &times, 14.0, None).reason, Some(REASON_TOO_FEW_BARS));
    }

    #[test]
    fn silence_long_enough_to_segment_fails_rather_than_naming_a_key() {
        let chroma = vec![[0f32; 12]; 80];
        let times: Vec<f64> = (0..80).map(|i| i as f64 * 0.5).collect();
        let block = segment(&chroma, &times, 40.0, None);
        assert_eq!(block.status, "failed");
        assert_eq!(block.reason, Some(REASON_SILENT));
        assert!(block.segments.is_empty());
    }
}
