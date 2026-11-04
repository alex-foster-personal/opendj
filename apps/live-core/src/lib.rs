//! odj-live-core: the Track Catcher live engine (slice L0).
//!
//! Audio in, live JSON frames out. Feed mono f32 samples at any rate; the
//! [`Analyzer`] resamples to 22050 Hz and, every 512 samples (43 per second),
//! computes one constant-Q chroma column ([`cqt`]), one onset-strength value
//! ([`tempo`]) and one set of tuning peaks ([`tuning`]). At the frame rate
//! asked for (10 Hz by default) it emits a [`Frame`]:
//!
//! - `chroma`: the mean chroma column since the previous frame;
//! - `chroma_fast` / `chroma_slow`: exponential moving averages of the
//!   columns with 1 s and 10 s time constants;
//! - the 24 Krumhansl-Kessler key scores of `chroma_slow` ([`keys`]), the top
//!   three with Camelot, Open Key, name and score, and the top key's
//!   confidence and margin;
//! - the BPM of the last 12 s of onsets (re-read every 0.5 s), and the
//!   tuning offset in cents.
//!
//! [`Analyzer::finish`] returns a [`Summary`]: the whole-track key (scored on
//! the mean chroma, exactly as `apps/analysis_key/profiles.py` scores a
//! track), the tuning, and key-change [`segments`]. The BPM and the segments
//! are withheld (`null`, `pending`) until their constants are calibrated; see
//! [`TEMPO_CALIBRATED`] and [`SEGMENTS_CALIBRATED`].
//!
//! Timing: a frame's `t` is the input clock when it was emitted. Its chroma
//! describes audio centered [`LATENCY_S`] (0.74 s) earlier, because a C1
//! filter is 1.5 s long (librosa's) and the constant-Q frame is centered.
//!
//! Retained history: the summary needs one chroma column, its time and one
//! onset value per hop (about 9 MB an hour), so a stream that runs all day
//! cannot keep them all. [`Analyzer::new_stream`] (what `odj-live
//! --stdin-f32` and the C ABI use) keeps only the last [`STREAM_HISTORY_S`]
//! (30 minutes) as a ring, and its summary describes that window:
//! `window_start_s` is where it starts on the stream clock, `duration_s` its
//! length, and the key, mean chroma, BPM and segments (whose times stay on
//! the stream clock) are the window's. `tuning_cents` and `frames` stay
//! whole-stream (a fixed-size histogram and a count). File analysis
//! ([`analyze_file`], [`Analyzer::new`]) is unbounded: a file is finite.
//! Live frames do not depend on the cap.

pub mod cqt;
pub mod decode;
pub mod keys;
pub mod resample;
pub mod tuning;

// The estimators stay crate-private until calibration: their public entry
// points would hand out the placeholder BPM and segments (TCLIVE-6567d/05).
mod segments;
mod tempo;

pub use crate::segments::SegmentBlock;

#[cfg(feature = "ffi")]
pub mod ffi;

use std::collections::VecDeque;
use std::path::Path;

use serde::Serialize;

use crate::cqt::{ConstantQ, FRAME};
use crate::resample::MonoResampler;
use crate::tempo::Flux;
use crate::tuning::Tuning;

/// The analysis sample rate (librosa's default).
pub const SR: u32 = 22050;
/// Samples between analysis frames (librosa's default hop).
pub const HOP: usize = 512;
/// Seconds between the input clock and the center of the newest chroma column.
pub const LATENCY_S: f64 = (FRAME / 2) as f64 / SR as f64;
pub const FAST_TAU_S: f64 = 1.0;
pub const SLOW_TAU_S: f64 = 10.0;
/// Seconds of onsets the live BPM is read from.
pub const LIVE_BPM_WINDOW_S: f64 = 12.0;
/// The live BPM is re-read at most this often; frames between carry the
/// last reading (it is a third of the CPU at 10 Hz otherwise).
pub const LIVE_BPM_EVERY_S: f64 = 0.5;
/// The constant-Q filters are rebuilt when the tuning estimate has moved at
/// least this far from the tuning they were built for.
const RETUNE_CENTS: f64 = 2.0;
/// ...and no more often than this many hops (about 0.5 s).
const RETUNE_EVERY_HOPS: u64 = 22;
/// A retune rebuilds this many of the 252 filters per hop and swaps them in
/// after 42 hops (about 1 s), so the feed never stalls for the whole 0.24 s
/// rebuild. Measured in release on a drifting tone: worst push 14 ms against
/// the 23 ms hop (275 ms rebuilding all at once, 36 ms at 12 per hop).
const RETUNE_BINS_PER_HOP: usize = 6;
pub const MAX_HZ: f64 = 50.0;
/// Input sample rates the analyzer accepts.
pub const INPUT_RATES: std::ops::RangeInclusive<u32> = 8000..=384_000;
/// TCLIVE-6567d: the tempo prior is an uncalibrated placeholder. Until it is
/// calibrated against an accepted dataset, the BPM is withheld from every
/// frame and summary (`null`), not published as a shipped answer.
pub const TEMPO_CALIBRATED: bool = false;
/// TCLIVE-6567e: the segment HMM constants are uncalibrated placeholders. Until
/// they are calibrated, the summary's segments block is `pending`, not `ok`.
pub const SEGMENTS_CALIBRATED: bool = false;

