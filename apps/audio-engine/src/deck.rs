//! One deck: a decoded track, its transport, and its channel strip.

use std::sync::Arc;

use crate::dsp::{Coeffs, Smoothed, StereoBiquad};
use crate::engine::{EngineError, ErrorCode};
use crate::mixer::{self, Assign};

/// One beat of a track's grid. `downbeat` marks the first beat of a bar, the
/// same fact rekordbox's PQTZ grid carries as beat number 1.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Beat {
    pub time_ms: f64,
    pub downbeat: bool,
}

/// Below this trim x fader x crossfader gain (-60 dB) a playing deck counts
/// as not heard, for the render timeline.
pub const AUDIBLE_GAIN: f64 = 1e-3;

/// A decoded track. Immutable once built, shared into the audio thread as an
/// `Arc` and handed back out when replaced, so it is never freed there.
#[derive(Debug)]
pub struct Track {
    pub sample_rate: u32,
    /// Interleaved stereo f32 at the file's own sample rate.
    pub pcm: Vec<f32>,
    pub frames: usize,
    pub beats: Vec<Beat>,
    /// Frame index (into `beats`) of every downbeat, precomputed for bar lookups.
    downbeats: Vec<usize>,
    /// Tag BPM, used for beat math only when there is no grid.
    pub bpm: Option<f64>,
}

impl Track {
    pub fn new(sample_rate: u32, pcm: Vec<f32>, beats: Vec<Beat>, bpm: Option<f64>) -> Track {
        assert!(pcm.len().is_multiple_of(2), "pcm must be interleaved stereo");
        let frames = pcm.len() / 2;
        let downbeats = beats
            .iter()
            .enumerate()
            .filter(|(_, b)| b.downbeat)
            .map(|(i, _)| i)
            .collect();
        Track { sample_rate, pcm, frames, beats, downbeats, bpm }
    }

    pub fn duration_ms(&self) -> f64 {
        self.frames as f64 * 1000.0 / self.sample_rate as f64
    }

    pub fn ms_to_frames(&self, ms: f64) -> f64 {
        ms * self.sample_rate as f64 / 1000.0
    }

    pub fn frames_to_ms(&self, frames: f64) -> f64 {
        frames * 1000.0 / self.sample_rate as f64
    }

    pub fn has_grid(&self) -> bool {
        self.beats.len() >= 2
    }

    /// Fractional beat index at `ms`, extrapolating past either end of the
    /// grid with its first or last interval.
    pub fn beat_index_at(&self, ms: f64) -> Option<f64> {
        let b = &self.beats;
        if b.len() < 2 {
            let bpm = self.bpm?;
            return Some(ms * bpm / 60000.0);
        }
        if ms <= b[0].time_ms {
            let step = b[1].time_ms - b[0].time_ms;
            return Some((ms - b[0].time_ms) / step);
        }
        let last = b.len() - 1;
        if ms >= b[last].time_ms {
            let step = b[last].time_ms - b[last - 1].time_ms;
            return Some(last as f64 + (ms - b[last].time_ms) / step);
        }
        // partition_point: first beat strictly after ms.
        let hi = b.partition_point(|x| x.time_ms <= ms);
        let lo = hi - 1;
        Some(lo as f64 + (ms - b[lo].time_ms) / (b[hi].time_ms - b[lo].time_ms))
    }

    /// Local tempo of the grid at track time `ms`, from the beat interval
    /// around it; the tag BPM when there is no grid.
    pub fn bpm_at(&self, ms: f64) -> Option<f64> {
        let b = &self.beats;
        if b.len() < 2 {
            return self.bpm;
        }
        let i = self.beat_index_at(ms)?.floor().clamp(0.0, (b.len() - 2) as f64) as usize;
        let interval = b[i + 1].time_ms - b[i].time_ms;
        (interval > 0.0).then(|| 60000.0 / interval)
    }

