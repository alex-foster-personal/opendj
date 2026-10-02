//! The engine actor: the single owner of playback state.
//!
//! Every sender (UI, MIDI, agent, CLI) reaches the engine through `apply`, one
//! command at a time, in mailbox order. `render` pulls audio. Both run on the
//! same thread (the audio thread in real time, the caller's thread on the fake
//! clock), so there is no second writer and no lock.
//!
//! Real-time rules for `apply` and `render`: no allocation, no lock, no IO.
//! Errors carry `&'static str` messages for that reason, and a replaced track
//! is returned to the caller instead of being dropped here.

use std::sync::Arc;

use crate::deck::{Beat, Deck, Track};
use crate::dsp::Smoothed;
use crate::mixer::{self, Assign};

pub const MAX_DECKS: usize = 4;
/// Largest block `render` processes in one pass; longer requests are chunked.
pub const MAX_BLOCK: usize = 1024;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ErrorCode {
    /// A value the command vocabulary does not allow.
    Invalid,
    /// Not an engine command at all (page-only, or unknown).
    Unsupported,
    /// An engine command this build does not implement yet.
    NotImplemented,
    NoTrack,
    NoBeatgrid,
    Decode,
    Io,
    /// A command that needs a different clock (advance on a wall clock).
    WrongClock,
}

impl ErrorCode {
    pub fn as_str(self) -> &'static str {
        match self {
            ErrorCode::Invalid => "invalid",
            ErrorCode::Unsupported => "unsupported",
            ErrorCode::NotImplemented => "not_implemented",
            ErrorCode::NoTrack => "no_track",
            ErrorCode::NoBeatgrid => "no_beatgrid",
            ErrorCode::Decode => "decode",
            ErrorCode::Io => "io",
            ErrorCode::WrongClock => "wrong_clock",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct EngineError {
    pub code: ErrorCode,
    pub message: &'static str,
}

impl EngineError {
    pub const fn new(code: ErrorCode, message: &'static str) -> EngineError {
        EngineError { code, message }
    }
}

/// Deck number as the page names it: 1..=4.
pub type DeckId = u8;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum EqBand {
    Low,
    Mid,
    High,
}

impl EqBand {
    pub fn parse(s: &str) -> Option<EqBand> {
        match s {
            "low" => Some(EqBand::Low),
            "mid" => Some(EqBand::Mid),
            "high" => Some(EqBand::High),
            _ => None,
        }
    }

    pub fn index(self) -> usize {
        match self {
            EqBand::Low => 0,
            EqBand::Mid => 1,
            EqBand::High => 2,
        }
    }
}

/// A command the engine applies. Field names and units follow the audio
/// subset of the page's `PerformanceCommand`; `Load` carries an already
/// decoded track because decoding never happens on the audio thread.
#[derive(Clone, Debug)]
pub enum EngineCmd {
    Load { deck: DeckId, track: Arc<Track> },
    /// The loaded audio with a new beatgrid, built off the audio thread by
    /// `Track::with_grid` from the deck's current track.
    Regrid { deck: DeckId, track: Arc<Track> },
    /// The whole decoded track for the head a progressive load put on the
    /// deck (`Deck::extend`), built off the audio thread with the deck's
    /// current grid. Refused if the deck holds anything else by then.
    Extend { deck: DeckId, track: Arc<Track> },
    Unload { deck: DeckId },
    Play { deck: DeckId, playing: bool },
    /// Play if paused, pause if playing, decided when the engine applies it,
    /// so a toggle queued behind another sender's play still flips the state
    /// the engine actually has (a controller's PLAY button, plan 20-03).
    PlayToggle { deck: DeckId },
    Cue { deck: DeckId },
    Quantize { deck: DeckId, enabled: bool },
    QuantizeGrid { deck: DeckId, beats: u8 },
    Seek { deck: DeckId, position_ms: f64 },
    Loop { deck: DeckId, bounds_ms: Option<(f64, f64)> },
    BeatLoop { deck: DeckId, beats: f64, start_ms: Option<f64> },
    BeatJump { deck: DeckId, beats: f64 },
    Tempo { deck: DeckId, ratio: f64 },
    /// A tempo fader position, 0..1 bottom to top, read against the deck's
    /// own pitch range when applied: 0 is -range, 0.5 is 1.0, 1 is +range
    /// (the page's `pitchRatioFromFader`, plan 20-03).
    TempoFader { deck: DeckId, value: f64 },
    PitchRange { deck: DeckId, range: f64 },
    MasterTempo { deck: DeckId, enabled: bool },
    KeyNudge { deck: DeckId, semitones: i32 },
    Trim { deck: DeckId, value: f64 },
    Eq { deck: DeckId, band: EqBand, value: f64 },
    Filter { deck: DeckId, value: f64 },
    Fader { deck: DeckId, value: f64 },
    Assign { deck: DeckId, assign: Assign },
    Crossfader { value: f64 },
    MasterVolume { value: f64 },
    MasterMute { muted: bool },
}

/// A tempo fader position (0..1) as a ratio within the deck's pitch range in
/// percent, as the page's `pitchRatioFromFader` computes it: both ends land
/// exactly on the range, never past it.
pub fn tempo_from_fader(value01: f64, range_pct: f64) -> f64 {
    1.0 + (value01 * 2.0 - 1.0) * (range_pct / 100.0)
}

/// What `apply` hands back: a track that left the engine and must be freed by
/// the caller, off the audio thread.
pub type Retired = Option<Arc<Track>>;

/// A refused command. A refused load hands its track back in `track`, so the
/// caller frees it off the audio thread just like a retired one.
#[derive(Debug)]
pub struct Rejected {
    pub error: EngineError,
    pub track: Retired,
}

impl From<EngineError> for Rejected {
    fn from(error: EngineError) -> Rejected {
        Rejected { error, track: None }
    }
}

impl std::ops::Deref for Rejected {
    type Target = EngineError;
    fn deref(&self) -> &EngineError {
        &self.error
    }
}

#[derive(Clone, Copy, Debug, Default, PartialEq)]
pub struct DeckSnapshot {
    pub loaded: bool,
    pub playing: bool,
    pub position_ms: f64,
    pub duration_ms: f64,
    /// How much of the track is decoded: `duration_ms` once all of it is,
    /// less while a progressive load is still decoding the rest.
    pub decoded_ms: f64,
    pub tempo: f64,
    pub master_tempo: bool,
    pub key_shift: i32,
    /// None until a cue point is set (first CUE while paused, or a pause).
    pub cue_ms: Option<f64>,
    pub loop_ms: Option<(f64, f64)>,
    /// Beat length of the engaged loop when it is a beat loop, None for a
    /// loop set by bounds (the page's `LoopState.beat_length`).
    pub loop_beats: Option<f64>,
    pub trim: f64,
    pub eq: [f64; 3],
    pub filter: f64,
    pub fader: f64,
    pub assign: Assign,
    pub pitch_range: f64,
    pub quantize: bool,
    pub quantize_grid: u8,
}

/// Fixed-size engine state for the state feed. Copy, so publishing it from the
/// audio thread is a plain memcpy into a ring slot.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Snapshot {
    pub frame: u64,
    pub sample_rate: u32,
    pub decks: [DeckSnapshot; MAX_DECKS],
    pub crossfader: f64,
    pub master_volume: f64,
    pub master_muted: bool,
}

pub struct Engine {
    sr: u32,
    decks: [Deck; MAX_DECKS],
    crossfader: f64,
    master_volume: f64,
    master_muted: bool,
    master_gain: Smoothed,
    /// Set outright, never smoothed: the page assigns its mute node's gain
    /// exactly 0 or 1 (`master-mute.svelte.ts`), so a mute is silent from its
    /// first frame.
    mute_gain: f64,
    frame: u64,
}

impl Engine {
    pub fn new(sample_rate: u32) -> Engine {
        let sr = sample_rate as f64;
        let mut e = Engine {
            sr: sample_rate,
            decks: std::array::from_fn(|i| Deck::in_slot(sr, i)),
            crossfader: 0.5,
            master_volume: 1.0,
            master_muted: false,
            master_gain: Smoothed::new(1.0, sr, mixer::PARAM_SMOOTH_S),
            mute_gain: 1.0,
            frame: 0,
        };
        // The page's default assign matrix (`_defaultChannel`): odd decks on
        // A, even decks on B. The strips start at the gain that gives, with
        // no glide in from THRU.
        for (i, d) in e.decks.iter_mut().enumerate() {
            d.assign = if i % 2 == 0 { Assign::A } else { Assign::B };
            d.snap_xf_gain(mixer::xf_gain(d.assign, e.crossfader));
        }
        e.apply_crossfader();
        e
    }