/// The frame-rate rule, shared by the analyzer and the CLI's argument check.
pub fn check_hz(hz: f64) -> Result<(), String> {
    if hz > 0.0 && hz <= MAX_HZ {
        Ok(())
    } else {
        Err(format!("frame rate must be above 0 and at most {MAX_HZ} Hz, got {hz}"))
    }
}

/// The input sample-rate rule, shared by the analyzer and the CLI's argument check.
pub fn check_rate(rate_in: u32) -> Result<(), String> {
    if INPUT_RATES.contains(&rate_in) {
        Ok(())
    } else {
        Err(format!("input sample rate must be {}..{} Hz, got {rate_in}", INPUT_RATES.start(), INPUT_RATES.end()))
    }
}
/// After this much digital silence (all-zero constant-Q columns) the key
/// averages and the recent tuning are cleared, so a stopped track stops
/// reporting its key and tuning instead of holding them while the averages
/// decay for minutes.
pub const SILENCE_CLEARS_KEY_S: f64 = 1.0;
/// How far past a quarter tone the filters may follow a drifting tuning
/// before they wrap to the other side of the nearest semitone.
const RETUNE_WRAP_CENTS: f64 = 60.0;
/// Seconds of whole-stream history a streaming analyzer keeps for its
/// summary (see "Retained history" above): 30 minutes, about 4.6 MB.
pub const STREAM_HISTORY_S: f64 = 1800.0;
/// Seconds of stream time a frame may wait for [`Analyzer::next_frame`]
/// before it is dropped (and counted in [`Analyzer::dropped_frames`]), so a
/// caller that feeds without draining cannot grow memory without bound. It
/// runs on the stream clock, so the bound holds at any frame rate.
pub const MAX_PENDING_S: f64 = 60.0;
/// Frame times are rounded to 4 dp, so a frame exactly at the bound must not
/// flip on rounding.
const FRAME_TIME_SLACK_S: f64 = 1e-3;

/// Where to rebuild the filters for, or `None` to keep them. The estimate is
/// circular on (-50, 50] cents, so +49 and -49 are 2 cents apart, not 98: the
/// step is taken the short way round from the current tuning, which keeps a
/// track tuned near a quarter tone from flipping its filters (and its chroma)
/// by a semitone every half second. Past [`RETUNE_WRAP_CENTS`] the target
/// wraps by a semitone, so the filters never drift to a whole semitone off
/// A440: a source pitched up by a semitone reads as the key a semitone up,
/// and the 10 cents of slack keep one hovering at +50 from flipping.
fn retune_target(current: f64, estimate: f64) -> Option<f64> {
    let step = (estimate - current + 50.0).rem_euclid(100.0) - 50.0;
    if step.abs() < RETUNE_CENTS {
        return None;
    }
    let mut target = (current + step).round();
    if target > RETUNE_WRAP_CENTS {
        target -= 100.0;
    } else if target < -RETUNE_WRAP_CENTS {
        target += 100.0;
    }
    Some(target)
}

/// One of the top keys in a frame or summary.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct KeyScore {
    pub camelot: String,
    pub openkey: String,
    pub name: String,
    pub score: f64,
}

/// One live frame. Keys and `key` are empty/`null` while `chroma_slow` has
/// no energy (start of stream, silence); `bpm` and `tuning_cents` are `null`
/// until there is enough audio to measure them.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Frame {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub t: f64,
    pub chroma: [f64; 12],
    pub chroma_fast: [f64; 12],
    pub chroma_slow: [f64; 12],
    pub keys: Vec<KeyScore>,
    pub key: Option<String>,
    pub key_name: Option<String>,
    pub key_openkey: Option<String>,
    pub confidence: Option<f64>,
    pub margin: Option<f64>,
    pub bpm: Option<f64>,
    pub tuning_cents: Option<f64>,
}