    /// Time of fractional beat index `idx`, the inverse of `beat_index_at`.
    pub fn beat_time_ms(&self, idx: f64) -> Option<f64> {
        let b = &self.beats;
        if b.len() < 2 {
            let bpm = self.bpm?;
            return Some(idx * 60000.0 / bpm);
        }
        if idx <= 0.0 {
            return Some(b[0].time_ms + idx * (b[1].time_ms - b[0].time_ms));
        }
        let last = b.len() - 1;
        if idx >= last as f64 {
            return Some(b[last].time_ms + (idx - last as f64) * (b[last].time_ms - b[last - 1].time_ms));
        }
        let lo = idx.floor() as usize;
        Some(b[lo].time_ms + (idx - lo as f64) * (b[lo + 1].time_ms - b[lo].time_ms))
    }

    /// Time of bar `bar` (1-based, fractional allowed) on the grid's own
    /// downbeats. Bars past the last downbeat extrapolate in 4-beat bars.
    pub fn bar_time_ms(&self, bar: f64) -> Option<f64> {
        if self.downbeats.is_empty() {
            // No downbeat flags: treat the first grid beat as bar 1.
            return self.beat_time_ms((bar - 1.0) * 4.0);
        }
        let n = self.downbeats.len();
        let whole = (bar - 1.0).floor();
        let frac = (bar - 1.0) - whole;
        let beat_idx = if whole < 0.0 {
            self.downbeats[0] as f64 + (bar - 1.0) * 4.0
        } else if (whole as usize) < n {
            let i = whole as usize;
            let start = self.downbeats[i] as f64;
            let len = if i + 1 < n { self.downbeats[i + 1] as f64 - start } else { 4.0 };
            start + frac * len
        } else {
            self.downbeats[n - 1] as f64 + ((bar - 1.0) - (n - 1) as f64) * 4.0
        };
        self.beat_time_ms(beat_idx)
    }
}

/// Transport and channel strip for one deck. Every field that the audio thread
/// touches is plain data: no allocation happens after construction.
pub struct Deck {
    pub track: Option<Arc<Track>>,
    /// Playhead in source frames of the loaded track.
    pub pos: f64,
    pub playing: bool,
    pub cue: f64,
    pub tempo: f64,
    pub pitch_range: f64,
    /// Loop bounds in source frames.
    pub looping: Option<(f64, f64)>,
    // Knob values as the sender set them, for the state feed and for ramps.
    pub trim: f64,
    pub eq: [f64; 3],
    pub filter: f64,
    pub fader: f64,
    pub assign: Assign,
    strip: Strip,
    /// Position is computed as `anchor + step * run` rather than by adding
    /// `step` each frame, so it does not drift over a long set. Any outside
    /// change to `pos` or the step re-anchors on the next render.
    anchor: f64,
    anchor_step: f64,
    run: u64,
    rendered_pos: f64,
}

struct Strip {
    sr: f64,
    trim: Smoothed,
    eq_db: [Smoothed; 3],
    eq: [StereoBiquad; 3],
    lp_hz: Smoothed,
    hp_hz: Smoothed,
    lp: StereoBiquad,
    hp: StereoBiquad,
    dry: Smoothed,
    lp_wet: Smoothed,
    hp_wet: Smoothed,
    fader: Smoothed,
    xf: Smoothed,
    /// Frames until filter coefficients are next recomputed while gliding.
    coeff_countdown: u32,
    /// Frames rendered since the deck last played a frame of its track.
    idle_run: u64,
    /// After this many idle frames the filter tails are over: state is zeroed
    /// and an idle strip at rest is skipped entirely.
    quiet_after: u64,
}

/// Coefficients glide with their smoothed parameters, recomputed every
/// COEFF_INTERVAL frames. A fixed interval keeps renders independent of the
/// caller's block size.
const COEFF_INTERVAL: u32 = 16;