    pub fn sample_rate(&self) -> u32 {
        self.sr
    }

    pub fn frame(&self) -> u64 {
        self.frame
    }

    pub fn deck(&self, deck: DeckId) -> Option<&Deck> {
        let i = (deck as usize).checked_sub(1)?;
        self.decks.get(i)
    }

    /// The deck's current audio with a new grid, for `EngineCmd::Regrid`.
    /// Allocates: call off the audio thread.
    pub fn regridded(&self, deck: DeckId, beats: Vec<Beat>, bpm: Option<f64>) -> Result<Arc<Track>, EngineError> {
        let d = self.deck(deck).ok_or(EngineError::new(ErrorCode::Invalid, "no such deck"))?;
        let t = d.track.as_ref().ok_or(EngineError::new(ErrorCode::NoTrack, "no track loaded on this deck"))?;
        Ok(Arc::new(t.with_grid(beats, bpm)))
    }

    fn deck_mut(&mut self, deck: DeckId) -> Result<&mut Deck, EngineError> {
        let i = (deck as usize)
            .checked_sub(1)
            .ok_or(EngineError::new(ErrorCode::Invalid, "deck must be 1..4"))?;
        self.decks.get_mut(i).ok_or(EngineError::new(ErrorCode::Invalid, "deck must be 1..4"))
    }

    fn apply_crossfader(&mut self) {
        let x = self.crossfader;
        for d in self.decks.iter_mut() {
            let g = mixer::xf_gain(d.assign, x);
            d.set_xf_gain(g);
        }
    }