/// The whole stream, emitted once at the end.
#[derive(Clone, Debug, Serialize, PartialEq)]
pub struct Summary {
    #[serde(rename = "type")]
    pub kind: &'static str,
    pub duration_s: f64,
    pub sample_rate_in: u32,
    pub frames: usize,
    pub analysis_frames: usize,
    pub chroma_mean: [f64; 12],
    pub keys: Vec<KeyScore>,
    pub key: Option<String>,
    pub key_name: Option<String>,
    pub key_openkey: Option<String>,
    pub confidence: Option<f64>,
    pub margin: Option<f64>,
    pub bpm: Option<f64>,
    pub tuning_cents: Option<f64>,
    pub latency_s: f64,
    /// Where the summarized window starts on the stream clock: 0 unless a
    /// bounded stream ran past its retained history, in which case the key,
    /// chroma, BPM, segments and `duration_s` describe only
    /// `[window_start_s, window_start_s + duration_s)`.
    pub window_start_s: f64,
    pub segments: SegmentBlock,
    /// File analysis only: packets that failed to decode and were replaced
    /// by silence of their own length (see [`decode`]). `null` for a stream.
    pub corrupt_packets: Option<u64>,
}

fn round_to(x: f64, places: i32) -> f64 {
    let m = 10f64.powi(places);
    (x * m).round() / m
}

fn round12(v: &[f64; 12]) -> [f64; 12] {
    v.map(|x| round_to(x, 4))
}

fn inf_normalized(v: &[f64; 12]) -> [f64; 12] {
    let max = v.iter().cloned().fold(0.0, f64::max);
    if max > 1e-12 {
        v.map(|x| x / max)
    } else {
        [0.0; 12]
    }
}

struct KeyFields {
    keys: Vec<KeyScore>,
    key: Option<String>,
    key_name: Option<String>,
    key_openkey: Option<String>,
    confidence: Option<f64>,
    margin: Option<f64>,
}

fn key_fields(chroma: &[f64; 12]) -> KeyFields {
    match keys::estimate(chroma) {
        None => KeyFields {
            keys: vec![],
            key: None,
            key_name: None,
            key_openkey: None,
            confidence: None,
            margin: None,
        },
        Some(e) => KeyFields {
            keys: e.order[..3]
                .iter()
                .map(|&i| {
                    let k = keys::Key::from_index(i);
                    KeyScore {
                        camelot: k.camelot(),
                        openkey: k.open_key(),
                        name: k.name(),
                        score: round_to(e.scores[i], 5),
                    }
                })
                .collect(),
            key: Some(e.key.camelot()),
            key_name: Some(e.key.name()),
            key_openkey: Some(e.key.open_key()),
            confidence: Some(round_to(e.confidence, 5)),
            margin: Some(round_to(e.margin, 5)),
        },
    }
}

/// The streaming engine. Not real-time safe (it allocates frames and
/// strings); run it off the audio thread, fed from a ring buffer.
pub struct Analyzer {
    rate_in: u32,
    hz: f64,
    resampler: Option<MonoResampler>,
    staged: Vec<f32>,
    staged_pos: usize,
    window: Vec<f32>,
    /// Samples at [`SR`] shifted into `window`, padding included.
    consumed: u64,
    /// Real (not padding) samples at [`SR`].
    real: u64,
    hops: u64,
    last_retune: u64,
    /// Set while the filters rebuild after a silence reset. Until the rebuild
    /// swaps in, the old bank still labels the chroma with the last source's
    /// pitch classes, so the key averages stay cleared rather than take them.
    settling: bool,
    padding: bool,
    finished: bool,
    cqt: ConstantQ,
    tuning: Tuning,
    flux: Flux,
    /// Seconds of history kept for the summary, `None` for all of it.
    history_s: Option<f64>,
    chroma: VecDeque<[f32; 12]>,
    times: VecDeque<f64>,
    onset: VecDeque<f32>,
    /// The last [`LIVE_BPM_WINDOW_S`] of `onset`, contiguous, for the live BPM.
    recent_onset: Vec<f32>,
    fast: [f64; 12],
    slow: [f64; 12],
    /// Consecutive all-zero constant-Q columns (see [`SILENCE_CLEARS_KEY_S`]).
    silent_hops: u64,
    acc: [f64; 12],
    acc_n: usize,
    last_chroma: [f64; 12],
    next_frame: u64,
    emitted: usize,
    live_bpm: Option<f64>,
    live_bpm_at: Option<f64>,
    ready: VecDeque<Frame>,
    dropped_frames: u64,
}