impl Strip {
    fn new(sr: f64) -> Strip {
        let s = |v: f64| Smoothed::new(v, sr, mixer::PARAM_SMOOTH_S);
        let fp = mixer::filter_params(0.5);
        let mut strip = Strip {
            sr,
            trim: s(mixer::trim_gain_from_knob(0.5)),
            eq_db: [s(0.0), s(0.0), s(0.0)],
            eq: [StereoBiquad::new(Coeffs::IDENTITY); 3],
            lp_hz: s(fp.lp_hz),
            hp_hz: s(fp.hp_hz),
            lp: StereoBiquad::new(Coeffs::IDENTITY),
            hp: StereoBiquad::new(Coeffs::IDENTITY),
            dry: s(fp.dry),
            lp_wet: s(fp.lp_wet),
            hp_wet: s(fp.hp_wet),
            fader: s(1.0),
            xf: s(1.0),
            coeff_countdown: 0,
            // A new strip has never carried audio, so it starts quiet.
            idle_run: sr as u64,
            quiet_after: sr as u64,
        };
        strip.recompute();
        strip
    }

    fn recompute(&mut self) {
        let sr = self.sr;
        self.eq[0].c = Coeffs::lowshelf(sr, mixer::EQ_FREQ_LOW_HZ, self.eq_db[0].value);
        self.eq[1].c = Coeffs::peaking(sr, mixer::EQ_FREQ_MID_HZ, mixer::EQ_MID_Q, self.eq_db[1].value);
        self.eq[2].c = Coeffs::highshelf(sr, mixer::EQ_FREQ_HIGH_HZ, self.eq_db[2].value);
        self.lp.c = Coeffs::lowpass(sr, self.lp_hz.value, mixer::FILTER_Q);
        self.hp.c = Coeffs::highpass(sr, self.hp_hz.value, mixer::FILTER_Q);
    }

    fn coeffs_gliding(&self) -> bool {
        !(self.eq_db[0].settled()
            && self.eq_db[1].settled()
            && self.eq_db[2].settled()
            && self.lp_hz.settled()
            && self.hp_hz.settled())
    }

    fn at_rest(&self) -> bool {
        !self.coeffs_gliding()
            && self.trim.settled()
            && self.dry.settled()
            && self.lp_wet.settled()
            && self.hp_wet.settled()
            && self.fader.settled()
            && self.xf.settled()
    }

    /// One frame of a deck that is not playing its track. Web Audio keeps an
    /// idle channel's parameters moving and its filters ringing out on
    /// silence; so does this, frame by frame, so a deck that starts later
    /// starts from where its knobs actually are. Returns None once the
    /// tails are over and nothing is moving.
    #[inline]
    fn idle(&mut self) -> Option<(f64, f64)> {
        if self.idle_run == self.quiet_after {
            for bq in self.eq.iter_mut().chain([&mut self.lp, &mut self.hp]) {
                bq.reset();
            }
        }
        if self.idle_run >= self.quiet_after && self.at_rest() {
            return None;
        }
        self.idle_run = self.idle_run.saturating_add(1).min(self.quiet_after + 1);
        Some(self.process(0.0, 0.0))
    }

    #[inline]
    fn process(&mut self, l: f64, r: f64) -> (f64, f64) {
        if self.coeff_countdown == 0 {
            if self.coeffs_gliding() {
                for s in self.eq_db.iter_mut() {
                    for _ in 0..COEFF_INTERVAL {
                        s.tick();
                    }
                }
                for _ in 0..COEFF_INTERVAL {
                    self.lp_hz.tick();
                    self.hp_hz.tick();
                }
                self.recompute();
            }
            self.coeff_countdown = COEFF_INTERVAL;
        }
        self.coeff_countdown -= 1;

        let trim = self.trim.tick();
        let (mut l, mut r) = (l * trim, r * trim);
        for bq in self.eq.iter_mut() {
            l = bq.process(0, l);
            r = bq.process(1, r);
        }
        let (dry, lpw, hpw) = (self.dry.tick(), self.lp_wet.tick(), self.hp_wet.tick());
        let (ll, lr) = (self.lp.process(0, l), self.lp.process(1, r));
        let (hl, hr) = (self.hp.process(0, l), self.hp.process(1, r));
        l = dry * l + lpw * ll + hpw * hl;
        r = dry * r + lpw * lr + hpw * hr;
        let g = self.fader.tick() * self.xf.tick();
        (l * g, r * g)
    }
}

