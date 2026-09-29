//! One deck: a decoded track, its transport, and its channel strip.

use std::sync::Arc;

use crate::dsp::{Coeffs, Ramped, Smoothed, StereoBiquad};
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

/// Largest beat count a beat loop or beat jump takes, either way. Far past
/// any track (65536 beats is over 4 hours at 250 BPM) and small enough that
/// beat-index arithmetic on it cannot overflow or saturate.
pub const MAX_BEATS: f64 = 65536.0;

/// A decoded track. Immutable once built, shared into the audio thread as an
/// `Arc` and handed back out when replaced, so it is never freed there.
#[derive(Debug)]
pub struct Track {
    pub sample_rate: u32,
    /// Interleaved stereo f32 at the file's own sample rate.
    /// Shared, so tracks decoded from one file hold one copy.
    pub pcm: Arc<[f32]>,
    pub frames: usize,
    pub beats: Vec<Beat>,
    /// Frame index (into `beats`) of every downbeat, precomputed for bar lookups.
    downbeats: Vec<usize>,
    /// Tag BPM, used only when there is no grid: for plan beat and bar
    /// times and the tempo readout. Beat loops and jumps need a real grid.
    pub bpm: Option<f64>,
}

impl Track {
    pub fn new(sample_rate: u32, pcm: impl Into<Arc<[f32]>>, beats: Vec<Beat>, bpm: Option<f64>) -> Track {
        let pcm = pcm.into();
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

    /// The quantize point nearest `ms` on a `grid`-beat grid, as the page's
    /// `quantizeToNearestGridBeat` picks it: any grid beat for 1, a downbeat
    /// for 4, every other downbeat from the first for 8. With no downbeats
    /// it falls back to any beat, and 8 with one downbeat uses that one. The
    /// earlier point wins a tie. Needs a non-empty grid; allocates nothing.
    pub fn quantize_ms(&self, ms: f64, grid: u8) -> f64 {
        if grid == 1 || self.downbeats.is_empty() {
            return self.beats[self.nearest_beat(ms)].time_ms;
        }
        let step = if grid == 8 && self.downbeats.len() > 1 { 2 } else { 1 };
        let n = self.downbeats.len().div_ceil(step);
        let at = |j: usize| self.beats[self.downbeats[j * step]].time_ms;
        // First point at or after ms, by bisection over the selected points.
        let (mut lo, mut hi) = (0, n);
        while lo < hi {
            let mid = (lo + hi) / 2;
            if at(mid) < ms {
                lo = mid + 1;
            } else {
                hi = mid;
            }
        }
        if lo == 0 {
            return at(0);
        }
        if lo == n {
            return at(n - 1);
        }
        if ms - at(lo - 1) <= at(lo) - ms {
            at(lo - 1)
        } else {
            at(lo)
        }
    }

    /// Index of the grid beat nearest `ms`, the earlier one on a tie
    /// (`beat-sync-math.ts` `_nearestBeatIndex`). Needs a non-empty grid.
    pub fn nearest_beat(&self, ms: f64) -> usize {
        let b = &self.beats;
        let later = b.partition_point(|x| x.time_ms < ms);
        if later == 0 {
            return 0;
        }
        if later == b.len() {
            return b.len() - 1;
        }
        if ms - b[later - 1].time_ms <= b[later].time_ms - ms {
            later - 1
        } else {
            later
        }
    }

    /// Index of the last downbeat at or before `ms` (`precedingDownbeatMs`).
    pub fn downbeat_at_or_before(&self, ms: f64) -> Option<usize> {
        let upto = self.beats.partition_point(|x| x.time_ms <= ms);
        let k = self.downbeats.partition_point(|&i| i < upto);
        k.checked_sub(1).map(|k| self.downbeats[k])
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
    pub cue: Option<f64>,
    /// The page's per-deck Quantize toggle and grid (`quantize_enabled`,
    /// `quantize_grid_beats`, on and 1 by default). The engine applies it
    /// where it resolves a position itself: the cue a pause or a paused CUE
    /// press stores, and a paused CUE jump. Seeks and loop bounds arrive
    /// already snapped by the page adapter.
    pub quantize: bool,
    pub quantize_grid: u8,
    pub tempo: f64,
    pub pitch_range: f64,
    /// Loop bounds in source frames.
    pub looping: Option<(f64, f64)>,
    /// Whole-grid-beat length of the engaged loop when `beat_loop` made it,
    /// None for a loop set by bounds. The page's `LoopState.beat_length`:
    /// its loop-size readout and a repeated beat loop's restart both key on it.
    pub loop_beats: Option<f64>,
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
    /// EQ gains ramp linearly, as the page's `applyEqRamp` does; the
    /// other parameters glide like its `setTargetAtTime`.
    eq_db: [Ramped; 3],
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
    /// Frames until filter coefficients are next due for a recompute.
    coeff_countdown: u32,
    /// Frames rendered since the deck last played a frame of its track.
    idle_run: u64,
    /// After this many idle frames the filter tails are over: state is zeroed
    /// and an idle strip at rest is skipped entirely.
    quiet_after: u64,
}

/// Coefficients glide with their parameters, recomputed every COEFF_INTERVAL
/// frames (and on the frame a glide settles). A fixed, free-running interval
/// keeps renders independent of the caller's block size.
const COEFF_INTERVAL: u32 = 16;

impl Strip {
    fn new(sr: f64) -> Strip {
        let s = |v: f64| Smoothed::new(v, sr, mixer::PARAM_SMOOTH_S);
        let fp = mixer::filter_params(0.5);
        let mut strip = Strip {
            sr,
            trim: s(mixer::trim_gain_from_knob(0.5)),
            eq_db: [Ramped::new(0.0, sr, mixer::PARAM_SMOOTH_S); 3],
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
        // The parameters behind the coefficients move every frame, like the
        // page's; only the coefficients are held between recomputes, so they
        // trail the ramp by under COEFF_INTERVAL frames and never run ahead
        // of it. The frame a glide settles recomputes at once, so the target
        // lands on time rather than at the next interval.
        let due = self.coeff_countdown == 0;
        if due {
            self.coeff_countdown = COEFF_INTERVAL;
        }
        self.coeff_countdown -= 1;
        if self.coeffs_gliding() {
            for s in self.eq_db.iter_mut() {
                s.tick();
            }
            self.lp_hz.tick();
            self.hp_hz.tick();
            if due || !self.coeffs_gliding() {
                self.recompute();
            }
        }

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
            cue: None,
            quantize: true,
            quantize_grid: 1,
            tempo: 1.0,
            pitch_range: 16.0,
            looping: None,
            loop_beats: None,
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
    /// A new track starts at unity tempo with no cue or loop, as the page's
    /// `_clearLoadedTrackState` leaves it (`st.pitch = 1`). The pitch range,
    /// Quantize and the channel strip carry over, as they do there.
    pub fn load(&mut self, track: Arc<Track>) -> Option<Arc<Track>> {
        self.pos = 0.0;
        self.cue = None;
        self.playing = false;
        self.tempo = 1.0;
        self.clear_loop();
        self.track.replace(track)
    }

    /// An emptied deck is the page's `_emptyDeckState`: unity tempo and
    /// Quantize back to on at grid 1. The pitch range and the channel strip
    /// live outside that state on the page and carry over.
    pub fn unload(&mut self) -> Option<Arc<Track>> {
        self.playing = false;
        self.pos = 0.0;
        self.cue = None;
        self.tempo = 1.0;
        self.quantize = true;
        self.quantize_grid = 1;
        self.clear_loop();
        self.track.take()
    }

    /// Pause stores the cue at the pause position (snapped when Quantize is
    /// on, as the page's pause does); play at the end restarts
    /// from 0 (`audio-engine.svelte.ts` CUE and transport semantics).
    pub fn play(&mut self, playing: bool) -> Result<(), EngineError> {
        let frames = self.track()?.frames as f64;
        if playing {
            if self.pos >= frames {
                self.pos = 0.0;
            }
        } else if self.playing {
            self.cue = Some(self.quantized(self.pos));
        }
        self.playing = playing;
        Ok(())
    }

    /// CUE while playing returns to the cue point (the start when none is
    /// set) and pauses, leaving an engaged loop engaged, as the page's press
    /// does: it is a pause at the cue, not a seek. While paused, the first
    /// press sets the cue where the playhead is without moving; later presses
    /// jump to it as a seek does (snapped, refused past the end of the track,
    /// leaving a loop it lands outside), as the page's `quantizedSeek` does
    /// (`audio-engine.svelte.ts` `pressCue`).
    pub fn cue(&mut self) -> Result<(), EngineError> {
        let t = self.track()?.clone();
        if self.playing {
            let to = self.cue.unwrap_or(0.0);
            // A pause near the end can snap the cue to a grid beat past the
            // audio; the page's playing press refuses that target
            // (`normalizeScheduledTransportEntrySec`) and keeps playing.
            if to > t.frames as f64 {
                return Err(EngineError::new(ErrorCode::Invalid, "the cue point is past the end of the track"));
            }
            self.pos = to;
            self.playing = false;
        } else if let Some(c) = self.cue {
            self.seek(t.frames_to_ms(c))?;
        } else {
            self.cue = Some(self.quantized(self.pos));
        }
        Ok(())
    }

    pub fn set_quantize(&mut self, on: bool) {
        self.quantize = on;
    }

    /// 1, 4 or 8 beats. The page's 'phase' grid is refused before it gets
    /// here, as the page refuses it.
    pub fn set_quantize_grid(&mut self, beats: u8) -> Result<(), EngineError> {
        if !matches!(beats, 1 | 4 | 8) {
            return Err(EngineError::new(ErrorCode::Invalid, "quantize grid must be 1, 4 or 8 beats"));
        }
        self.quantize_grid = beats;
        Ok(())
    }

    /// `frames` snapped to the quantize grid when Quantize is on and the
    /// track has a grid (the page's `effectiveQuantize`); unchanged otherwise.
    fn quantized(&self, frames: f64) -> f64 {
        match self.track.as_ref() {
            Some(t) if self.quantize && t.has_grid() => t.ms_to_frames(t.quantize_ms(t.frames_to_ms(frames), self.quantize_grid)),
            _ => frames,
        }
    }

    /// `ms` snapped the same way, for the targets the wire sends in ms.
    fn quantized_ms(&self, ms: f64) -> f64 {
        match self.track.as_ref() {
            Some(t) if self.quantize && t.has_grid() => t.quantize_ms(ms, self.quantize_grid),
            _ => ms,
        }
    }

    /// Seek, snapped to the quantize grid when Quantize is on and the track
    /// has one, as the page's `quantizedSeek` does
    /// (`quantizedSeekDecisionMs`). The asked position must lie within the
    /// track, and so must the snapped one: a last grid beat past the decoded
    /// end is refused, not clamped.
    pub fn seek(&mut self, ms: f64) -> Result<(), EngineError> {
        let t = self.track()?;
        let dur = t.duration_ms();
        if !ms.is_finite() || ms < 0.0 || ms > dur {
            return Err(EngineError::new(ErrorCode::Invalid, "seek position must be within the track"));
        }
        let to_ms = self.quantized_ms(ms);
        if to_ms > dur {
            return Err(EngineError::new(ErrorCode::Invalid, "the quantized seek target is past the end of the track"));
        }
        let to = self.track()?.ms_to_frames(to_ms);
        self.move_to(to);
        Ok(())
    }

    /// Put the playhead at `to`. Landing outside an engaged loop exits it, as
    /// rekordbox and the page do (`quantizedSeek`); inside, the loop stays.
    fn move_to(&mut self, to: f64) {
        if let Some((a, b)) = self.looping {
            if to < a || to >= b {
                self.clear_loop();
            }
        }
        self.pos = to;
    }

    /// A manual in/out loop, its ends snapped to the quantize grid when
    /// Quantize is on and the track has one, as the page's `setLoop` does
    /// (`quantizedLoopEndpointsMs`); exact without. Ends that snap to the
    /// same grid point are refused, not engaged as an empty loop.
    pub fn set_loop(&mut self, bounds_ms: Option<(f64, f64)>) -> Result<(), EngineError> {
        let Some((in_ms, out_ms)) = bounds_ms else {
            self.clear_loop();
            return Ok(());
        };
        self.track()?;
        if !(in_ms.is_finite() && out_ms.is_finite()) || in_ms < 0.0 || out_ms <= in_ms {
            return Err(EngineError::new(ErrorCode::Invalid, "loop needs 0 <= in_ms < out_ms"));
        }
        let (in_ms, out_ms) = (self.quantized_ms(in_ms), self.quantized_ms(out_ms));
        if out_ms <= in_ms {
            return Err(EngineError::new(
                ErrorCode::Invalid,
                "the quantized loop collapsed onto one grid point; choose ends on different grid points",
            ));
        }
        self.set_loop_exact(in_ms, out_ms)
    }

    /// Engage `[in_ms, out_ms)` as given, bounded to the track.
    fn set_loop_exact(&mut self, in_ms: f64, out_ms: f64) -> Result<(), EngineError> {
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
        self.loop_beats = None;
        Ok(())
    }

    fn clear_loop(&mut self) {
        self.looping = None;
        self.loop_beats = None;
    }

    /// Engage `(in_ms, out_ms)` as a `beats`-long beat loop. Reissuing the
    /// loop that is already engaged, same length and same bounds, is a
    /// RESTART, as the page's `engageBeatLoop` does: the playhead goes back
    /// to loop-in, playing or paused. Anything else engages the new loop and
    /// leaves the playhead where it is.
    ///
    /// A beat loop that would run past the end of the audio is refused, not
    /// clamped: `set_loop` would shorten it silently while the deck still
    /// reported the full beat length.
    fn engage_beat_loop(&mut self, beats: f64, in_ms: f64, out_ms: f64) -> Result<(), EngineError> {
        if out_ms > self.track()?.duration_ms() {
            return Err(EngineError::new(ErrorCode::Invalid, "the beat loop runs past the end of the track"));
        }
        let before = self.looping.zip(self.loop_beats);
        // Beat loop ends are already whole grid beats. The page sends them
        // through `setLoop`, whose re-snap is the identity on the 1-beat grid
        // but on a 4 or 8-beat grid collapses a short loop or stretches it
        // off its labeled length; this engine keeps the exact beats.
        self.set_loop_exact(in_ms, out_ms)?;
        self.loop_beats = Some(beats);
        if let (Some(((a, b), n)), Some(now)) = (before, self.looping) {
            if n == beats && (a, b) == now {
                self.pos = a;
            }
        }
        Ok(())
    }

    /// On a real grid the loop spans whole grid beats, as the page's
    /// `engageBeatLoop` does: it starts on the beat nearest `start_ms`, or,
    /// with no `start_ms`, on the preceding downbeat for a 4-beat loop and on
    /// the nearest beat otherwise. With no real grid it is refused, as the
    /// page's `requireBeatGrid` refuses it: a tag BPM is not a grid.
    pub fn beat_loop(&mut self, beats: f64, start_ms: Option<f64>) -> Result<(), EngineError> {
        // Whole beats on every track, as the page's command parser requires,
        // whether or not a grid is loaded.
        if !(beats.is_finite() && beats > 0.0 && beats <= MAX_BEATS && beats.fract() == 0.0) {
            return Err(EngineError::new(
                ErrorCode::Invalid,
                "beat loop length must be a whole number of beats, 1 to 65536",
            ));
        }
        let t = self.track()?.clone();
        if !t.has_grid() {
            return Err(requires_grid());
        }
        let here = start_ms.unwrap_or_else(|| t.frames_to_ms(self.pos));
        let start = if start_ms.is_none() && beats == 4.0 {
            t.downbeat_at_or_before(here)
                .ok_or(EngineError::new(ErrorCode::NoBeatgrid, "no downbeat at or before the playhead"))?
        } else {
            t.nearest_beat(here)
        };
        let end = start + beats as usize;
        if end >= t.beats.len() {
            return Err(EngineError::new(ErrorCode::Invalid, "the loop runs past the last beat of the grid"));
        }
        self.engage_beat_loop(beats, t.beats[start].time_ms, t.beats[end].time_ms)
    }

    /// Jump by `beats` along the grid. A jump inside an active loop moves the
    /// loop with it, as rekordbox does, so the playhead stays inside it.
    pub fn beat_jump(&mut self, beats: f64) -> Result<(), EngineError> {
        if !(beats.is_finite() && beats != 0.0 && beats.abs() <= MAX_BEATS && beats.fract() == 0.0) {
            return Err(EngineError::new(
                ErrorCode::Invalid,
                "beat jump must be a non-zero whole number of beats, at most 65536 either way",
            ));
        }
        let t = self.track()?.clone();
        // The page refuses a jump with no real grid (`requireBeatGrid`); a
        // tag BPM is not a grid, so neither does this engine count on one.
        if !t.has_grid() {
            return Err(requires_grid());
        }
        let now = t.frames_to_ms(self.pos);
        // Whole beats from the nearest real beat, clamped to the grid and
        // to its last beat inside the audio (`beatJumpTargetMs`).
        let last = t.beats.len() as i64 - 1;
        let i = (t.nearest_beat(now) as i64 + beats as i64).clamp(0, last) as usize;
        let dur = t.duration_ms();
        // A grid with no beat inside the decoded audio has nowhere to
        // land; the page refuses it too (`beatJumpTargetWithinDurationMs`).
        let i = (0..=i).rev().find(|&k| t.beats[k].time_ms <= dur).ok_or_else(|| {
            EngineError::new(ErrorCode::Invalid, "beat jump: no grid beat at or before the end of the decoded audio")
        })?;
        let target = t.beats[i].time_ms.clamp(0.0, dur);
        let Some((a, b)) = self.looping else {
            self.pos = t.ms_to_frames(target);
            return Ok(());
        };
        // An engaged loop moves by whole grid beats, keeping any manual
        // offset of its ends from their beats, and the playhead stays
        // inside it (`shiftLiveBeatLoopRangeMs`,
        // `targetWithinShiftedLiveLoopMs`). A shift that does not fit is
        // refused whole, as the page does, and nothing moves.
        let (lo, hi) = shift_live_loop(&t, t.frames_to_ms(a), t.frames_to_ms(b), beats as i64)?;
        let to = target_within_loop(&t, target, lo, hi)?;
        self.looping = Some((t.ms_to_frames(lo), t.ms_to_frames(hi)));
        self.pos = t.ms_to_frames(to);
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

    /// Start at crossfader gain `g` without a glide, for the engine's
    /// initial state.
    pub fn snap_xf_gain(&mut self, g: f64) {
        self.strip.xf.snap(g);
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
            let (l, r) = hermite(pcm, frames, self.pos, self.looping);
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
            // The last frame has played: the deck stops now, not when the
            // next frame is asked for, so the state published after this
            // buffer says so. A loop has wrapped by here and plays on.
            if self.pos >= end {
                self.pos = end;
                self.playing = false;
                break;
            }
        }
        if done > 0 {
            self.strip.idle_run = 0;
        }
        self.rendered_pos = self.pos;
        done
    }
}

/// Shift loop `[in_ms, out_ms)` by `delta` grid beats: each end moves to the
/// beat `delta` on from its nearest beat and keeps its offset from it.
fn shift_live_loop(t: &Track, in_ms: f64, out_ms: f64, delta: i64) -> Result<(f64, f64), EngineError> {
    let doesnt_fit = EngineError::new(ErrorCode::Invalid, "the loop does not fit on the grid after that jump");
    let (i, o) = (t.nearest_beat(in_ms), t.nearest_beat(out_ms));
    let len = o as i64 - i as i64;
    let next_in = i as i64 + delta;
    let next_out = next_in + len;
    if len <= 0 || next_in < 0 || next_out >= t.beats.len() as i64 {
        return Err(doesnt_fit);
    }
    let offset = |ms: f64, k: usize| ms - t.beats[k].time_ms;
    let lo = t.beats[next_in as usize].time_ms + offset(in_ms, i);
    let hi = t.beats[next_out as usize].time_ms + offset(out_ms, o);
    if lo < 0.0 || hi <= lo {
        return Err(doesnt_fit);
    }
    if hi > t.duration_ms() {
        return Err(EngineError::new(ErrorCode::Invalid, "the loop would run past the end of the track after that jump"));
    }
    Ok((lo, hi))
}

/// Pull a jump target inside loop `[lo, hi)`: before it, to its first grid
/// beat; at or past its end, to its last grid beat before the end.
fn target_within_loop(t: &Track, target: f64, lo: f64, hi: f64) -> Result<f64, EngineError> {
    let no_beat = EngineError::new(ErrorCode::Invalid, "the moved loop has no grid beat inside it");
    if target >= lo && target < hi {
        return Ok(target);
    }
    if target < lo {
        let k = t.beats.partition_point(|b| b.time_ms < lo);
        return t.beats.get(k).map(|b| b.time_ms).filter(|&ms| ms < hi).ok_or(no_beat);
    }
    let k = t.beats.partition_point(|b| b.time_ms < hi);
    k.checked_sub(1).map(|k| t.beats[k].time_ms).filter(|&ms| ms >= lo).ok_or(no_beat)
}

/// A beat loop or jump on a deck with no real grid, which the page refuses
/// too (`requireBeatGrid`).
fn requires_grid() -> EngineError {
    EngineError::new(ErrorCode::NoBeatgrid, "the deck has no real beat grid; a tag BPM is not one")
}

/// 4-point, 3rd-order Hermite interpolation of interleaved stereo at a
/// fractional frame position, with edge frames clamped.
///
/// Inside an engaged loop `[a, b)` the audio is periodic: a tap outside the
/// whole frames the loop holds (`ceil(a)` up to `ceil(b) - 1`) reads the
/// frame that many frames back in from the other end, as Web Audio's looping
/// source wraps its read index. Wrapping by the count of whole frames rather
/// than the fractional length keeps every tap inside `[a, b)`; otherwise the
/// frames around a wrap would interpolate with audio from outside the loop.
#[inline]
fn hermite(pcm: &[f32], frames: usize, pos: f64, looping: Option<(f64, f64)>) -> (f64, f64) {
    let i = pos.floor();
    let t = pos - i;
    let i = i as isize;
    let last = frames as isize - 1;
    let mut k = [i - 1, i, i + 1, i + 2];
    if let Some((a, b)) = looping {
        if b > a && pos >= a && pos < b {
            let (first, end) = (a.ceil() as isize, b.ceil() as isize);
            // A loop shorter than a frame may hold no whole frame: it then
            // reads the one it starts in.
            let (first, period) = if end > first { (first, end - first) } else { (a.floor() as isize, 1) };
            if k[0] < first || k[3] >= first + period {
                for k in k.iter_mut() {
                    *k = first + (*k - first).rem_euclid(period);
                }
            }
        }
    }
    for k in k.iter_mut() {
        *k = (*k).clamp(0, last) * 2;
    }
    let mut out = [0.0; 2];
    for (ch, o) in out.iter_mut().enumerate() {
        let at = |j: usize| pcm[k[j] as usize + ch] as f64;
        let (xm1, x0, x1, x2) = (at(0), at(1), at(2), at(3));
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
        assert_eq!(d.cue, Some(48000.0));
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
    fn reissuing_the_engaged_beat_loop_restarts_it_at_loop_in() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        d.beat_loop(4.0, Some(1000.0)).unwrap();
        assert_eq!(d.loop_beats, Some(4.0));
        d.seek(2200.0).unwrap();
        // Paused: the same loop again puts the playhead back at loop-in.
        d.beat_loop(4.0, Some(1000.0)).unwrap();
        assert_eq!((d.looping, d.pos), (Some((48000.0, 144000.0)), 48000.0));
        // Playing: the same.
        d.seek(2200.0).unwrap();
        d.play(true).unwrap();
        d.beat_loop(4.0, Some(1000.0)).unwrap();
        assert_eq!(d.pos, 48000.0);
        assert!(d.playing);
    }

    #[test]
    fn a_different_beat_loop_engages_without_moving_the_playhead() {
        // Control for the restart: only the SAME engaged loop restarts.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        d.set_quantize(false);
        d.beat_loop(4.0, Some(1000.0)).unwrap();
        d.seek(1200.0).unwrap();
        // Same start, other length: a new loop, the playhead stays.
        d.beat_loop(2.0, Some(1000.0)).unwrap();
        assert_eq!((d.looping, d.pos, d.loop_beats), (Some((48000.0, 96000.0)), 57600.0, Some(2.0)));
        // A bounds loop over the same range as a beat loop is not a beat
        // loop, so the beat loop over it engages fresh rather than restarts.
        d.set_loop(Some((1000.0, 2000.0))).unwrap();
        assert_eq!(d.loop_beats, None);
        d.beat_loop(2.0, Some(1000.0)).unwrap();
        assert_eq!((d.pos, d.loop_beats), (57600.0, Some(2.0)));
        // Leaving the loop forgets its length.
        d.seek(5000.0).unwrap();
        assert_eq!((d.looping, d.loop_beats), (None, None));
    }

    #[test]
    fn a_playing_cue_press_refuses_a_cue_past_the_end() {
        // Codex's case: a 6.9 s track whose grid runs on to 7.5 s. Pausing at
        // 6.8 s snaps the cue to the 7 s beat, past the audio; a later CUE
        // press while playing is refused and changes nothing.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 6.9, grid_120(4))));
        d.pos = 6.8 * 48000.0;
        d.play(true).unwrap();
        d.play(false).unwrap();
        assert_eq!(d.cue, Some(7.0 * 48000.0));
        d.play(true).unwrap();
        let e = d.cue().unwrap_err();
        assert!(e.message.contains("cue point is past the end"), "{}", e.message);
        assert_eq!((d.pos, d.playing), (6.8 * 48000.0, true));
        // Control: a cue inside the track, including exactly at its end, is
        // returned to and paused on.
        for (at, cue) in [(6.0, 6.0), (6.2, 6.0)] {
            d.pos = at * 48000.0;
            d.play(false).unwrap();
            assert_eq!(d.cue, Some(cue * 48000.0));
            d.play(true).unwrap();
            d.cue().unwrap();
            assert_eq!((d.pos, d.playing), (cue * 48000.0, false));
        }
        d.cue = Some(6.9 * 48000.0);
        d.play(true).unwrap();
        d.cue().unwrap();
        assert_eq!((d.pos, d.playing), (6.9 * 48000.0, false));
    }

    #[test]
    fn a_beat_loop_past_the_end_of_the_audio_is_refused_not_shortened() {
        // A 120 BPM grid (500 ms beats) running to 12 s over a 10 s track.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(6))));
        let e = d.beat_loop(4.0, Some(8500.0)).unwrap_err();
        assert!(e.message.contains("past the end"), "{}", e.message);
        assert_eq!((d.looping, d.loop_beats), (None, None));
        // Control: a loop ending exactly at the end still engages at full length.
        d.beat_loop(4.0, Some(8000.0)).unwrap();
        assert_eq!((d.looping, d.loop_beats), (Some((384000.0, 480000.0)), Some(4.0)));
        // On a grid whose beats run past the audio, the same rule.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 7.0, grid_120(4))));
        assert!(d.beat_loop(2.0, Some(6500.0)).is_err(), "ends on the 7.5 s beat");
        d.beat_loop(2.0, Some(6000.0)).unwrap();
        assert_eq!((d.looping, d.loop_beats), (Some((288000.0, 336000.0)), Some(2.0)));
    }

    #[test]
    fn beat_counts_are_whole_with_or_without_a_grid() {
        // Whole beats only, and a bad count is Invalid before any grid check:
        // on a tag BPM with no grid the count is still what is wrong.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(Track::new(48000, vec![0.0; 48000 * 2 * 10], vec![], Some(120.0))));
        for bad in [0.5, 1.5, 0.0, -1.0] {
            assert_eq!(d.beat_loop(bad, Some(1000.0)).unwrap_err().code, ErrorCode::Invalid, "loop {bad}");
        }
        for bad in [0.5, -2.5, 0.0] {
            assert_eq!(d.beat_jump(bad).unwrap_err().code, ErrorCode::Invalid, "jump {bad}");
        }
        // And on a real grid.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(5))));
        for bad in [0.5, 1.5, 0.0, -1.0] {
            assert_eq!(d.beat_loop(bad, Some(1000.0)).unwrap_err().code, ErrorCode::Invalid, "loop {bad}");
        }
        for bad in [0.5, -2.5, 0.0] {
            assert_eq!(d.beat_jump(bad).unwrap_err().code, ErrorCode::Invalid, "jump {bad}");
        }
        assert_eq!(d.looping, None);
        // Control: whole counts work on the grid, negative jumps too.
        d.beat_loop(2.0, Some(1000.0)).unwrap();
        assert_eq!(d.looping, Some((48000.0, 96000.0)));
        d.set_loop(None).unwrap();
        d.seek(4000.0).unwrap();
        d.beat_jump(-2.0).unwrap();
        assert_eq!(d.pos, 3000.0 * 48.0);
    }

    #[test]
    fn an_eq_change_lands_in_10_ms_like_the_pages_ramp() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 1.0, vec![])));
        d.play(true).unwrap();
        d.set_eq(0, 0.0);
        let low = |db: f64| Coeffs::lowshelf(48000.0, mixer::EQ_FREQ_LOW_HZ, db).b0;
        let ramp_at = |frame: f64| mixer::EQ_MIN_DB * frame / 480.0;
        // The first frame moves one frame's worth, not a whole coefficient
        // interval's: nothing runs ahead of the page's ramp.
        let mut buf = vec![0.0f32; 2];
        d.render_add(&mut buf, 48000.0);
        assert!((d.strip.eq_db[0].value - ramp_at(1.0)).abs() < 1e-12, "{}", d.strip.eq_db[0].value);
        assert_eq!(d.strip.eq[0].c.b0, low(ramp_at(1.0)));
        // Halfway through, exactly halfway to the -26 dB kill. The filter in
        // use was computed at the last interval (frame 225 of the ramp), so
        // it trails the ramp and never leads it.
        let mut buf = vec![0.0f32; 239 * 2];
        d.render_add(&mut buf, 48000.0);
        assert!((d.strip.eq_db[0].value - -13.0).abs() < 1e-12, "{}", d.strip.eq_db[0].value);
        assert_eq!(d.strip.eq[0].c.b0, low(ramp_at(225.0)));
        // The frame the ramp lands (480, not on an interval) puts the target
        // in the filter at once.
        let mut buf = vec![0.0f32; 240 * 2];
        d.render_add(&mut buf, 48000.0);
        assert_eq!(d.strip.eq_db[0].value, mixer::EQ_MIN_DB);
        assert_eq!(d.strip.eq[0].c.b0, low(mixer::EQ_MIN_DB));
    }