impl Analyzer {
    /// An analyzer that keeps the whole stream for its summary, for input
    /// known to end (a file).
    /// `rate_in`: the sample rate of what [`Analyzer::push`] is fed.
    /// `hz`: frames per second, above 0 and at most [`MAX_HZ`].
    pub fn new(rate_in: u32, hz: f64) -> Result<Analyzer, String> {
        Analyzer::with_history(rate_in, hz, None)
    }

    /// An analyzer for a stream that may never end: its summary keeps the
    /// last [`STREAM_HISTORY_S`] only (see the crate docs).
    pub fn new_stream(rate_in: u32, hz: f64) -> Result<Analyzer, String> {
        Analyzer::with_history(rate_in, hz, Some(STREAM_HISTORY_S))
    }

    /// As [`Analyzer::new`], keeping `history_s` seconds for the summary
    /// (`None`: all of it). The live BPM reads the last
    /// [`LIVE_BPM_WINDOW_S`] of the same history, so a shorter cap is
    /// refused rather than allowed to change the live frames.
    pub fn with_history(rate_in: u32, hz: f64, history_s: Option<f64>) -> Result<Analyzer, String> {
        if let Some(h) = history_s {
            if !(h.is_finite() && h >= LIVE_BPM_WINDOW_S) {
                return Err(format!("retained history must be at least {LIVE_BPM_WINDOW_S} s, got {h}"));
            }
        }
        check_hz(hz)?;
        check_rate(rate_in)?;
        let resampler = if rate_in == SR {
            None
        } else {
            Some(MonoResampler::new(rate_in, SR)?)
        };
        Ok(Analyzer {
            rate_in,
            hz,
            resampler,
            staged: Vec::new(),
            staged_pos: 0,
            window: vec![0.0; FRAME],
            consumed: 0,
            real: 0,
            hops: 0,
            last_retune: 0,
            settling: false,
            padding: false,
            finished: false,
            cqt: ConstantQ::new(0.0),
            tuning: Tuning::new(HOP as f64 / f64::from(SR)),
            flux: Flux::new(),
            history_s,
            chroma: VecDeque::new(),
            times: VecDeque::new(),
            onset: VecDeque::new(),
            recent_onset: Vec::new(),
            fast: [0.0; 12],
            slow: [0.0; 12],
            silent_hops: 0,
            acc: [0.0; 12],
            acc_n: 0,
            last_chroma: [0.0; 12],
            next_frame: 1,
            emitted: 0,
            live_bpm: None,
            live_bpm_at: None,
            ready: VecDeque::new(),
            dropped_frames: 0,
        })
    }

    /// Feed mono samples at the input rate. Frames that became due are
    /// queued for [`Analyzer::next_frame`]. Non-finite samples are refused
    /// whole, so one bad block cannot poison every average after it.
    pub fn push(&mut self, samples: &[f32]) -> Result<(), String> {
        if self.finished {
            return Err("the analyzer is finished".into());
        }
        if let Some(bad) = samples.iter().position(|s| !s.is_finite()) {
            return Err(format!("sample {bad} of the block is not finite"));
        }
        match self.resampler.as_mut() {
            Some(r) => r.push(samples, &mut self.staged)?,
            None => self.staged.extend_from_slice(samples),
        }
        self.drain_hops();
        self.evict_stale();
        Ok(())
    }

    /// Drops queued frames that have waited [`MAX_PENDING_S`] of stream time,
    /// so a frame waits at most that long even when no new frame arrives
    /// (a frame rate of 0.02 Hz, say, would otherwise hold one for 100 s).
    fn evict_stale(&mut self) {
        let now = self.real as f64 / f64::from(SR);
        while self
            .ready
            .front()
            .is_some_and(|f| now - f.t >= MAX_PENDING_S - FRAME_TIME_SLACK_S)
        {
            self.ready.pop_front();
            self.dropped_frames += 1;
        }
    }

    /// The oldest frame not yet taken.
    pub fn next_frame(&mut self) -> Option<Frame> {
        self.ready.pop_front()
    }

    pub fn pending_frames(&self) -> usize {
        self.ready.len()
    }

    /// Frames dropped, oldest first, because more than [`MAX_PENDING_S`]
    /// of them were waiting undrained.
    pub fn dropped_frames(&self) -> u64 {
        self.dropped_frames
    }