    /// Apply one command at the current frame.
    pub fn apply(&mut self, cmd: EngineCmd) -> Result<Retired, Rejected> {
        use EngineCmd::*;
        match cmd {
            // A refused load gives its track back rather than dropping it here.
            Load { deck, track } => {
                return match self.deck_mut(deck) {
                    Ok(d) => Ok(d.load(track)),
                    Err(error) => Err(Rejected { error, track: Some(track) }),
                }
            }
            Regrid { deck, track } => {
                return match self.deck_mut(deck) {
                    Ok(d) => d.regrid(track).map(Some).map_err(|(error, t)| Rejected { error, track: Some(t) }),
                    Err(error) => Err(Rejected { error, track: Some(track) }),
                }
            }
            Extend { deck, track } => {
                return match self.deck_mut(deck) {
                    Ok(d) => d.extend(track).map(Some).map_err(|(error, t)| Rejected { error, track: Some(t) }),
                    Err(error) => Err(Rejected { error, track: Some(track) }),
                }
            }
            Unload { deck } => return Ok(self.deck_mut(deck)?.unload()),
            Play { deck, playing } => self.deck_mut(deck)?.play(playing)?,
            PlayToggle { deck } => {
                let d = self.deck_mut(deck)?;
                let playing = !d.playing;
                d.play(playing)?
            }
            Cue { deck } => self.deck_mut(deck)?.cue()?,
            Quantize { deck, enabled } => self.deck_mut(deck)?.set_quantize(enabled),
            QuantizeGrid { deck, beats } => self.deck_mut(deck)?.set_quantize_grid(beats)?,
            Seek { deck, position_ms } => self.deck_mut(deck)?.seek(position_ms)?,
            Loop { deck, bounds_ms } => self.deck_mut(deck)?.set_loop(bounds_ms)?,
            BeatLoop { deck, beats, start_ms } => self.deck_mut(deck)?.beat_loop(beats, start_ms)?,
            BeatJump { deck, beats } => self.deck_mut(deck)?.beat_jump(beats)?,
            Tempo { deck, ratio } => self.deck_mut(deck)?.set_tempo(ratio)?,
            TempoFader { deck, value } => {
                let v = mixer::check_unit(value)?;
                let d = self.deck_mut(deck)?;
                d.set_tempo(tempo_from_fader(v, d.pitch_range))?
            }
            PitchRange { deck, range } => self.deck_mut(deck)?.set_pitch_range(range)?,
            MasterTempo { deck, enabled } => self.deck_mut(deck)?.set_master_tempo(enabled),
            KeyNudge { deck, semitones } => self.deck_mut(deck)?.nudge_key(semitones)?,
            Trim { deck, value } => {
                let v = mixer::check_unit(value)?;
                self.deck_mut(deck)?.set_trim(v)
            }
            Eq { deck, band, value } => {
                let v = mixer::check_unit(value)?;
                self.deck_mut(deck)?.set_eq(band.index(), v)
            }
            Filter { deck, value } => {
                let v = mixer::check_unit(value)?;
                self.deck_mut(deck)?.set_filter(v)
            }
            Fader { deck, value } => {
                let v = mixer::check_unit(value)?;
                self.deck_mut(deck)?.set_fader(v)
            }
            Assign { deck, assign } => {
                let x = self.crossfader;
                let d = self.deck_mut(deck)?;
                d.assign = assign;
                d.set_xf_gain(mixer::xf_gain(assign, x));
            }
            Crossfader { value } => {
                self.crossfader = mixer::check_unit(value)?;
                self.apply_crossfader();
            }
            MasterVolume { value } => {
                self.master_volume = mixer::check_unit(value)?;
                self.master_gain.set(self.master_volume);
            }
            MasterMute { muted } => {
                self.master_muted = muted;
                self.mute_gain = if muted { 0.0 } else { 1.0 };
            }
        }
        Ok(None)
    }

    /// Render `out.len() / 2` frames of interleaved stereo into `out`,
    /// overwriting it. Never allocates.
    pub fn render(&mut self, out: &mut [f32]) {
        debug_assert!(out.len().is_multiple_of(2));
        for chunk in out.chunks_mut(MAX_BLOCK * 2) {
            self.render_block(chunk);
        }
    }

    /// Like `render`, and also writes each deck's own contribution (after its
    /// strip, before the master gain) into `split[deck - 1]`, each the same
    /// length as `out`. The mix is bit-identical to `render`'s, because decks
    /// are summed in the same order from the same values.
    pub fn render_split(&mut self, out: &mut [f32], split: &mut [&mut [f32]; MAX_DECKS]) {
        debug_assert!(out.len().is_multiple_of(2));
        debug_assert!(split.iter().all(|s| s.len() == out.len()));
        let step = MAX_BLOCK * 2;
        let mut off = 0;
        while off < out.len() {
            let end = (off + step).min(out.len());
            let chunk = &mut out[off..end];
            chunk.fill(0.0);
            let sr = self.sr as f64;
            for (d, s) in self.decks.iter_mut().zip(split.iter_mut()) {
                let s = &mut s[off..end];
                s.fill(0.0);
                d.render_add(s, sr);
                for (o, x) in chunk.iter_mut().zip(s.iter()) {
                    *o += *x;
                }
            }
            self.finish_block(chunk);
            off = end;
        }
    }

    fn render_block(&mut self, out: &mut [f32]) {
        out.fill(0.0);
        let sr = self.sr as f64;
        for d in self.decks.iter_mut() {
            d.render_add(out, sr);
        }
        self.finish_block(out);
    }

    /// Master gain and the frame count, shared by both render paths.
    fn finish_block(&mut self, out: &mut [f32]) {
        for o in out.chunks_exact_mut(2) {
            let g = (self.master_gain.tick() * self.mute_gain) as f32;
            o[0] *= g;
            o[1] *= g;
        }
        self.frame += (out.len() / 2) as u64;
    }

    pub fn snapshot(&self) -> Snapshot {
        let mut decks = [DeckSnapshot::default(); MAX_DECKS];
        for (s, d) in decks.iter_mut().zip(self.decks.iter()) {
            let Some(t) = d.track.as_ref() else {
                *s = DeckSnapshot {
                    tempo: d.tempo,
                    master_tempo: d.master_tempo,
                    key_shift: d.key_shift,
                    assign: d.assign,
                    pitch_range: d.pitch_range,
                    quantize: d.quantize,
                    quantize_grid: d.quantize_grid,
                    trim: d.trim,
                    eq: d.eq,
                    filter: d.filter,
                    fader: d.fader,
                    ..DeckSnapshot::default()
                };
                continue;
            };
            *s = DeckSnapshot {
                loaded: true,
                playing: d.playing,
                position_ms: t.frames_to_ms(d.pos),
                duration_ms: t.duration_ms(),
                decoded_ms: if t.decoding { t.frames_to_ms(t.available() as f64) } else { t.duration_ms() },
                tempo: d.tempo,
                master_tempo: d.master_tempo,
                key_shift: d.key_shift,
                cue_ms: d.cue.map(|c| t.frames_to_ms(c)),
                assign: d.assign,
                pitch_range: d.pitch_range,
                quantize: d.quantize,
                quantize_grid: d.quantize_grid,
                loop_ms: d.looping.map(|(a, b)| (t.frames_to_ms(a), t.frames_to_ms(b))),
                loop_beats: d.looping.and(d.loop_beats),
                trim: d.trim,
                eq: d.eq,
                filter: d.filter,
                fader: d.fader,
            };
        }
        Snapshot {
            frame: self.frame,
            sample_rate: self.sr,
            decks,
            crossfader: self.crossfader,
            master_volume: self.master_volume,
            master_muted: self.master_muted,
        }
    }