    #[test]
    fn a_filter_sweep_glides_frame_by_frame() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 1.0, vec![])));
        d.play(true).unwrap();
        let from = d.strip.lp_hz.value;
        d.set_filter(0.25);
        let to = d.strip.lp_hz.target;
        let mut buf = vec![0.0f32; 2];
        d.render_add(&mut buf, 48000.0);
        // One frame of the one-pole glide, as setTargetAtTime makes it.
        let k = 1.0 - (-1.0 / (mixer::PARAM_SMOOTH_S * 48000.0)).exp();
        let want = from + (to - from) * k;
        assert!((d.strip.lp_hz.value - want).abs() < 1e-6 * from, "{} vs {want}", d.strip.lp_hz.value);
    }

    #[test]
    fn a_new_track_starts_at_unity_tempo_like_the_pages() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 2.0, vec![])));
        d.set_pitch_range(8.0).unwrap();
        d.set_tempo(1.05).unwrap();
        d.set_quantize(false);
        d.set_quantize_grid(4).unwrap();
        d.set_trim(0.25);
        // Loading over it: unity tempo; range, Quantize and strip kept.
        d.load(Arc::new(silent(48000, 2.0, vec![])));
        assert_eq!(d.tempo, 1.0);
        assert_eq!((d.pitch_range, d.quantize, d.quantize_grid, d.trim), (8.0, false, 4, 0.25));
        // Unloading: the page's empty deck, Quantize back on at grid 1.
        d.set_tempo(0.95).unwrap();
        d.unload();
        assert_eq!((d.tempo, d.quantize, d.quantize_grid), (1.0, true, 1));
        assert_eq!((d.pitch_range, d.trim), (8.0, 0.25));
    }

    #[test]
    fn cue_placement_follows_the_pages_quantize() {
        // 120 BPM grid: beats every 500 ms, downbeats at 0, 2, 4 and 6 s.
        let t = Arc::new(silent(48000, 10.0, grid_120(4)));
        let paused_at = |ms: f64, setup: &dyn Fn(&mut Deck)| {
            let mut d = Deck::new(48000.0);
            d.load(t.clone());
            setup(&mut d);
            // Park the playhead off the grid, as pausing mid-beat does: a
            // seek with Quantize on would snap it.
            let q = d.quantize;
            d.quantize = false;
            d.seek(ms).unwrap();
            d.quantize = q;
            d
        };
        let cue_ms = |d: &Deck| t.frames_to_ms(d.cue.unwrap());
        let none = |_: &mut Deck| {};
        // Quantize is on at grid 1 by default, as on the page: the first
        // paused CUE stores the nearest beat and leaves the playhead alone.
        let mut d = paused_at(1300.0, &none);
        d.cue().unwrap();
        assert_eq!(cue_ms(&d), 1500.0);
        assert_eq!(t.frames_to_ms(d.pos), 1300.0);
        // A tie goes to the earlier beat.
        let mut d = paused_at(1250.0, &none);
        d.cue().unwrap();
        assert_eq!(cue_ms(&d), 1000.0);
        // Pausing stores the snapped cue too.
        let mut d = paused_at(2600.0, &none);
        d.play(true).unwrap();
        d.play(false).unwrap();
        assert_eq!(cue_ms(&d), 2500.0);
        // Grid 4 snaps to downbeats; grid 8 to every other one from the first.
        // Ties go to the earlier point here too.
        for (grid, at, want) in [(4, 3100.0, 4000.0), (4, 2100.0, 2000.0), (4, 3000.0, 2000.0), (8, 2100.0, 4000.0), (8, 1900.0, 0.0), (8, 2000.0, 0.0)] {
            let mut d = paused_at(at, &|d: &mut Deck| d.set_quantize_grid(grid).unwrap());
            d.cue().unwrap();
            assert_eq!(cue_ms(&d), want, "grid {grid} at {at}");
        }
        // Controls: Quantize off, or no grid to snap to, stores the playhead.
        let mut d = paused_at(1300.0, &|d: &mut Deck| d.set_quantize(false));
        d.cue().unwrap();
        assert_eq!(cue_ms(&d), 1300.0);
        let mut bpm_only = Deck::new(48000.0);
        bpm_only.load(Arc::new(Track::new(48000, vec![0.0; 480000 * 2], vec![], Some(120.0))));
        bpm_only.seek(1300.0).unwrap();
        bpm_only.cue().unwrap();
        assert_eq!(bpm_only.cue, Some(1300.0 * 48.0));
        // A cue set with Quantize off is snapped when a paused CUE jumps to
        // it with Quantize on, as the page's quantizedSeek does.
        d.set_quantize(true);
        d.seek(5000.0).unwrap();
        d.cue().unwrap();
        assert_eq!(t.frames_to_ms(d.pos), 1500.0);
        // Control: CUE while playing returns to the stored cue as it is,
        // unsnapped, as the page's playing press does.
        let mut d = paused_at(1300.0, &|d: &mut Deck| d.set_quantize(false));
        d.cue().unwrap();
        d.set_quantize(true);
        d.seek(5000.0).unwrap();
        d.play(true).unwrap();
        d.cue().unwrap();
        assert_eq!(t.frames_to_ms(d.pos), 1300.0);
        assert_eq!(d.set_quantize_grid(2).unwrap_err().code, ErrorCode::Invalid);
    }

    #[test]
    fn a_cue_press_pauses_without_seeking_and_jumps_like_a_seek() {
        // Codex's case: cue at 0, a loop engaged at 10 to 12 s, CUE while
        // playing. The page's press pauses at the cue and keeps the loop, so
        // the next PLAY runs back into it.
        let t = Arc::new(silent(48000, 20.0, grid_120(10)));
        let mut d = Deck::new(48000.0);
        d.load(t.clone());
        d.cue().unwrap();
        assert_eq!(d.cue, Some(0.0));
        d.set_loop(Some((10000.0, 12000.0))).unwrap();
        d.seek(10500.0).unwrap();
        d.play(true).unwrap();
        d.cue().unwrap();
        assert_eq!((d.playing, d.pos), (false, 0.0));
        assert_eq!(d.looping, Some((480000.0, 576000.0)), "the loop survives the press");
        // Control: a paused press jumps as a seek does, so landing outside the
        // loop leaves it, as the page's quantizedSeek does.
        d.seek(10500.0).unwrap();
        d.cue().unwrap();
        assert_eq!((d.pos, d.looping), (0.0, None));

        // A cue stored on a grid beat past the decoded end (the page stores
        // it too) cannot be jumped to: the paused press is refused and the
        // playhead stays, as the page's quantizedSeek refuses it.
        let mut beats = grid_120(2);
        beats.push(Beat { time_ms: 4000.0, downbeat: false });
        let short = Arc::new(silent(48000, 3.9, beats));
        let mut d = Deck::new(48000.0);
        d.load(short.clone());
        d.quantize = false;
        d.seek(3800.0).unwrap();
        d.quantize = true;
        d.play(true).unwrap();
        d.play(false).unwrap();
        assert_eq!(d.cue, Some(4000.0 * 48.0));
        let e = d.cue().unwrap_err();
        assert_eq!(e.code, ErrorCode::Invalid);
        assert_eq!(d.pos, 3800.0 * 48.0);
        // Control: a cue inside the track is jumped to.
        d.cue = Some(3500.0 * 48.0);
        d.cue().unwrap();
        assert_eq!(d.pos, 3500.0 * 48.0);
    }

    #[test]
    fn seek_and_loop_snap_to_the_quantize_grid_like_the_pages() {
        // 120 BPM grid: beats every 500 ms, downbeats at 0, 2, 4 and 6 s.
        let t = Arc::new(silent(48000, 10.0, grid_120(4)));
        let deck = |setup: &dyn Fn(&mut Deck)| {
            let mut d = Deck::new(48000.0);
            d.load(t.clone());
            setup(&mut d);
            d
        };
        let at = |d: &Deck| t.frames_to_ms(d.pos);
        let lp = |d: &Deck| d.looping.map(|(a, b)| (t.frames_to_ms(a), t.frames_to_ms(b)));
        let none = |_: &mut Deck| {};
        // Codex's case: a seek to 1250 ms lands on the 1000 ms beat, the
        // earlier one on the tie, as the page's quantizedSeek does.
        for (grid, to, want) in [(1, 1250.0, 1000.0), (1, 1300.0, 1500.0), (4, 2900.0, 2000.0), (4, 3100.0, 4000.0), (8, 2100.0, 4000.0)] {
            let mut d = deck(&|d: &mut Deck| d.set_quantize_grid(grid).unwrap());
            d.seek(to).unwrap();
            assert_eq!(at(&d), want, "grid {grid} seek {to}");
        }
        // Controls: Quantize off, or no grid, seeks exactly.
        let mut d = deck(&|d: &mut Deck| d.set_quantize(false));
        d.seek(1250.0).unwrap();
        assert_eq!(at(&d), 1250.0);
        let mut bpm_only = Deck::new(48000.0);
        bpm_only.load(Arc::new(Track::new(48000, vec![0.0; 480000 * 2], vec![], Some(120.0))));
        bpm_only.seek(1250.0).unwrap();
        assert_eq!(bpm_only.pos, 1250.0 * 48.0);
        // The loop exit is decided on the snapped target: 1900 ms snaps to
        // the loop's out point and leaves it; 1700 ms snaps inside and stays.
        let mut d = deck(&none);
        d.set_loop(Some((1000.0, 2000.0))).unwrap();
        d.seek(1700.0).unwrap();
        assert_eq!((lp(&d), at(&d)), (Some((1000.0, 2000.0)), 1500.0));
        d.seek(1900.0).unwrap();
        assert_eq!((lp(&d), at(&d)), (None, 2000.0));
        // A snapped target past the decoded end is refused and nothing moves,
        // while one that snaps inside is taken.
        let mut beats = grid_120(2);
        beats.push(Beat { time_ms: 4000.0, downbeat: false });
        let short = Arc::new(silent(48000, 3.9, beats));
        let mut d = Deck::new(48000.0);
        d.load(short.clone());
        d.seek(3700.0).unwrap();
        assert_eq!(d.pos, 3500.0 * 48.0);
        assert_eq!(d.seek(3800.0).unwrap_err().code, ErrorCode::Invalid);
        assert_eq!(d.pos, 3500.0 * 48.0);
        // A last beat exactly at the end is inside the track, as on the page.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 3.5, grid_120(2))));
        d.seek(3400.0).unwrap();
        assert_eq!(d.pos, 3500.0 * 48.0);
        // Manual loop ends snap each to its nearest grid point.
        let mut d = deck(&none);
        d.set_loop(Some((1100.0, 1900.0))).unwrap();
        assert_eq!(lp(&d), Some((1000.0, 2000.0)));
        let mut d = deck(&|d: &mut Deck| d.set_quantize_grid(4).unwrap());
        d.set_loop(Some((900.0, 3100.0))).unwrap();
        assert_eq!(lp(&d), Some((0.0, 4000.0)));
        // Ends that snap onto one grid point are refused; the loop in force
        // stays.
        let e = d.set_loop(Some((1100.0, 2900.0))).unwrap_err();
        assert!(e.message.contains("collapsed"), "{}", e.message);
        assert_eq!(lp(&d), Some((0.0, 4000.0)));
        // Controls: Quantize off keeps the ends exact, and beat loops keep
        // their exact beats on a coarse grid (see engage_beat_loop).
        let mut d = deck(&|d: &mut Deck| d.set_quantize(false));
        d.set_loop(Some((1100.0, 1900.0))).unwrap();
        assert_eq!(lp(&d), Some((1100.0, 1900.0)));
        let mut d = deck(&|d: &mut Deck| d.set_quantize_grid(4).unwrap());
        d.beat_loop(1.0, Some(500.0)).unwrap();
        assert_eq!((lp(&d), d.loop_beats), (Some((500.0, 1000.0)), Some(1.0)));
    }

    #[test]
    fn a_loop_interpolates_only_audio_inside_it() {
        // A 44.1 kHz track played at 48 kHz, so every read is fractional:
        // silence, then 1.0 over the whole frames a loop holds, then silence.
        // Looping it must hear 1.0 throughout, across every wrap. The loop's
        // ends are given in frames and converted to ms for set_loop.
        let sr = 44100;
        let track_for = |ones: std::ops::Range<usize>| {
            let mut pcm = vec![0.0f32; sr as usize * 2];
            pcm[ones.start * 2..ones.end * 2].fill(1.0);
            Arc::new(Track::new(sr, pcm, vec![], None))
        };
        let ms = |frames: f64| frames * 1000.0 / sr as f64;
        let render = |track: &Arc<Track>, bounds: Option<(f64, f64)>| {
            let mut d = Deck::new(48000.0);
            d.load(track.clone());
            d.seek(510.0).unwrap();
            if let Some((a, b)) = bounds {
                d.set_loop(Some((ms(a), ms(b)))).unwrap();
            }
            d.play(true).unwrap();
            // 300 ms: three wraps, or straight past the span's end.
            let mut buf = vec![0.0f32; 14400 * 2];
            d.render_add(&mut buf, 48000.0);
            buf
        };
        let spread = |b: &[f32]| {
            let (lo, hi) = b.iter().fold((f32::MAX, f32::MIN), |(lo, hi), &x| (lo.min(x), hi.max(x)));
            hi - lo
        };
        // A whole-frame loop, and one whose ends fall between frames with a
        // fractional length that rounds up past the frames it holds (4409.6
        // frames, holding 4409).
        let whole = track_for(22050..26460);
        let looped = render(&whole, Some((22050.0, 26460.0)));
        assert!(spread(&looped) < 1e-6, "whole-frame loop heard outside itself: spread {}", spread(&looped));
        let fractional = track_for(22051..26460);
        let looped = render(&fractional, Some((22050.2, 26459.8)));
        assert!(spread(&looped) < 1e-6, "fractional loop heard outside itself: spread {}", spread(&looped));
        // Control: the same span played through without a loop does reach
        // the silence after it, so the measure can see a leak.
        assert!(spread(&render(&whole, None)) > 0.5);
        // And a loop still ahead of the playhead leaves the audio before it
        // alone: the first 400 ms are the track's silence, not the loop's.
        let mut d = Deck::new(48000.0);
        d.load(whole.clone());
        d.set_loop(Some((500.0, 600.0))).unwrap();
        d.play(true).unwrap();
        let mut buf = vec![0.0f32; 19200 * 2];
        d.render_add(&mut buf, 48000.0);
        assert!(buf.iter().all(|&x| x == 0.0), "audio before the loop read from inside it");
    }

    #[test]
    fn huge_beat_counts_are_refused_not_overflowed() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        // Off beat 0, so an unbounded count would overflow the beat index
        // (a saturated cast plus 4) rather than merely run past the grid.
        d.seek(2000.0).unwrap();
        for beats in [1e300, MAX_BEATS + 1.0, 9.3e18] {
            assert_eq!(d.beat_loop(beats, None).unwrap_err().code, ErrorCode::Invalid, "{beats}");
            assert_eq!(d.beat_jump(beats).unwrap_err().code, ErrorCode::Invalid, "{beats}");
            assert_eq!(d.beat_jump(-beats).unwrap_err().code, ErrorCode::Invalid, "-{beats}");
        }
        // Control: the bound itself is accepted. The jump clamps to the last
        // grid beat (7.5 s); the loop is refused only for running past it.
        let err = d.beat_loop(MAX_BEATS, None).unwrap_err();
        assert!(err.message.contains("past the last beat"), "{}", err.message);
        d.beat_jump(MAX_BEATS).unwrap();
        assert_eq!(d.pos, 7500.0 * 48.0);
        d.beat_jump(-MAX_BEATS).unwrap();
        assert_eq!(d.pos, 0.0);
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
    fn a_grid_loop_moves_by_whole_grid_beats() {
        // Codex's case: 120 BPM grid, loop [1 s, 2 s], playhead 1.25 s, +2.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 20.0, grid_120(10))));
        // Off-grid ends and playheads need Quantize off to be placed.
        d.set_quantize(false);
        d.set_loop(Some((1000.0, 2000.0))).unwrap();
        d.seek(1250.0).unwrap();
        d.beat_jump(2.0).unwrap();
        assert_eq!(d.looping, Some((96000.0, 144000.0)), "[2 s, 3 s], on the grid");
        assert_eq!(d.pos, 96000.0);
        // A manual offset on either end is kept, not snapped away.
        d.set_loop(Some((4100.0, 5050.0))).unwrap();
        d.seek(4500.0).unwrap();
        d.beat_jump(-2.0).unwrap();
        assert_eq!(d.looping, Some((3100.0 * 48.0, 4050.0 * 48.0)));
        assert_eq!(d.pos, 3500.0 * 48.0);
        // Near the loop's end the nearest beat is its out point; the jump
        // target lands on the moved out point and is pulled back inside, to
        // the last beat before it.
        d.set_loop(Some((1000.0, 2000.0))).unwrap();
        d.seek(1900.0).unwrap();
        d.beat_jump(2.0).unwrap();
        assert_eq!((d.looping, d.pos), (Some((96000.0, 144000.0)), 2500.0 * 48.0));
        // At 44.1 kHz on an off-round grid (128 BPM from 37.3 ms), a beat loop
        // moved by a jump lands exactly on grid beats: converting its ends
        // to frames and back leaves no offset behind.
        let beats: Vec<Beat> =
            (0..64).map(|i| Beat { time_ms: 37.3 + i as f64 * 468.75, downbeat: i % 4 == 0 }).collect();
        let t = Arc::new(Track::new(44100, vec![0.0; 44100 * 2 * 40], beats, None));
        let mut d = Deck::new(44100.0);
        d.load(t.clone());
        d.beat_loop(4.0, Some(t.beats[9].time_ms)).unwrap();
        d.seek(t.beats[10].time_ms).unwrap();
        d.beat_jump(3.0).unwrap();
        assert_eq!(d.looping, Some((t.ms_to_frames(t.beats[12].time_ms), t.ms_to_frames(t.beats[16].time_ms))));
    }

    #[test]
    fn a_grid_loop_that_would_leave_the_grid_is_refused_whole() {
        // 20 s at 120 bpm: 40 beats, the last at 19.5 s.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 20.0, grid_120(10))));
        // Loop [1 s, 2 s]: back 4 beats would start it before beat 0.
        d.set_loop(Some((1000.0, 2000.0))).unwrap();
        d.seek(1500.0).unwrap();
        assert_eq!(d.beat_jump(-4.0).unwrap_err().code, ErrorCode::Invalid);
        assert_eq!((d.looping, d.pos), (Some((48000.0, 96000.0)), 72000.0), "nothing moved");
        // Loop [18 s, 19 s]: forward 4 beats would end it past the last beat.
        d.set_loop(Some((18000.0, 19000.0))).unwrap();
        d.seek(18500.0).unwrap();
        assert!(d.beat_jump(4.0).is_err());
        assert_eq!((d.looping, d.pos), (Some((864000.0, 912000.0)), 888000.0));
        // Control: a jump that fits moves the loop and the playhead together.
        d.set_loop(Some((4000.0, 6000.0))).unwrap();
        d.seek(5000.0).unwrap();
        d.beat_jump(2.0).unwrap();
        assert_eq!(d.looping, Some((240000.0, 336000.0)));
        assert_eq!(d.pos, 288000.0);
    }

    #[test]
    fn a_beat_loop_or_jump_needs_a_real_grid_not_a_tag_bpm() {
        // The page refuses both with no real grid (`requireBeatGrid`), so a
        // tag BPM must not stand in for one here: the same command would
        // move one engine and not the other.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(Track::new(48000, vec![0.0; 48000 * 2 * 20], vec![], Some(120.0))));
        d.set_quantize(false);
        d.set_loop(Some((1000.0, 2000.0))).unwrap();
        d.seek(1500.0).unwrap();
        assert_eq!(d.beat_jump(-4.0).unwrap_err().code, ErrorCode::NoBeatgrid);
        assert_eq!(d.beat_loop(4.0, Some(3000.0)).unwrap_err().code, ErrorCode::NoBeatgrid);
        assert_eq!(d.beat_loop(4.0, None).unwrap_err().code, ErrorCode::NoBeatgrid);
        // Nothing moved.
        assert_eq!((d.looping, d.pos, d.loop_beats), (Some((48000.0, 96000.0)), 72000.0, None));
        // Control: the same commands on a real grid over the same audio work.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 20.0, grid_120(10))));
        d.beat_loop(4.0, Some(3000.0)).unwrap();
        assert_eq!(d.looping, Some((144000.0, 240000.0)));
        d.beat_jump(4.0).unwrap();
        assert_eq!(d.looping, Some((240000.0, 336000.0)));
    }

    #[test]
    fn the_first_cue_press_while_paused_sets_the_cue_without_moving() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        d.seek(3000.0).unwrap();
        d.cue().unwrap();
        assert_eq!((d.cue, d.pos), (Some(144000.0), 144000.0), "first press sets, stays");
        d.seek(5000.0).unwrap();
        d.cue().unwrap();
        assert_eq!(d.pos, 144000.0, "a later press jumps to the cue");
        // Control: with no cue set, CUE while playing returns to the start.
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        d.seek(2000.0).unwrap();
        d.playing = true;
        d.cue().unwrap();
        assert_eq!((d.pos, d.playing, d.cue), (0.0, false, None));
    }

    #[test]
    fn a_seek_outside_the_loop_exits_it() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        d.set_loop(Some((1000.0, 3000.0))).unwrap();
        // Control: inside the loop, the loop stays.
        d.seek(2000.0).unwrap();
        assert_eq!(d.looping, Some((48000.0, 144000.0)));
        // At the out point (exclusive) and before the in point, it exits.
        d.seek(3000.0).unwrap();
        assert_eq!((d.looping, d.pos), (None, 144000.0));
        d.set_loop(Some((1000.0, 3000.0))).unwrap();
        d.seek(500.0).unwrap();
        assert_eq!(d.looping, None);
    }

    #[test]
    fn beat_loops_and_jumps_land_on_real_grid_beats() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, grid_120(4))));
        // Mid-beat at 2.8 s: a 4-beat loop starts on the preceding downbeat
        // (2.0 s), a 2-beat loop on the nearest beat (3.0 s).
        d.seek(2800.0).unwrap();
        d.beat_loop(4.0, None).unwrap();
        assert_eq!(d.looping, Some((96000.0, 192000.0)));
        d.set_loop(None).unwrap();
        d.beat_loop(2.0, None).unwrap();
        assert_eq!(d.looping, Some((144000.0, 192000.0)));
        d.set_loop(None).unwrap();
        // An explicit start snaps to its nearest beat too.
        d.beat_loop(1.0, Some(1240.0)).unwrap();
        assert_eq!(d.looping, Some((48000.0, 72000.0)));
        d.set_loop(None).unwrap();
        assert!(d.beat_loop(0.5, None).is_err(), "sub-beat loops are refused on a grid");
        // A +4 jump from mid-beat (2.8 s, nearest beat 3.0 s) lands on 5.0 s.
        d.seek(2800.0).unwrap();
        d.beat_jump(4.0).unwrap();
        assert_eq!(d.pos, 240000.0);
        // Past the last beat (7.5 s) it clamps to that beat, not beyond.
        d.beat_jump(16.0).unwrap();
        assert_eq!(d.pos, 360000.0);
        assert!(d.beat_jump(0.5).is_err());
        // Codex's case: a 500 ms track whose grid starts after its audio ends
        // has no beat to land on; the jump is refused and nothing moves,
        // where it used to land on the track end.
        let mut d = Deck::new(48000.0);
        let late = vec![Beat { time_ms: 1000.0, downbeat: true }, Beat { time_ms: 1500.0, downbeat: false }];
        d.load(Arc::new(silent(48000, 0.5, late)));
        let e = d.beat_jump(1.0).unwrap_err();
        assert!(e.message.contains("no grid beat"), "{}", e.message);
        assert_eq!(d.pos, 0.0);
        // Control: one beat inside the audio is enough to land on.
        let mut d = Deck::new(48000.0);
        let one_in = vec![Beat { time_ms: 400.0, downbeat: true }, Beat { time_ms: 1500.0, downbeat: false }];
        d.load(Arc::new(silent(48000, 0.5, one_in)));
        d.beat_jump(1.0).unwrap();
        assert_eq!(d.pos, 400.0 * 48.0);
    }

    #[test]
    fn a_deck_stops_on_the_buffer_that_plays_its_last_frame() {
        // Codex's case: a one-frame track in a one-frame buffer. The state
        // read after that buffer says stopped, at the end.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(Track::new(48000, vec![0.5, 0.5], vec![], None)));
        d.play(true).unwrap();
        let mut one = [0.0f32; 2];
        d.render_add(&mut one, 48000.0);
        assert_ne!(one, [0.0, 0.0], "the last frame must still play");
        assert_eq!((d.playing, d.pos), (false, 1.0));
        // Control: one frame short of the end it is still playing, and stops
        // on the buffer that plays the last one.
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(Track::new(48000, vec![0.5; 4], vec![], None)));
        d.play(true).unwrap();
        d.render_add(&mut one, 48000.0);
        assert_eq!((d.playing, d.pos), (true, 1.0));
        d.render_add(&mut one, 48000.0);
        assert_eq!((d.playing, d.pos), (false, 2.0));
    }

    #[test]
    fn a_loop_ending_at_the_track_end_wraps() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 4.0, grid_120(2))));
        // The grid ends at 3.5 s; Quantize would snap the out point to it.
        d.set_quantize(false);
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
    fn a_beat_jump_without_a_grid_is_refused_with_or_without_a_bpm() {
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(silent(48000, 10.0, vec![])));
        assert_eq!(d.beat_jump(1.0).unwrap_err().code, ErrorCode::NoBeatgrid);
        let mut d = Deck::new(48000.0);
        d.load(Arc::new(Track::new(48000, vec![0.0; 960000], vec![], Some(120.0))));
        assert_eq!(d.beat_jump(2.0).unwrap_err().code, ErrorCode::NoBeatgrid);
        assert_eq!(d.pos, 0.0);
    }
}