    /// Chroma columns (with their times) held for the summary.
    pub fn retained_columns(&self) -> usize {
        self.chroma.len()
    }

    /// Onset values held for the summary and the live BPM.
    pub fn retained_onsets(&self) -> usize {
        self.onset.len()
    }

    /// Onset values kept: the history's hops, `None` when unbounded.
    fn onset_cap(&self) -> Option<usize> {
        self.history_s.map(|h| (h * f64::from(SR) / HOP as f64).ceil() as usize)
    }

    /// End of stream: flush the resampler and the centered tail, and return
    /// the summary. Frames still queued stay available to `next_frame`.
    pub fn finish(&mut self) -> Result<Summary, String> {
        if self.finished {
            return Err("the analyzer is already finished".into());
        }
        if let Some(r) = self.resampler.as_mut() {
            r.finish(&mut self.staged)?;
        }
        self.drain_hops();
        // The remainder of a hop, then half a frame of zeros, so the last
        // real samples get centered chroma columns (librosa pads the same way).
        let tail = self.staged.len() - self.staged_pos;
        if tail > 0 {
            let real_tail = tail as u64;
            self.staged.resize(self.staged_pos + HOP, 0.0);
            self.process_hop_partial(real_tail);
        }
        // The partial hop can move the stream clock past a queued frame's
        // bound with no frame due, so evict here too.
        self.evict_stale();
        self.padding = true;
        let duration = self.real as f64 / f64::from(SR);
        let pad = FRAME / 2;
        self.staged.extend(std::iter::repeat_n(0.0, pad));
        self.drain_hops();
        self.finished = true;

        // The summarized window: the whole stream, or its retained tail.
        let start = match self.history_s {
            Some(h) if duration > h => duration - h,
            _ => 0.0,
        };
        let window = duration - start;
        // Keep only the columns centered inside the real audio of the window,
        // on the window's clock for the segmenter.
        let keep: Vec<usize> = (0..self.times.len())
            .filter(|&i| self.times[i] >= start && self.times[i] < duration)
            .collect();
        let chroma: Vec<[f32; 12]> = keep.iter().map(|&i| self.chroma[i]).collect();
        let times: Vec<f64> = keep.iter().map(|&i| self.times[i] - start).collect();
        let mut mean = [0f64; 12];
        for c in &chroma {
            for (m, x) in mean.iter_mut().zip(c) {
                *m += f64::from(*x);
            }
        }
        if !chroma.is_empty() {
            mean.iter_mut().for_each(|m| *m /= chroma.len() as f64);
        }
        let frame_rate = f64::from(SR) / HOP as f64;
        let bpm = if TEMPO_CALIBRATED { tempo::estimate_bpm(self.onset.make_contiguous(), frame_rate) } else { None };
        let k = key_fields(&mean);
        let mut segments = if SEGMENTS_CALIBRATED {
            segments::segment(&chroma, &times, window, bpm)
        } else {
            SegmentBlock::pending()
        };
        // Segment times back on the stream clock, where frame `t` lives.
        for seg in &mut segments.segments {
            seg.start_s += start;
            seg.end_s += start;
        }
        Ok(Summary {
            kind: "summary",
            duration_s: round_to(window, 4),
            sample_rate_in: self.rate_in,
            frames: self.emitted,
            analysis_frames: chroma.len(),
            chroma_mean: round12(&mean),
            keys: k.keys,
            key: k.key,
            key_name: k.key_name,
            key_openkey: k.key_openkey,
            confidence: k.confidence,
            margin: k.margin,
            bpm: bpm.map(|b| round_to(b, 2)),
            tuning_cents: self.tuning.cents_total().map(|c| round_to(c, 1)),
            latency_s: round_to(LATENCY_S, 4),
            window_start_s: round_to(start, 4),
            segments,
            corrupt_packets: None,
        })
    }

    fn drain_hops(&mut self) {
        while self.staged.len() - self.staged_pos >= HOP {
            self.process_hop_partial(HOP as u64);
        }
        if self.staged_pos > 0 {
            self.staged.drain(..self.staged_pos);
            self.staged_pos = 0;
        }
    }