    /// Current value of a rampable knob, for ramps that start "from here".
    pub fn knob(&self, target: KnobTarget) -> Option<f64> {
        Some(match target {
            KnobTarget::Crossfader => self.crossfader,
            KnobTarget::MasterVolume => self.master_volume,
            KnobTarget::Trim(d) => self.deck(d)?.trim,
            KnobTarget::Eq(d, b) => self.deck(d)?.eq[b.index()],
            KnobTarget::Filter(d) => self.deck(d)?.filter,
            KnobTarget::Fader(d) => self.deck(d)?.fader,
            KnobTarget::Tempo(d) => self.deck(d)?.tempo,
        })
    }
}

/// A continuous control a ramp can move.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum KnobTarget {
    Crossfader,
    MasterVolume,
    Trim(DeckId),
    Eq(DeckId, EqBand),
    Filter(DeckId),
    Fader(DeckId),
    Tempo(DeckId),
}

impl KnobTarget {
    pub fn command(self, value: f64) -> EngineCmd {
        match self {
            KnobTarget::Crossfader => EngineCmd::Crossfader { value },
            KnobTarget::MasterVolume => EngineCmd::MasterVolume { value },
            KnobTarget::Trim(deck) => EngineCmd::Trim { deck, value },
            KnobTarget::Eq(deck, band) => EngineCmd::Eq { deck, band, value },
            KnobTarget::Filter(deck) => EngineCmd::Filter { deck, value },
            KnobTarget::Fader(deck) => EngineCmd::Fader { deck, value },
            KnobTarget::Tempo(deck) => EngineCmd::Tempo { deck, ratio: value },
        }
    }