impl Deck {
    pub fn new(sr: f64) -> Deck {
        Deck {
            track: None,
            pos: 0.0,
            playing: false,
            cue: 0.0,
            tempo: 1.0,
            pitch_range: 16.0,
            looping: None,
            trim: 0.5,
            eq: [0.5; 3],
            filter: 0.5,
            fader: 1.0,
            assign: Assign::Thru,
            strip: Strip::new(sr),
            anchor: 0.0,
            anchor_step: 0.0,
            run: 0,
            rendered_pos: f64::NAN,
        }
    }

    fn track(&self) -> Result<&Arc<Track>, EngineError> {
        self.track.as_ref().ok_or(EngineError::new(ErrorCode::NoTrack, "no track loaded on this deck"))
    }

    pub fn position_ms(&self) -> f64 {
        self.track.as_ref().map_or(0.0, |t| t.frames_to_ms(self.pos))
    }

    /// Load a track: transport resets, the strip keeps its knob settings.
    /// Returns the track it replaced, for the caller to free off this thread.
    pub fn load(&mut self, track: Arc<Track>) -> Option<Arc<Track>> {
        self.pos = 0.0;
        self.cue = 0.0;
        self.playing = false;
        self.looping = None;
        self.track.replace(track)
    }

    pub fn unload(&mut self) -> Option<Arc<Track>> {
        self.playing = false;
        self.pos = 0.0;
        self.cue = 0.0;
        self.looping = None;
        self.track.take()
    }

    /// Pause stores the cue at the pause position; play at the end restarts
    /// from 0 (`audio-engine.svelte.ts` CUE and transport semantics).
    pub fn play(&mut self, playing: bool) -> Result<(), EngineError> {
        let frames = self.track()?.frames as f64;
        if playing {
            if self.pos >= frames {
                self.pos = 0.0;
            }
        } else if self.playing {
            self.cue = self.pos;
        }
        self.playing = playing;
        Ok(())
    }

    /// CUE while playing returns to the cue point and pauses; while paused it
    /// jumps the playhead to the cue point.
    pub fn cue(&mut self) -> Result<(), EngineError> {
        self.track()?;
        self.pos = self.cue;
        self.playing = false;
        Ok(())
    }

    pub fn seek(&mut self, ms: f64) -> Result<(), EngineError> {
        let t = self.track()?;
        if !ms.is_finite() || ms < 0.0 || ms > t.duration_ms() {
            return Err(EngineError::new(ErrorCode::Invalid, "seek position must be within the track"));
        }
        self.pos = t.ms_to_frames(ms);
        Ok(())
    }

    pub fn set_loop(&mut self, bounds_ms: Option<(f64, f64)>) -> Result<(), EngineError> {
        let Some((in_ms, out_ms)) = bounds_ms else {
            self.looping = None;
            return Ok(());
        };
        let t = self.track()?;
        let dur = t.duration_ms();
        if !(in_ms.is_finite() && out_ms.is_finite()) || in_ms < 0.0 || out_ms <= in_ms {
            return Err(EngineError::new(ErrorCode::Invalid, "loop needs 0 <= in_ms < out_ms"));
        }
        if in_ms >= dur {
            return Err(EngineError::new(ErrorCode::Invalid, "loop cannot start at or past the end of the track"));
        }
        let out_ms = out_ms.min(dur);
        self.looping = Some((t.ms_to_frames(in_ms), t.ms_to_frames(out_ms)));
        Ok(())
    }