    /// Shift one hop into the window; `real` of its samples are audio (the
    /// rest padding).
    fn process_hop_partial(&mut self, real: u64) {
        let hop = &self.staged[self.staged_pos..self.staged_pos + HOP];
        self.window.copy_within(HOP.., 0);
        self.window[FRAME - HOP..].copy_from_slice(hop);
        self.staged_pos += HOP;
        self.consumed += HOP as u64;
        self.hops += 1;
        if !self.padding {
            self.real += real;
            self.tuning.push(&self.window[FRAME - tuning::WINDOW..]);
            let o = self.flux.push(&self.window[FRAME - tempo::FLUX_WINDOW..]);
            self.onset.push_back(o);
            if let Some(cap) = self.onset_cap() {
                while self.onset.len() > cap {
                    self.onset.pop_front();
                }
            }
            if self.cqt.retuning() {
                self.cqt.step_retune(RETUNE_BINS_PER_HOP);
            } else if self.hops - self.last_retune >= RETUNE_EVERY_HOPS {
                if let Some(c) = self.tuning.cents() {
                    if let Some(target) = retune_target(self.cqt.tuning_cents(), c) {
                        self.cqt.begin_retune(target);
                        self.cqt.step_retune(RETUNE_BINS_PER_HOP);
                    }
                }
                self.last_retune = self.hops;
            }
        }
        // The column is centered FRAME/2 samples back; before that it would
        // be centered on the zeros the window started with.
        if self.consumed >= (FRAME / 2) as u64 {
            let center = (self.consumed - (FRAME / 2) as u64) as f64 / f64::from(SR);
            let c = self.cqt.chroma(&self.window);
            // While a silence reset rebuilds, the filters still answering label
            // this column with the last source's pitch classes. It stays out of
            // the summary history too, or the whole-track key would take it.
            if !(self.settling && self.cqt.retuning()) {
                self.chroma.push_back(c);
                self.times.push_back(center);
            }
            // A bounded stream drops columns centered before its window,
            // which ends at the input clock. The padding hops of `finish`
            // add only columns past the end, which it filters out.
            if let (Some(h), false) = (self.history_s, self.padding) {
                let floor = self.real as f64 / f64::from(SR) - h;
                while self.times.front().is_some_and(|&t| t < floor) {
                    self.times.pop_front();
                    self.chroma.pop_front();
                }
            }
            let dt = HOP as f64 / f64::from(SR);
            if c.iter().all(|&x| x == 0.0) {
                self.silent_hops += 1;
                if self.silent_hops as f64 * dt >= SILENCE_CLEARS_KEY_S {
                    self.fast = [0.0; 12];
                    self.slow = [0.0; 12];
                    self.tuning.clear_recent();
                    // The next source is estimated from scratch, so its tuning
                    // must be measured from concert pitch too. Left where the
                    // last source put it (+58 here, -42 there reads as no move),
                    // the filters stay a semitone off the pitch-class labels.
                    // A reset supersedes a retune already in flight, which would
                    // otherwise swap its old target in after the silence. The
                    // flag keeps a reset from restarting on every silent hop.
                    if !self.settling && (self.cqt.retuning() || self.cqt.tuning_cents() != 0.0) {
                        self.cqt.begin_retune(0.0);
                        self.settling = true;
                    }
                }
            } else {
                self.silent_hops = 0;
            }
            if !self.cqt.retuning() {
                self.settling = false;
            }
            if !self.settling {
                let af = 1.0 - (-dt / FAST_TAU_S).exp();
                let a_s = 1.0 - (-dt / SLOW_TAU_S).exp();
                for (i, &ci) in c.iter().enumerate() {
                    let x = f64::from(ci);
                    self.fast[i] += af * (x - self.fast[i]);
                    self.slow[i] += a_s * (x - self.slow[i]);
                    self.acc[i] += x;
                }
                self.acc_n += 1;
            }
        }
        if !self.padding {
            self.emit_due();
        }
    }