    /// The knob a command sets outright, if any: what a ramp on it must yield
    /// to. A load or unload sets its deck's tempo back to 1, so it counts as
    /// setting the tempo.
    pub fn set_by(cmd: &EngineCmd) -> Option<KnobTarget> {
        Some(match *cmd {
            EngineCmd::Crossfader { .. } => KnobTarget::Crossfader,
            EngineCmd::MasterVolume { .. } => KnobTarget::MasterVolume,
            EngineCmd::Trim { deck, .. } => KnobTarget::Trim(deck),
            EngineCmd::Eq { deck, band, .. } => KnobTarget::Eq(deck, band),
            EngineCmd::Filter { deck, .. } => KnobTarget::Filter(deck),
            EngineCmd::Fader { deck, .. } => KnobTarget::Fader(deck),
            EngineCmd::Tempo { deck, .. } | EngineCmd::Load { deck, .. } | EngineCmd::Unload { deck } => KnobTarget::Tempo(deck),
            _ => return None,
        })
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::deck::Beat;

    fn tone(sr: u32, hz: f64, secs: f64) -> Track {
        let frames = (sr as f64 * secs) as usize;
        let mut pcm = Vec::with_capacity(frames * 2);
        for i in 0..frames {
            let v = (2.0 * std::f64::consts::PI * hz * i as f64 / sr as f64).sin() as f32 * 0.5;
            pcm.push(v);
            pcm.push(v);
        }
        let beats = (0..(secs * 2.0) as usize).map(|i| Beat { time_ms: i as f64 * 500.0, downbeat: i % 4 == 0, bpm: None }).collect();
        Track::new(sr, pcm, beats, None)
    }

    fn rms_db(buf: &[f32]) -> f64 {
        let s: f64 = buf.iter().map(|&x| (x as f64) * (x as f64)).sum();
        10.0 * (s / buf.len() as f64).log10()
    }

    /// Frequency by counting rising zero crossings on the left channel.
    fn freq(buf: &[f32], sr: f64) -> f64 {
        let left: Vec<f32> = buf.chunks_exact(2).map(|c| c[0]).collect();
        let mut first = None;
        let mut last = 0usize;
        let mut n = 0usize;
        for i in 1..left.len() {
            if left[i - 1] < 0.0 && left[i] >= 0.0 {
                if first.is_none() {
                    first = Some(i);
                } else {
                    n += 1;
                }
                last = i;
            }
        }
        n as f64 * sr / (last - first.unwrap()) as f64
    }

    #[test]
    fn play_toggle_flips_the_state_the_engine_has_when_it_applies() {
        let mut e = Engine::new(48000);
        // No track: refused like play, not silently accepted.
        assert_eq!(e.apply(EngineCmd::PlayToggle { deck: 1 }).unwrap_err().code, ErrorCode::NoTrack);
        e.apply(EngineCmd::Load { deck: 1, track: Arc::new(tone(48000, 440.0, 2.0)) }).unwrap();
        e.apply(EngineCmd::PlayToggle { deck: 1 }).unwrap();
        assert!(e.snapshot().decks[0].playing);
        // Another sender started it first: the toggle reads that and pauses,
        // where a toggle decided by the sender's stale view would play again.
        e.apply(EngineCmd::Play { deck: 1, playing: false }).unwrap();
        e.apply(EngineCmd::Play { deck: 1, playing: true }).unwrap();
        e.apply(EngineCmd::PlayToggle { deck: 1 }).unwrap();
        assert!(!e.snapshot().decks[0].playing);
        // A pause by toggle sets the cue where it stopped, as play(false) does.
        assert!(e.snapshot().decks[0].cue_ms.is_some());
    }

    #[test]
    fn tempo_fader_reads_the_decks_own_pitch_range() {
        let mut e = Engine::new(48000);
        e.apply(EngineCmd::Load { deck: 1, track: Arc::new(tone(48000, 440.0, 2.0)) }).unwrap();
        let tempo = |e: &Engine| e.snapshot().decks[0].tempo;
        for (v, want) in [(0.0, 0.84), (0.5, 1.0), (1.0, 1.16)] {
            e.apply(EngineCmd::TempoFader { deck: 1, value: v }).unwrap();
            assert_eq!(tempo(&e), want, "fader {v} at 16%");
        }
        e.apply(EngineCmd::TempoFader { deck: 1, value: 0.5 }).unwrap();
        e.apply(EngineCmd::PitchRange { deck: 1, range: 8.0 }).unwrap();
        e.apply(EngineCmd::TempoFader { deck: 1, value: 1.0 }).unwrap();
        assert_eq!(tempo(&e), 1.08, "the top of an 8% fader is +8%, not +16%");
        // Out of 0..1 is refused as a fader value, naming that, rather than
        // as the ratio it would have made; and it changes nothing.
        for v in [1.01, -0.01, f64::NAN] {
            let err = e.apply(EngineCmd::TempoFader { deck: 1, value: v }).unwrap_err();
            assert_eq!(err.code, ErrorCode::Invalid);
            assert!(err.message.contains("within 0..1"), "{v}: {}", err.message);
        }
        assert_eq!(tempo(&e), 1.08);
    }

    fn playing_tone(hz: f64, src_sr: u32) -> Engine {
        let mut e = Engine::new(48000);
        e.apply(EngineCmd::Load { deck: 1, track: Arc::new(tone(src_sr, hz, 10.0)) }).unwrap();
        e.apply(EngineCmd::Play { deck: 1, playing: true }).unwrap();
        e
    }

    #[test]
    fn varispeed_tempo_changes_pitch_and_rate_conversion_is_exact() {
        // Control: tempo 1.0 at a 44.1 kHz source rendered at 48 kHz keeps 1 kHz.
        let mut e = playing_tone(1000.0, 44100);
        let mut buf = vec![0.0f32; 48000 * 2];
        e.render(&mut buf);
        let f = freq(&buf, 48000.0);
        assert!((f - 1000.0).abs() < 0.5, "tempo 1.0 measured {f}");

        let mut e = playing_tone(1000.0, 44100);
        e.apply(EngineCmd::Tempo { deck: 1, ratio: 1.08 }).unwrap();
        e.render(&mut buf);
        let f = freq(&buf, 48000.0);
        assert!((f - 1080.0).abs() < 0.5, "tempo 1.08 measured {f}");
        let pos = e.snapshot().decks[0].position_ms;
        assert!((pos - 1080.0).abs() < 1e-6, "position {pos}");
    }

    /// Frequency of the left channel after the stretcher's start-up.
    fn settled_freq(buf: &[f32]) -> f64 {
        freq(&buf[9600 * 2..], 48000.0)
    }

    #[test]
    fn master_tempo_keeps_pitch_and_moves_the_playhead() {
        for (mt, want_hz) in [(true, 1000.0), (false, 1080.0)] {
            let mut e = playing_tone(1000.0, 48000);
            e.apply(EngineCmd::MasterTempo { deck: 1, enabled: mt }).unwrap();
            e.apply(EngineCmd::Tempo { deck: 1, ratio: 1.08 }).unwrap();
            let mut buf = vec![0.0f32; 48000 * 2];
            e.render(&mut buf);
            let f = settled_freq(&buf);
            assert!((f - want_hz).abs() < 2.0, "master_tempo {mt}: {f} Hz, want {want_hz}");
            // Either way the track moves at the tempo.
            let pos = e.snapshot().decks[0].position_ms;
            assert!((pos - 1080.0).abs() < 1e-6, "position {pos}");
        }
    }

    #[test]
    fn key_shift_moves_pitch_by_semitones_with_or_without_master_tempo() {
        for mt in [true, false] {
            let mut e = playing_tone(1000.0, 48000);
            e.apply(EngineCmd::MasterTempo { deck: 1, enabled: mt }).unwrap();
            e.apply(EngineCmd::KeyNudge { deck: 1, semitones: 1 }).unwrap();
            e.apply(EngineCmd::KeyNudge { deck: 1, semitones: 1 }).unwrap();
            let mut buf = vec![0.0f32; 48000 * 2];
            e.render(&mut buf);
            let want = 1000.0 * 2f64.powf(2.0 / 12.0);
            let f = settled_freq(&buf);
            assert!((f - want).abs() < 2.0, "master_tempo {mt}: {f} Hz, want {want}");
            assert_eq!(e.snapshot().decks[0].key_shift, 2);
        }
        // The page's range is -12..12, and a load resets the shift.
        let mut e = playing_tone(1000.0, 48000);
        for _ in 0..12 {
            e.apply(EngineCmd::KeyNudge { deck: 1, semitones: -1 }).unwrap();
        }
        assert_eq!(e.apply(EngineCmd::KeyNudge { deck: 1, semitones: -1 }).unwrap_err().code, ErrorCode::Invalid);
        e.apply(EngineCmd::Load { deck: 1, track: Arc::new(tone(48000, 500.0, 1.0)) }).unwrap();
        assert_eq!(e.snapshot().decks[0].key_shift, 0);
    }

    /// Frame of the first sample above half of the peak, left channel.
    fn onset(buf: &[f32]) -> usize {
        let peak = buf.iter().step_by(2).fold(0.0f32, |m, x| m.max(x.abs()));
        buf.iter().step_by(2).position(|x| x.abs() > peak * 0.5).unwrap()
    }

    fn clicks(sr: u32, secs: f64, every_s: f64) -> Track {
        let frames = (sr as f64 * secs) as usize;
        let mut pcm = vec![0.0f32; frames * 2];
        let mut t = every_s;
        while ((t * sr as f64) as usize) < frames - 64 {
            let i = (t * sr as f64) as usize;
            for k in 0..32 {
                let v = 0.8 * (1.0 - k as f32 / 32.0) * if k % 2 == 0 { 1.0 } else { -1.0 };
                pcm[(i + k) * 2] = v;
                pcm[(i + k) * 2 + 1] = v;
            }
            t += every_s;
        }
        Track::new(sr, pcm, vec![], None)
    }

    #[test]
    fn stretched_output_is_time_aligned_with_the_playhead() {
        // The page's worklet compensates the stretcher's latency, landing a
        // click 0 to 2 ms from where varispeed puts it (null test 20-05).
        // Same rule here, so position, loops and beat maths need no offset.
        let render = |mt: bool| {
            let mut e = Engine::new(48000);
            e.apply(EngineCmd::Load { deck: 1, track: Arc::new(clicks(48000, 3.0, 1.0)) }).unwrap();
            e.apply(EngineCmd::MasterTempo { deck: 1, enabled: mt }).unwrap();
            e.apply(EngineCmd::Seek { deck: 1, position_ms: 500.0 }).unwrap();
            e.apply(EngineCmd::Play { deck: 1, playing: true }).unwrap();
            let mut buf = vec![0.0f32; 48000 * 2];
            e.render(&mut buf);
            onset(&buf)
        };
        let (plain, stretched) = (render(false), render(true));
        assert_eq!(plain, 24000);
        let off_ms = (stretched as f64 - plain as f64) / 48.0;
        assert!(off_ms.abs() <= 2.0, "stretched click {off_ms:.2} ms from the varispeed one");
    }

    #[test]
    fn stretched_renders_do_not_depend_on_block_size() {
        // The stretcher runs in fixed 128-frame quanta whatever the caller's
        // block, so blocks of 480 and 997 give the same bytes (D6), with a
        // key nudge landing mid-render on the same frame.
        let render = |block: usize| {
            let mut e = playing_tone(440.0, 48000);
            e.apply(EngineCmd::MasterTempo { deck: 1, enabled: true }).unwrap();
            e.apply(EngineCmd::Tempo { deck: 1, ratio: 0.94 }).unwrap();
            let mut out = vec![0.0f32; 48000 * 2];
            let (head, tail) = out.split_at_mut(9600 * 2);
            for c in head.chunks_mut(block * 2) {
                e.render(c);
            }
            e.apply(EngineCmd::KeyNudge { deck: 1, semitones: 1 }).unwrap();
            for c in tail.chunks_mut(block * 2) {
                e.render(c);
            }
            out
        };
        let (a, b) = (render(480), render(997));
        let same = a.iter().zip(&b).all(|(x, y)| x.to_bits() == y.to_bits());
        assert!(same, "block size changed a stretched render");
        // Control: the render is not silence, so equality means something.
        assert!(rms_db(&a) > -20.0);
    }

    #[test]
    fn switching_master_tempo_mid_play_does_not_drop_out() {
        let mut e = playing_tone(440.0, 48000);
        e.apply(EngineCmd::Tempo { deck: 1, ratio: 1.04 }).unwrap();
        let mut buf = vec![0.0f32; 24000 * 2];
        e.render(&mut buf);
        let before = rms_db(&buf[12000 * 2..]);
        e.apply(EngineCmd::MasterTempo { deck: 1, enabled: true }).unwrap();
        let mut sw = vec![0.0f32; 480 * 2];
        e.render(&mut sw);
        // 10 ms windows across the switch stay within 1.5 dB of steady play;
        // an unprimed stretcher fades in over about 40 ms instead.
        let during = rms_db(&sw);
        assert!((during - before).abs() < 1.5, "switch window {during:.2} dB vs {before:.2} dB");
        e.apply(EngineCmd::MasterTempo { deck: 1, enabled: false }).unwrap();
        e.render(&mut sw);
        let back = rms_db(&sw);
        assert!((back - before).abs() < 1.5, "switch back {back:.2} dB vs {before:.2} dB");
    }

    #[test]
    fn eq_low_kill_cuts_bass_and_flat_leaves_it_alone() {
        let mut flat = playing_tone(100.0, 48000);
        let mut buf = vec![0.0f32; 48000 * 2];
        flat.render(&mut buf);
        let reference = rms_db(&buf[48000..]);
        // Flat EQ, unity trim and fader: a 0.5-amplitude sine is -9.03 dBFS,
        // less the equal-power crossfader's 3.01 dB at center (deck 1 is on A).
        assert!((reference - (-9.0309 - 3.0103)).abs() < 0.1, "flat measured {reference}");

        let mut cut = playing_tone(100.0, 48000);
        cut.apply(EngineCmd::Eq { deck: 1, band: EqBand::Low, value: 0.0 }).unwrap();
        cut.render(&mut buf);
        let killed = rms_db(&buf[48000..]);
        assert!(reference - killed > 20.0, "low kill only cut {} dB", reference - killed);
    }

    #[test]
    fn crossfader_and_mute_reach_the_output() {
        let mut e = playing_tone(1000.0, 48000);
        e.apply(EngineCmd::Assign { deck: 1, assign: Assign::A }).unwrap();
        e.apply(EngineCmd::Crossfader { value: 1.0 }).unwrap();
        let mut buf = vec![0.0f32; 48000 * 2];
        e.render(&mut buf);
        assert!(rms_db(&buf[48000..]) < -120.0, "deck on A still audible at crossfader B");
        e.apply(EngineCmd::Crossfader { value: 0.0 }).unwrap();
        e.render(&mut buf);
        assert!(rms_db(&buf[48000..]) > -10.0);
        e.apply(EngineCmd::MasterMute { muted: true }).unwrap();
        e.render(&mut buf);
        assert!(rms_db(&buf[48000..]) < -120.0, "master mute leaked");
    }

    #[test]
    fn master_mute_is_a_hard_switch_like_the_pages() {
        let mut e = playing_tone(1000.0, 48000);
        let mut buf = vec![0.0f32; 4800 * 2];
        e.render(&mut buf);
        // Muted mid-play: the very first frame is silent, as the page's
        // gain.value = 0 is, rather than gliding down over the next 200 ms.
        e.apply(EngineCmd::MasterMute { muted: true }).unwrap();
        let mut one = vec![0.0f32; 2];
        e.render(&mut one);
        assert_eq!(one, [0.0, 0.0]);
        // Unmuted: full level from the first frame back, no glide in.
        e.render(&mut buf);
        e.apply(EngineCmd::MasterMute { muted: false }).unwrap();
        let mut with = vec![0.0f32; 480 * 2];
        e.render(&mut with);
        let mut reference = playing_tone(1000.0, 48000);
        let mut skip = vec![0.0f32; (4800 + 1 + 4800) * 2];
        reference.render(&mut skip);
        let mut without = vec![0.0f32; 480 * 2];
        reference.render(&mut without);
        assert_eq!(with, without, "unmute glided in");
        // Control: master volume still glides like the page's setTargetAtTime,
        // so snapping everything is not what passes this test.
        e.apply(EngineCmd::MasterVolume { value: 0.0 }).unwrap();
        let mut tail = vec![0.0f32; 48 * 2];
        e.render(&mut tail);
        assert!(tail.iter().any(|&x| x != 0.0), "master volume snapped");
    }

    #[test]
    fn a_deck_starts_from_where_its_knobs_are_not_where_they_were() {
        // Crossfade a stopped deck out, then start it. Web Audio keeps an idle
        // channel's parameters moving, so its first frames are already
        // faded out; a strip that only moved while playing would glide down
        // from full volume and blip the deck in.
        let peak = |b: &[f32]| b.iter().fold(0.0f32, |m, &x| m.max(x.abs()));
        let mut e = Engine::new(48000);
        e.apply(EngineCmd::Load { deck: 2, track: Arc::new(tone(48000, 1000.0, 3.0)) }).unwrap();
        e.apply(EngineCmd::Assign { deck: 2, assign: Assign::B }).unwrap();
        e.apply(EngineCmd::Crossfader { value: 0.0 }).unwrap();
        let mut buf = vec![0.0f32; 48000 * 2];
        e.render(&mut buf);
        e.apply(EngineCmd::Play { deck: 2, playing: true }).unwrap();
        let mut first = vec![0.0f32; 480 * 2];
        e.render(&mut first);
        assert!(peak(&first) < 1e-6, "deck blipped in at {}", peak(&first));
        // Control, the other direction: an untouched deck is at its full
        // level (0.5 through the centered crossfader) from its first frames,
        // so idling does not fade decks down.
        let mut u = playing_tone(1000.0, 48000);
        u.render(&mut first);
        let full = 0.5 * mixer::xf_gain(Assign::A, 0.5) as f32;
        assert!(peak(&first) > full * 0.99, "untouched deck started at {}, full is {full}", peak(&first));
        // And a stopped deck goes exactly silent once its tails are over.
        u.apply(EngineCmd::Play { deck: 1, playing: false }).unwrap();
        u.render(&mut buf);
        u.render(&mut buf);
        assert_eq!(peak(&buf), 0.0);
    }

    #[test]
    fn out_of_range_and_state_errors_are_refused() {
        let mut e = Engine::new(48000);
        assert_eq!(e.apply(EngineCmd::Eq { deck: 1, band: EqBand::Low, value: 1.2 }).unwrap_err().code, ErrorCode::Invalid);
        assert_eq!(e.apply(EngineCmd::Play { deck: 1, playing: true }).unwrap_err().code, ErrorCode::NoTrack);
        assert_eq!(e.apply(EngineCmd::Fader { deck: 5, value: 0.5 }).unwrap_err().code, ErrorCode::Invalid);
        assert_eq!(e.apply(EngineCmd::Fader { deck: 0, value: 0.5 }).unwrap_err().code, ErrorCode::Invalid);
        // A refused load hands its track back instead of dropping it here.
        let t = Arc::new(Track::new(48000, vec![0.0f32; 96], vec![], None));
        let r = e.apply(EngineCmd::Load { deck: 5, track: t.clone() }).unwrap_err();
        assert_eq!(r.code, ErrorCode::Invalid);
        assert!(r.track.as_ref().is_some_and(|x| Arc::ptr_eq(x, &t)));
        // Control: a good load keeps it on the deck.
        assert!(e.apply(EngineCmd::Load { deck: 1, track: t.clone() }).unwrap().is_none());
        // Persistent deck controls show in the state feed.
        e.apply(EngineCmd::Assign { deck: 2, assign: Assign::B }).unwrap();
        e.apply(EngineCmd::PitchRange { deck: 2, range: 8.0 }).unwrap();
        let s = e.snapshot();
        assert_eq!((s.decks[1].assign, s.decks[1].pitch_range), (Assign::B, 8.0));
        assert_eq!((s.decks[0].assign, s.decks[0].cue_ms), (Assign::A, None));
    }

    #[test]
    fn the_snapshot_says_whether_the_loop_is_a_beat_loop() {
        let mut e = Engine::new(48000);
        let grid = (0..20).map(|i| Beat { time_ms: i as f64 * 500.0, downbeat: i % 4 == 0, bpm: None }).collect();
        let t = Arc::new(Track::new(48000, vec![0.0f32; 48000 * 2 * 10], grid, Some(120.0)));
        e.apply(EngineCmd::Load { deck: 1, track: t }).unwrap();
        e.apply(EngineCmd::BeatLoop { deck: 1, beats: 4.0, start_ms: Some(1000.0) }).unwrap();
        let d = e.snapshot().decks[0];
        assert_eq!((d.loop_ms, d.loop_beats), (Some((1000.0, 3000.0)), Some(4.0)));
        e.apply(EngineCmd::Loop { deck: 1, bounds_ms: Some((1000.0, 3000.0)) }).unwrap();
        assert_eq!(e.snapshot().decks[0].loop_beats, None);
    }

    #[test]
    fn decks_start_on_the_pages_crossfader_sides() {
        let mut e = Engine::new(48000);
        let sides: Vec<_> = e.snapshot().decks.iter().map(|d| d.assign).collect();
        assert_eq!(sides, vec![Assign::A, Assign::B, Assign::A, Assign::B]);
        // So the crossfader works with no assign sent: hard to A silences
        // deck 2 and leaves deck 1 at full level.
        for deck in [1, 2] {
            e.apply(EngineCmd::Load { deck, track: Arc::new(tone(48000, 440.0, 1.0)) }).unwrap();
            e.apply(EngineCmd::Play { deck, playing: true }).unwrap();
        }
        e.apply(EngineCmd::Crossfader { value: 0.0 }).unwrap();
        let mut buf = vec![0.0f32; 4800 * 2];
        e.render(&mut buf);
        assert!((e.decks[0].level_gain() - 1.0).abs() < 1e-3, "{}", e.decks[0].level_gain());
        assert!(e.decks[1].level_gain() < 1e-3, "{}", e.decks[1].level_gain());
        // At rest in the middle, a fresh engine starts at the equal-power
        // gain rather than gliding down to it from THRU's 1.0.
        let e = Engine::new(48000);
        let mid = mixer::xf_gain(Assign::A, 0.5);
        assert!(mid < 0.99, "{mid}");
        assert_eq!(e.decks[0].level_gain(), mid);
    }

    #[test]
    fn replaced_track_is_handed_back_not_dropped() {
        let mut e = Engine::new(48000);
        let a = Arc::new(tone(48000, 440.0, 1.0));
        assert!(e.apply(EngineCmd::Load { deck: 2, track: a.clone() }).unwrap().is_none());
        let retired = e.apply(EngineCmd::Load { deck: 2, track: Arc::new(tone(48000, 440.0, 1.0)) }).unwrap();
        assert!(Arc::ptr_eq(retired.as_ref().unwrap(), &a));
    }

    fn grid(every_ms: f64, secs: f64) -> Vec<Beat> {
        (0..(secs * 1000.0 / every_ms) as usize).map(|i| Beat { time_ms: i as f64 * every_ms, downbeat: i % 4 == 0, bpm: None }).collect()
    }

    #[test]
    fn set_beatgrid_swaps_the_grid_and_leaves_the_audio_and_transport_alone() {
        let run = |regrid: bool| {
            let mut e = Engine::new(48000);
            let t = Arc::new(tone(48000, 440.0, 4.0));
            e.apply(EngineCmd::Load { deck: 1, track: t.clone() }).unwrap();
            e.apply(EngineCmd::Play { deck: 1, playing: true }).unwrap();
            let mut buf = vec![0.0f32; 14400 * 2];
            e.render(&mut buf);
            let before = e.snapshot().decks[0];
            if regrid {
                let g = e.regridded(1, grid(400.0, 4.0), Some(150.0)).unwrap();
                assert!(Arc::ptr_eq(&g.pcm, &t.pcm), "the new grid must share the decoded samples");
                let retired = e.apply(EngineCmd::Regrid { deck: 1, track: g }).unwrap();
                assert!(Arc::ptr_eq(retired.as_ref().unwrap(), &t), "the old track comes back to be freed off-thread");
            }
            let after = e.snapshot().decks[0];
            assert_eq!(before.position_ms, after.position_ms);
            assert!(after.playing);
            let mut out = vec![0.0f32; 4800 * 2];
            e.render(&mut out);
            // 300 ms in: nearest beat is 400 ms on the new grid, 500 ms on the old.
            e.apply(EngineCmd::Play { deck: 1, playing: false }).unwrap();
            e.apply(EngineCmd::Seek { deck: 1, position_ms: 300.0 }).unwrap();
            e.apply(EngineCmd::BeatJump { deck: 1, beats: 1.0 }).unwrap();
            (out, e.snapshot().decks[0].position_ms)
        };
        let (a, jump_new) = run(true);
        let (b, jump_old) = run(false);
        assert_eq!(a, b, "a grid swap must not touch the audio");
        assert!((jump_new - 800.0).abs() < 1e-6, "{jump_new}");
        assert!((jump_old - 1000.0).abs() < 1e-6, "control: the old grid jumps to {jump_old}");
    }

    #[test]
    fn set_beatgrid_is_refused_once_the_deck_holds_other_audio() {
        let mut e = Engine::new(48000);
        assert_eq!(e.regridded(1, grid(500.0, 1.0), None).unwrap_err().code, ErrorCode::NoTrack);
        e.apply(EngineCmd::Load { deck: 1, track: Arc::new(tone(48000, 440.0, 2.0)) }).unwrap();
        let stale = e.regridded(1, grid(400.0, 2.0), None).unwrap();
        let fresh = Arc::new(tone(48000, 880.0, 2.0));
        e.apply(EngineCmd::Load { deck: 1, track: fresh.clone() }).unwrap();
        let r = e.apply(EngineCmd::Regrid { deck: 1, track: stale.clone() }).unwrap_err();
        assert_eq!(r.error.code, ErrorCode::Invalid);
        assert!(Arc::ptr_eq(r.track.as_ref().unwrap(), &stale), "a refused grid is handed back, not dropped here");
        assert!(Arc::ptr_eq(e.deck(1).unwrap().track.as_ref().unwrap(), &fresh));
        // Control: built from the track the deck now holds, it is accepted.
        let ok = e.regridded(1, grid(400.0, 2.0), None).unwrap();
        assert!(e.apply(EngineCmd::Regrid { deck: 1, track: ok }).is_ok());
    }
}