    pub fn beat_loop(&mut self, beats: f64, start_ms: Option<f64>) -> Result<(), EngineError> {
        if !(beats.is_finite() && beats > 0.0) {
            return Err(EngineError::new(ErrorCode::Invalid, "beat loop length must be positive"));
        }
        let t = self.track()?.clone();
        let start = start_ms.unwrap_or_else(|| t.frames_to_ms(self.pos));
        let idx = t.beat_index_at(start).ok_or(no_grid())?;
        let end = t.beat_time_ms(idx + beats).ok_or(no_grid())?;
        self.set_loop(Some((start, end)))
    }

    /// Jump by `beats` along the grid. A jump inside an active loop moves the
    /// loop with it, as rekordbox does, so the playhead stays inside it.
    pub fn beat_jump(&mut self, beats: f64) -> Result<(), EngineError> {
        if !beats.is_finite() {
            return Err(EngineError::new(ErrorCode::Invalid, "beat jump must be finite"));
        }
        let t = self.track()?.clone();
        let now = t.frames_to_ms(self.pos);
        let idx = t.beat_index_at(now).ok_or(no_grid())?;
        let target = t.beat_time_ms(idx + beats).ok_or(no_grid())?;
        let target = target.clamp(0.0, t.duration_ms());
        let delta = t.ms_to_frames(target) - self.pos;
        self.pos = t.ms_to_frames(target);
        if let Some((a, b)) = self.looping {
            // Keep the moved loop inside the track at its own length: a jump
            // near either end would otherwise push a bound past 0 or the end.
            let end = t.frames as f64;
            let len = (b - a).min(end);
            let a = (a + delta).clamp(0.0, end - len);
            self.looping = Some((a, a + len));
            if self.pos < a || self.pos >= a + len {
                self.pos = self.pos.clamp(a, a + len);
            }
        }
        Ok(())
    }

    pub fn set_tempo(&mut self, ratio: f64) -> Result<(), EngineError> {
        if !(ratio.is_finite() && ratio > 0.0) {
            return Err(EngineError::new(ErrorCode::Invalid, "tempo ratio must be finite and positive"));
        }
        if (ratio - 1.0).abs() > self.pitch_range / 100.0 + 1e-9 {
            return Err(EngineError::new(ErrorCode::Invalid, "tempo ratio is outside the deck's pitch range"));
        }
        self.tempo = ratio;
        Ok(())
    }

    pub fn set_pitch_range(&mut self, range: f64) -> Result<(), EngineError> {
        if range != 8.0 && range != 16.0 && range != 100.0 {
            return Err(EngineError::new(ErrorCode::Invalid, "pitch range must be 8, 16 or 100"));
        }
        // As the page engine: refuse a range the current tempo does not fit,
        // rather than clamp the tempo silently.
        if (self.tempo - 1.0).abs() > range / 100.0 + 1e-9 {
            return Err(EngineError::new(
                ErrorCode::Invalid,
                "the current tempo is outside that pitch range; reset the tempo first",
            ));
        }
        self.pitch_range = range;
        Ok(())
    }

    pub fn set_trim(&mut self, v: f64) {
        self.trim = v;
        self.strip.trim.set(mixer::trim_gain_from_knob(v));
    }

    pub fn set_eq(&mut self, band: usize, v: f64) {
        self.eq[band] = v;
        self.strip.eq_db[band].set(mixer::eq_db_from_knob(v));
    }

    pub fn set_filter(&mut self, v: f64) {
        self.filter = v;
        let p = mixer::filter_params(v);
        self.strip.lp_hz.set(p.lp_hz);
        self.strip.hp_hz.set(p.hp_hz);
        self.strip.dry.set(p.dry);
        self.strip.lp_wet.set(p.lp_wet);
        self.strip.hp_wet.set(p.hp_wet);
    }