    fn emit_due(&mut self) {
        while (self.real as f64) >= self.next_frame as f64 * f64::from(SR) / self.hz {
            let t = self.next_frame as f64 / self.hz;
            self.next_frame += 1;
            let chroma = if self.acc_n > 0 {
                self.acc.map(|s| s / self.acc_n as f64)
            } else {
                self.last_chroma
            };
            self.last_chroma = chroma;
            self.acc = [0.0; 12];
            self.acc_n = 0;
            let k = key_fields(&self.slow);
            if self.live_bpm_at.is_none_or(|at| t - at >= LIVE_BPM_EVERY_S - 1e-9) {
                let frame_rate = f64::from(SR) / HOP as f64;
                let window = (LIVE_BPM_WINDOW_S * frame_rate) as usize;
                self.recent_onset.clear();
                self.recent_onset.extend(self.onset.range(self.onset.len().saturating_sub(window)..));
                self.live_bpm = tempo::estimate_bpm(&self.recent_onset, frame_rate);
                self.live_bpm_at = Some(t);
            }
            let bpm = if TEMPO_CALIBRATED { self.live_bpm } else { None };
            self.evict_stale();
            self.ready.push_back(Frame {
                kind: "frame",
                t: round_to(t, 4),
                chroma: round12(&chroma),
                chroma_fast: round12(&inf_normalized(&self.fast)),
                chroma_slow: round12(&inf_normalized(&self.slow)),
                keys: k.keys,
                key: k.key,
                key_name: k.key_name,
                key_openkey: k.key_openkey,
                confidence: k.confidence,
                margin: k.margin,
                bpm: bpm.map(|b| round_to(b, 2)),
                tuning_cents: self.tuning.cents().map(|c| round_to(c, 1)),
            });
            self.emitted += 1;
        }
    }
}

/// Analyze a whole file: decode, run the analyzer, return every frame and
/// the summary.
pub fn analyze_file(path: &Path, hz: f64) -> Result<(Vec<Frame>, Summary), String> {
    let mut frames = Vec::new();
    let mut summary = None;
    analyze_file_with(path, hz, |f| frames.push(f), |s| summary = Some(s))?;
    Ok((frames, summary.expect("analyze_file_with always ends with a summary")))
}

/// As [`analyze_file`], handing each frame to `on_frame` as it is made.
pub fn analyze_file_with<F: FnMut(Frame), S: FnOnce(Summary)>(path: &Path, hz: f64, mut on_frame: F, on_summary: S) -> Result<(), String> {
    let mut analyzer: Option<Analyzer> = None;
    let info = decode::decode_mono(path, |rate, block| {
        let a = match analyzer.as_mut() {
            Some(a) => a,
            None => analyzer.insert(Analyzer::new(rate, hz)?),
        };
        a.push(block)?;
        while let Some(f) = a.next_frame() {
            on_frame(f);
        }
        Ok(())
    })?;
    let mut a = analyzer.ok_or("no audio decoded")?;
    let mut summary = a.finish()?;
    summary.corrupt_packets = Some(info.corrupt_packets);
    while let Some(f) = a.next_frame() {
        on_frame(f);
    }
    on_summary(summary);
    Ok(())
}

#[cfg(test)]
mod retune_tests {
    use super::retune_target;

    #[test]
    fn a_small_move_keeps_the_filters() {
        assert_eq!(retune_target(10.0, 11.0), None);
        assert_eq!(retune_target(49.0, -49.5), None); // 1.5 cents the short way
    }

    #[test]
    fn a_real_move_retunes_to_the_estimate() {
        assert_eq!(retune_target(0.0, 20.0), Some(20.0));
        assert_eq!(retune_target(20.0, -5.0), Some(-5.0));
    }

    #[test]
    fn the_quarter_tone_boundary_is_crossed_the_short_way() {
        // +49 to -49 is 2 cents, so the filters move to 51, not back to -49.
        assert_eq!(retune_target(49.0, -49.0), Some(51.0));
        assert_eq!(retune_target(-49.0, 49.0), Some(-51.0));
    }

    #[test]
    fn a_drift_past_the_slack_wraps_by_a_semitone() {
        // 58 + 4 = 62 is past the 60-cent slack: the filters go to -38 (the
        // next semitone, 38 flat), not on towards a whole semitone sharp.
        assert_eq!(retune_target(58.0, -38.0), Some(-38.0));
        assert_eq!(retune_target(-58.0, 38.0), Some(38.0));
        // Inside the slack they still follow the short way.
        assert_eq!(retune_target(56.0, -40.0), Some(60.0));
    }
}

#[cfg(test)]
mod pending_tests {
    use super::*;

    #[test]
    fn a_frame_waits_at_most_the_bound_however_low_the_rate() {
        // At 0.02 Hz a frame comes every 50 s. A count cap of two held the
        // first frame until the third arrived, 100 s on; the bound is stream
        // time, so no queued frame may be 60 s old at the end of any block.
        let mut an = Analyzer::new(48_000, 0.02).expect("0.02 Hz is in range");
        for _ in 0..30 {
            an.push(&vec![0.0f32; 48_000 * 10])
                .expect("silence is valid input");
            let now = an.real as f64 / f64::from(SR);
            if let Some(f) = an.ready.front() {
                assert!(now - f.t < MAX_PENDING_S, "frame at {} s waited {} s", f.t, now - f.t);
            }
        }
        assert!(an.dropped_frames() > 0, "frames past the bound were dropped");
    }

    #[test]
    fn finishing_a_stream_also_drops_frames_past_the_bound() {
        // The last partial hop moves the stream clock past a queued frame's
        // bound with no frame due, so finish has to evict as well.
        let mut an = Analyzer::new(48_000, 0.02).expect("0.02 Hz is in range");
        an.push(&vec![0.0f32; 48_000 * 110 + 240])
            .expect("silence is valid input");
        an.finish().expect("the stream finishes");
        let end = an.real as f64 / f64::from(SR);
        while let Some(f) = an.next_frame() {
            assert!(end - f.t < MAX_PENDING_S, "frame at {} s waited {} s", f.t, end - f.t);
        }
    }
}

#[cfg(test)]
mod silence_tests {
    use super::*;

    #[test]
    fn sustained_silence_puts_the_filters_back_at_concert_pitch() {
        let mut an = Analyzer::new(48_000, 10.0).expect("48 kHz is an input rate");
        // A source that ends on a hysteresis branch: +58 cents.
        an.cqt.retune(58.0);
        assert_eq!(an.cqt.tuning_cents(), 58.0);
        // Ten seconds of digital silence: past the key-clearing threshold, and
        // long enough for the gradual rebuild to finish.
        an.push(&vec![0.0f32; 48_000 * 10])
            .expect("silence is valid input");
        assert_eq!(
            an.cqt.tuning_cents(),
            0.0,
            "filters still tuned to the last source"
        );
    }

    #[test]
    fn a_source_right_after_silence_is_not_averaged_until_the_reset_finishes() {
        let mut an = Analyzer::new(48_000, 10.0).expect("48 kHz is an input rate");
        an.cqt.retune(58.0);
        // 2 s of silence: the first column comes after half a frame (0.74 s),
        // then one second of silent columns reaches the key-clearing threshold
        // and the rebuild to concert pitch begins about 0.25 s before the end.
        an.push(&vec![0.0f32; 48_000 * 2]).expect("silence is valid input");
        assert!(an.cqt.retuning(), "the reset should still be rebuilding");
        // A 440 Hz source starts while the old bank is still in use, so its
        // chroma carries the last source's pitch classes: none may reach the
        // key averages before the rebuild swaps in.
        let tone: Vec<f32> = (0..9_600)
            .map(|i| 0.5 * (2.0 * std::f64::consts::PI * 440.0 * i as f64 / 48_000.0).sin() as f32)
            .collect();
        an.push(&tone).expect("a tone is valid input");
        assert!(an.cqt.retuning(), "the reset should still be rebuilding");
        assert_eq!(an.slow, [0.0; 12], "the key average took chroma from the old bank");
    }

    #[test]
    fn a_silence_reset_replaces_a_retune_already_in_flight() {
        let mut an = Analyzer::new(48_000, 10.0).expect("48 kHz is an input rate");
        // 1.2 s of silence, then a drift retune to +20 cents starts and gets
        // partway built before the key-clearing threshold (about 1.74 s).
        an.push(&vec![0.0f32; 48_000 * 12 / 10]).expect("silence is valid input");
        an.cqt.begin_retune(20.0);
        an.push(&vec![0.0f32; 48_000 * 8 / 10]).expect("silence is valid input");
        assert_eq!(
            an.cqt.pending_target(),
            Some(0.0),
            "the silence reset should replace the +20 cent rebuild, not let it swap in"
        );
    }

    #[test]
    fn a_source_during_a_silence_reset_stays_out_of_the_summary_history() {
        let mut an = Analyzer::new(48_000, 10.0).expect("48 kHz is an input rate");
        an.cqt.retune(58.0);
        an.push(&vec![0.0f32; 48_000 * 2]).expect("silence is valid input");
        assert!(an.cqt.retuning(), "the reset should still be rebuilding");
        let kept = an.times.len();
        // 0.2 s of a 440 Hz source while the old bank still answers.
        let tone: Vec<f32> = (0..9_600)
            .map(|i| 0.5 * (2.0 * std::f64::consts::PI * 440.0 * i as f64 / 48_000.0).sin() as f32)
            .collect();
        an.push(&tone).expect("a tone is valid input");
        assert!(an.cqt.retuning(), "the reset should still be rebuilding");
        assert_eq!(
            an.times.len(),
            kept,
            "columns from the old bank reached the summary history"
        );
    }
}