    pub fn set_fader(&mut self, v: f64) {
        self.fader = v;
        self.strip.fader.set(v);
    }

    pub fn set_xf_gain(&mut self, g: f64) {
        self.strip.xf.set(g);
    }

    /// The smoothed trim x fader x crossfader gain, as it stands now. EQ and
    /// filter are left out on purpose: a deck with its lows killed is still
    /// in the mix.
    pub fn level_gain(&self) -> f64 {
        self.strip.trim.value * self.strip.fader.value * self.strip.xf.value
    }

    /// Loaded, playing, and above `AUDIBLE_GAIN`: this deck is being heard.
    pub fn audible(&self) -> bool {
        self.track.is_some() && self.playing && self.level_gain() >= AUDIBLE_GAIN
    }

    /// Source frames advanced per output frame.
    pub fn step(&self, engine_sr: f64) -> f64 {
        self.track.as_ref().map_or(0.0, |t| self.tempo * t.sample_rate as f64 / engine_sr)
    }

    /// Mix `out.len() / 2` frames of this deck into `out` (interleaved stereo).
    /// No allocation: reads the shared PCM and writes into the caller's buffer.
    pub fn render_add(&mut self, out: &mut [f32], engine_sr: f64) {
        let done = self.render_track(out, engine_sr);
        for o in out[done * 2..].chunks_exact_mut(2) {
            let Some((l, r)) = self.strip.idle() else { break };
            o[0] += l as f32;
            o[1] += r as f32;
        }
    }

    /// Plays the track into `out` until it ends; returns frames played.
    fn render_track(&mut self, out: &mut [f32], engine_sr: f64) -> usize {
        let Some(track) = self.track.as_ref() else { return 0 };
        if !self.playing {
            return 0;
        }
        let mut done = 0;
        let pcm = &track.pcm[..];
        let frames = track.frames;
        let end = frames as f64;
        let step = self.tempo * track.sample_rate as f64 / engine_sr;
        if self.pos != self.rendered_pos || step != self.anchor_step {
            self.anchor = self.pos;
            self.anchor_step = step;
            self.run = 0;
        }
        for o in out.chunks_exact_mut(2) {
            if self.pos >= end {
                self.pos = end;
                self.playing = false;
                break;
            }
            let (l, r) = hermite(pcm, frames, self.pos);
            let (l, r) = self.strip.process(l, r);
            o[0] += l as f32;
            o[1] += r as f32;
            done += 1;
            self.run += 1;
            self.pos = self.anchor + step * self.run as f64;
            if let Some((a, b)) = self.looping {
                if self.pos >= b && b > a {
                    self.pos = a + (self.pos - b) % (b - a);
                    self.anchor = self.pos;
                    self.run = 0;
                }
            }
        }
        if done > 0 {
            self.strip.idle_run = 0;
        }
        self.rendered_pos = self.pos;
        done
    }
}

fn no_grid() -> EngineError {
    EngineError::new(ErrorCode::NoBeatgrid, "the track has no beatgrid or bpm to count beats on")
}

/// 4-point, 3rd-order Hermite interpolation of interleaved stereo at a
/// fractional frame position, with edge frames clamped.
#[inline]
fn hermite(pcm: &[f32], frames: usize, pos: f64) -> (f64, f64) {
    let i = pos.floor();
    let t = pos - i;
    let i = i as isize;
    let last = frames as isize - 1;
    let at = |k: isize, ch: usize| -> f64 { pcm[(k.clamp(0, last) as usize) * 2 + ch] as f64 };
    let mut out = [0.0; 2];
    for (ch, o) in out.iter_mut().enumerate() {
        let (xm1, x0, x1, x2) = (at(i - 1, ch), at(i, ch), at(i + 1, ch), at(i + 2, ch));
        let c1 = 0.5 * (x1 - xm1);
        let c2 = xm1 - 2.5 * x0 + 2.0 * x1 - 0.5 * x2;
        let c3 = 0.5 * (x2 - xm1) + 1.5 * (x0 - x1);
        *o = ((c3 * t + c2) * t + c1) * t + x0;
    }
    (out[0], out[1])
}

#[cfg(test)]
mod tests {
    use super::*;

    fn grid_120(bars: usize) -> Vec<Beat> {
        (0..bars * 4).map(|i| Beat { time_ms: i as f64 * 500.0, downbeat: i % 4 == 0 }).collect()
    }

    fn silent(sr: u32, secs: f64, beats: Vec<Beat>) -> Track {
        let frames = (sr as f64 * secs) as usize;
        Track::new(sr, vec![0.0; frames * 2], beats, None)
    }

    #[test]
    fn bar_and_beat_lookups_follow_the_grid() {
        let t = silent(48000, 60.0, grid_120(16));
        assert_eq!(t.bar_time_ms(1.0), Some(0.0));
        assert_eq!(t.bar_time_ms(5.0), Some(8000.0));
        assert_eq!(t.bar_time_ms(5.5), Some(9000.0));
        // Past the last downbeat (bar 16 at 30 s) it extrapolates in 4-beat bars.
        assert_eq!(t.bar_time_ms(20.0), Some(38000.0));
        assert_eq!(t.beat_index_at(1250.0), Some(2.5));
        assert_eq!(t.beat_time_ms(2.5), Some(1250.0));
    }

    #[test]
    fn cue_semantics_match_the_page_engine() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        d.play(true).unwrap();
        let mut buf = vec![0.0f32; 48000 * 2];
        d.render_add(&mut buf, 48000.0);
        // Pausing stores the cue where it paused.
        d.play(false).unwrap();
        assert_eq!(d.cue, 48000.0);
        d.seek(5000.0).unwrap();
        // CUE while paused jumps back to the cue point.
        d.cue().unwrap();
        assert_eq!(d.pos, 48000.0);
        d.play(true).unwrap();
        d.cue().unwrap();
        assert!(!d.playing);
        assert_eq!(d.pos, 48000.0);
    }

    #[test]
    fn loop_wraps_and_stays_in_bounds() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        d.beat_loop(4.0, Some(1000.0)).unwrap();
        assert_eq!(d.looping, Some((48000.0, 144000.0)));
        d.seek(1000.0).unwrap();
        d.play(true).unwrap();
        let mut buf = vec![0.0f32; 512 * 2];
        for _ in 0..1000 {
            d.render_add(&mut buf, 48000.0);
            assert!(d.pos >= 48000.0 && d.pos < 144000.0, "pos {}", d.pos);
        }
        assert!(d.playing);
    }

    #[test]
    fn beat_jump_moves_an_active_loop_with_the_playhead() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 20.0, grid_120(8))));
        d.beat_loop(4.0, Some(0.0)).unwrap();
        d.beat_jump(4.0).unwrap();
        assert_eq!(d.pos, 96000.0);
        assert_eq!(d.looping, Some((96000.0, 192000.0)));
    }

    #[test]
    fn beat_jump_keeps_a_moved_loop_inside_the_track() {
        // 20 s at 120 bpm: 40 beats of 24000 frames.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 20.0, grid_120(10))));
        // Loop [1 s, 2 s] with the playhead at 1.5 s; jumping back 4 beats
        // clamps the playhead to 0, which would drag the loop to [-0.5 s, 0.5 s].
        d.set_loop(Some((1000.0, 2000.0))).unwrap();
        d.seek(1500.0).unwrap();
        d.beat_jump(-4.0).unwrap();
        assert_eq!(d.looping, Some((0.0, 48000.0)));
        assert!(d.pos >= 0.0 && d.pos < 48000.0, "pos {}", d.pos);
        // Near the end: loop [18 s, 19 s], playhead 18.5 s, jump forward 4 beats.
        d.set_loop(Some((18000.0, 19000.0))).unwrap();
        d.seek(18500.0).unwrap();
        d.beat_jump(4.0).unwrap();
        assert_eq!(d.looping, Some((912000.0, 960000.0)));
        assert!(d.pos >= 912000.0 && d.pos <= 960000.0, "pos {}", d.pos);
        // Control: a jump that stays inside the track moves the loop unchanged.
        d.set_loop(Some((4000.0, 6000.0))).unwrap();
        d.seek(5000.0).unwrap();
        d.beat_jump(2.0).unwrap();
        assert_eq!(d.looping, Some((240000.0, 336000.0)));
        assert_eq!(d.pos, 288000.0);
    }

    #[test]
    fn a_loop_ending_at_the_track_end_wraps() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 4.0, grid_120(2))));
        d.set_loop(Some((3000.0, 4000.0))).unwrap();
        assert_eq!(d.looping, Some((144000.0, 192000.0)));
        d.seek(3000.0).unwrap();
        d.play(true).unwrap();
        let mut buf = vec![0.0f32; 480 * 2];
        for _ in 0..500 {
            d.render_add(&mut buf, 48000.0);
            assert!(d.playing, "an end-of-track loop stopped at pos {}", d.pos);
            assert!(d.pos >= 144000.0 && d.pos < 192000.0, "pos {}", d.pos);
        }
    }

    #[test]
    fn pitch_range_refuses_a_range_the_tempo_does_not_fit() {
        let mut d = Deck::new(48000.0);
        d.set_pitch_range(16.0).unwrap();
        d.set_tempo(1.12).unwrap();
        assert!(d.set_pitch_range(8.0).is_err());
        assert_eq!(d.pitch_range, 16.0);
        // Control: a range the tempo fits is taken.
        d.set_tempo(1.05).unwrap();
        d.set_pitch_range(8.0).unwrap();
        assert_eq!(d.pitch_range, 8.0);
    }

    #[test]
    fn playhead_does_not_drift_over_a_long_play() {
        // 110 s at an awkward step (44.1 kHz source, 48 kHz out, tempo 1.07)
        // in uneven blocks. Adding the step every frame drifts by about 1e-3
        // frames here; the anchored form is exact.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(44100, 120.0, vec![])));
        d.set_tempo(1.07).unwrap();
        d.play(true).unwrap();
        let step = d.step(48000.0);
        let mut buf = vec![0.0f32; 997 * 2];
        let mut n = 0u64;
        while n + 997 <= 48000 * 110 {
            d.render_add(&mut buf, 48000.0);
            n += 997;
        }
        assert!((d.pos - step * n as f64).abs() < 1e-7, "drift {}", d.pos - step * n as f64);
    }

    #[test]
    fn tempo_respects_the_pitch_range() {
        let mut d = Deck::new(48000.0);
        assert!(d.set_tempo(1.16).is_ok());
        assert!(d.set_tempo(1.2).is_err());
        d.set_pitch_range(100.0).unwrap();
        assert!(d.set_tempo(1.9).is_ok());
        assert!(d.set_pitch_range(12.0).is_err());
    }

    #[test]
    fn play_at_the_end_restarts_from_zero() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 1.0, vec![])));
        d.play(true).unwrap();
        let mut buf = vec![0.0f32; 60000 * 2];
        d.render_add(&mut buf, 48000.0);
        assert!(!d.playing);
        assert_eq!(d.pos, 48000.0);
        d.play(true).unwrap();
        assert_eq!(d.pos, 0.0);
    }

    #[test]
    fn beat_math_without_a_grid_needs_a_bpm() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, vec![])));
        assert_eq!(d.beat_jump(1.0).unwrap_err().code, ErrorCode::NoBeatgrid);
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(Track::new(48000, vec![0.0; 960000], vec![], Some(120.0))));
        d.beat_jump(2.0).unwrap();
        assert_eq!(d.pos, 48000.0);
    }
}
