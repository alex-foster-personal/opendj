//! Render a plan on the fake clock: as fast as the CPU allows, bit for bit the
//! same every time.
//!
//! Blocks are split at every event frame, every ramp step and every timeline
//! step, so what fires when never depends on the block size.
//!
//! Besides the mix, a render reports what a transition scorer needs: which
//! decks are heard over time, where they overlap and how long each plays alone
//! either side, each deck's tempo, and optionally each deck's own audio.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use sha2::{Digest, Sha256};

use crate::deck::Track;
use crate::decode::decode_file;
use crate::engine::{DeckId, Engine, EngineCmd, ErrorCode, KnobTarget, MAX_DECKS};
use crate::plan::{Action, At, DeckPos, Over, Plan};
use crate::protocol::{Command, LoadSpec, ProtoError};

/// Ramps move their knob in steps this far apart; the engine's own parameter
/// smoothing glides between steps.
pub const RAMP_STEP_FRAMES: u64 = 32;
/// How close to a deck-relative target counts as reaching it, in source frames.
pub const POS_EPS_FRAMES: f64 = 1e-3;

/// Which decks are heard is sampled this often, and on every frame an event
/// fires, so segment edges are exact for commands and within 10 ms for ramps.
pub const TIMELINE_STEP_MS: u32 = 10;

#[derive(Clone, Debug, PartialEq)]
pub struct Fired {
    pub event: usize,
    pub frame: u64,
}

/// A span of output in which the same decks are heard (see `Deck::audible`).
#[derive(Clone, Debug, PartialEq)]
pub struct Segment {
    pub start: u64,
    pub end: u64,
    /// Bit `d - 1` is set when deck `d` is heard.
    pub decks: u8,
}

/// One deck heard alone, next to an overlap.
#[derive(Clone, Debug, PartialEq)]
pub struct Solo {
    pub deck: DeckId,
    pub frames: u64,
}

/// A run of segments with two or more decks heard, and the solo segment on
/// either side of it when there is one.
#[derive(Clone, Debug, PartialEq)]
pub struct Overlap {
    pub start: u64,
    pub end: u64,
    /// Every deck heard at some point in the overlap, as in `Segment::decks`.
    pub decks: u8,
    pub solo_before: Option<Solo>,
    pub solo_after: Option<Solo>,
}

/// A deck's tempo from `frame` on. `bpm` is the grid's local BPM (or the tag
/// BPM) times the tempo, read where the playhead was at that frame.
#[derive(Clone, Debug, PartialEq)]
pub struct TempoPoint {
    pub deck: DeckId,
    pub frame: u64,
    pub tempo: f64,
    pub bpm: Option<f64>,
}

/// One deck's own audio: after its channel strip, crossfader included, before
/// the master gain. The decks' outputs sum to the mix before master gain.
pub struct DeckOutput {
    pub deck: DeckId,
    /// Interleaved stereo, the same length as the mix.
    pub pcm: Vec<f32>,
    pub sha256: String,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct RenderOptions {
    /// Also return each loaded deck's own audio (memory: one more mix-sized
    /// buffer per deck).
    pub deck_outputs: bool,
}

pub struct RenderOutput {
    pub sample_rate: u32,
    pub frames: u64,
    /// Interleaved stereo.
    pub pcm: Vec<f32>,
    pub fired: Vec<Fired>,
    pub sha256: String,
    pub decode_wall_s: f64,
    pub render_wall_s: f64,
    pub timeline: Vec<Segment>,
    pub overlaps: Vec<Overlap>,
    pub tempo: Vec<TempoPoint>,
    /// Empty unless `RenderOptions::deck_outputs`.
    pub decks: Vec<DeckOutput>,
}

impl RenderOutput {
    /// Seconds of audio per second of wall time spent rendering (decode excluded).
    pub fn realtime_factor(&self) -> f64 {
        (self.frames as f64 / self.sample_rate as f64) / self.render_wall_s.max(1e-9)
    }
}

struct ActiveRamp {
    target: KnobTarget,
    from: f64,
    to: f64,
    start: u64,
    len: u64,
    next: u64,
    event: usize,
}

/// Loads every track the plan names, decoding each distinct file once.
/// Relative paths resolve against `base`.
pub fn file_loader(base: PathBuf) -> impl FnMut(&LoadSpec) -> Result<Arc<Track>, ProtoError> {
    let mut cache: HashMap<PathBuf, (u32, Arc<[f32]>)> = HashMap::new();
    move |spec: &LoadSpec| {
        let path = base.join(&spec.path);
        let (sr, pcm) = match cache.get(&path) {
            Some((sr, pcm)) => (*sr, pcm.clone()),
            None => {
                let d = decode_file(&path)?;
                let pcm: Arc<[f32]> = d.pcm.into();
                cache.insert(path.clone(), (d.sample_rate, pcm.clone()));
                (d.sample_rate, pcm)
            }
        };
        // Every load of one file shares the cached samples; nothing is copied.
        Ok(Arc::new(Track::new(sr, pcm, spec.beats.clone(), spec.bpm)))
    }
}

pub fn render_plan_files(plan: &Plan, base: &Path) -> Result<RenderOutput, ProtoError> {
    render_plan(plan, file_loader(base.to_path_buf()))
}

pub fn render_plan_files_with(plan: &Plan, base: &Path, opts: RenderOptions) -> Result<RenderOutput, ProtoError> {
    render_plan_with(plan, file_loader(base.to_path_buf()), opts)
}

fn sha256_hex(pcm: &[f32]) -> String {
    let mut h = Sha256::new();
    for s in pcm {
        h.update(s.to_le_bytes());
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

/// Records which decks are heard and each deck's tempo as the render goes.
struct Observer {
    segments: Vec<Segment>,
    tempo: Vec<TempoPoint>,
    /// Last recorded (tempo, track identity) per deck.
    last: [Option<(f64, usize)>; MAX_DECKS],
}

impl Observer {
    fn new() -> Observer {
        Observer { segments: Vec::new(), tempo: Vec::new(), last: [None; MAX_DECKS] }
    }

    fn observe(&mut self, engine: &Engine, now: u64) {
        let mut mask = 0u8;
        for i in 0..MAX_DECKS {
            let id = i as DeckId + 1;
            let Some(d) = engine.deck(id) else { continue };
            if d.audible() {
                mask |= 1 << i;
            }
            let key = d.track.as_ref().map(|t| (d.tempo, Arc::as_ptr(t) as usize));
            if key.is_some() && key != self.last[i] {
                let t = d.track.as_ref().expect("key is some");
                self.tempo.push(TempoPoint {
                    deck: id,
                    frame: now,
                    tempo: d.tempo,
                    bpm: t.bpm_at(t.frames_to_ms(d.pos)).map(|b| b * d.tempo),
                });
            }
            self.last[i] = key;
        }
        let seg = Segment { start: now, end: now, decks: mask };
        match self.segments.last_mut() {
            Some(l) if l.decks == mask => {}
            // Changed twice on one frame: the earlier state lasted no time.
            Some(l) if l.start == now => {
                l.decks = mask;
                let n = self.segments.len();
                if n >= 2 && self.segments[n - 2].decks == mask {
                    self.segments.pop();
                }
            }
            Some(l) => {
                l.end = now;
                self.segments.push(seg);
            }
            None => self.segments.push(seg),
        }
    }

    fn finish(mut self, end: u64) -> (Vec<Segment>, Vec<Overlap>, Vec<TempoPoint>) {
        if let Some(l) = self.segments.last_mut() {
            l.end = end;
            if l.start == l.end {
                self.segments.pop();
            }
        }
        let overlaps = overlaps(&self.segments);
        (self.segments, overlaps, self.tempo)
    }
}

fn solo(seg: Option<&Segment>) -> Option<Solo> {
    let s = seg?;
    (s.decks.count_ones() == 1).then(|| Solo { deck: s.decks.trailing_zeros() as DeckId + 1, frames: s.end - s.start })
}

fn overlaps(segments: &[Segment]) -> Vec<Overlap> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < segments.len() {
        if segments[i].decks.count_ones() < 2 {
            i += 1;
            continue;
        }
        let first = i;
        let mut decks = 0u8;
        while i < segments.len() && segments[i].decks.count_ones() >= 2 {
            decks |= segments[i].decks;
            i += 1;
        }
        out.push(Overlap {
            start: segments[first].start,
            end: segments[i - 1].end,
            decks,
            solo_before: solo(first.checked_sub(1).map(|k| &segments[k])),
            solo_after: solo(segments.get(i)),
        });
    }
    out
}

fn at_frame_of_ms(ms: f64, sr: u32) -> u64 {
    (ms * sr as f64 / 1000.0).round() as u64
}

/// Frames from now until `at` is due: Some(0) means now, None means it cannot
/// happen from the current state (the deck is empty, paused, or looping
/// short of the target).
fn due_in(at: At, engine: &Engine, now: u64) -> Option<u64> {
    let sr = engine.sample_rate();
    match at {
        At::Frame(f) => Some(f.saturating_sub(now)),
        At::Ms(ms) => Some(at_frame_of_ms(ms, sr).saturating_sub(now)),
        At::Deck { deck, pos } => {
            let d = engine.deck(deck)?;
            let t = d.track.as_ref()?;
            let target_ms = match pos {
                DeckPos::Ms(ms) => Some(ms),
                DeckPos::Bar(b) => t.bar_time_ms(b),
                DeckPos::Beat(b) => t.beat_time_ms(b),
            }?;
            // A thousandth of a frame (about 20 ns) counts as there: the
            // playhead is exact to float rounding, not to zero.
            let target = t.ms_to_frames(target_ms) - POS_EPS_FRAMES;
            if d.pos >= target {
                return Some(0);
            }
            if !d.playing {
                return None;
            }
            if let Some((a, b)) = d.looping {
                if d.pos >= a && d.pos < b && target >= b {
                    return None;
                }
            }
            let step = d.step(sr as f64);
            if step <= 0.0 {
                return None;
            }
            let n = ((target - d.pos) / step).ceil().max(0.0);
            Some(n as u64)
        }
    }
}

fn fail(event: usize, e: ProtoError) -> ProtoError {
    ProtoError::new(e.code, format!("events[{event}]: {}", e.message))
}

/// Render `plan`, resolving each `load` through `load`.
pub fn render_plan(
    plan: &Plan,
    load: impl FnMut(&LoadSpec) -> Result<Arc<Track>, ProtoError>,
) -> Result<RenderOutput, ProtoError> {
    render_plan_with(plan, load, RenderOptions::default())
}

pub fn render_plan_with(
    plan: &Plan,
    mut load: impl FnMut(&LoadSpec) -> Result<Arc<Track>, ProtoError>,
    opts: RenderOptions,
) -> Result<RenderOutput, ProtoError> {
    let sr = plan.sample_rate;
    // Decode everything up front, so decode time is reported apart from render
    // time and the render loop itself never waits on IO.
    let decode_start = Instant::now();
    let mut loaded: HashMap<usize, Arc<Track>> = HashMap::new();
    let mut loads_deck = [false; MAX_DECKS];
    for (i, ev) in plan.events.iter().enumerate() {
        if let Action::Cmd(Command::Load(spec)) = &ev.action {
            loaded.insert(i, load(spec).map_err(|e| fail(i, e))?);
            loads_deck[spec.deck as usize - 1] = true;
        }
    }
    let decode_wall_s = decode_start.elapsed().as_secs_f64();

    let mut engine = Engine::new(sr);
    let mut pending: Vec<usize> = (0..plan.events.len()).collect();
    let mut ramps: Vec<ActiveRamp> = Vec::new();
    let mut fired = Vec::new();
    let mut pcm: Vec<f32> = Vec::new();
    let max_frames = at_frame_of_ms(plan.max_ms, sr);
    let tl_step = (sr as u64 * TIMELINE_STEP_MS as u64 / 1000).max(1);
    let mut observer = Observer::new();
    let mut scratch: [Vec<f32>; MAX_DECKS] = Default::default();
    let mut deck_pcm: [Vec<f32>; MAX_DECKS] = Default::default();
    let render_start = Instant::now();

    loop {
        let now = engine.frame();
        let fired_before = fired.len();
        // Fire every due event in plan order. Firing one can make another due
        // (a seek past a bar), so scan until nothing more fires.
        loop {
            let mut any = false;
            let mut i = 0;
            while i < pending.len() {
                let idx = pending[i];
                let ev = &plan.events[idx];
                if due_in(ev.at, &engine, now) != Some(0) {
                    i += 1;
                    continue;
                }
                pending.remove(i);
                any = true;
                fired.push(Fired { event: idx, frame: now });
                match &ev.action {
                    Action::Cmd(Command::Load(spec)) => {
                        let track = loaded.remove(&idx).expect("decoded above");
                        engine.apply(EngineCmd::Load { deck: spec.deck, track }).map_err(|e| fail(idx, e.into()))?;
                    }
                    Action::Cmd(Command::Apply(cmd)) => {
                        engine.apply(cmd.clone()).map_err(|e| fail(idx, e.into()))?;
                    }
                    Action::Cmd(Command::NoOp) => {}
                    Action::Cmd(_) => unreachable!("rejected by parse_plan"),
                    Action::Ramp(r) => {
                        let from = engine
                            .knob(r.target)
                            .ok_or_else(|| fail(idx, ProtoError::new(ErrorCode::Invalid, "ramp target deck does not exist")))?;
                        let len = match r.over {
                            Over::Frames(f) => f,
                            Over::Ms(ms) => at_frame_of_ms(ms, sr).max(1),
                            Over::Beats { deck, beats } => {
                                let d = engine.deck(deck).ok_or_else(|| fail(idx, ProtoError::new(ErrorCode::Invalid, "no such deck")))?;
                                let t = d.track.as_ref().ok_or_else(|| {
                                    fail(idx, ProtoError::new(ErrorCode::NoTrack, "a ramp measured in beats needs a loaded deck"))
                                })?;
                                let no_grid = || fail(idx, ProtoError::new(ErrorCode::NoBeatgrid, "the ramp's deck has no beatgrid or bpm"));
                                let here = t.frames_to_ms(d.pos);
                                let b0 = t.beat_index_at(here).ok_or_else(no_grid)?;
                                let track_ms = t.beat_time_ms(b0 + beats).ok_or_else(no_grid)? - here;
                                at_frame_of_ms(track_ms / d.tempo, sr).max(1)
                            }
                        };
                        // A newer ramp on the same knob replaces the older one.
                        ramps.retain(|a| a.target != r.target);
                        ramps.push(ActiveRamp { target: r.target, from, to: r.to, start: now, len, next: now, event: idx });
                    }
                }
            }
            if !any {
                break;
            }
        }

        // Ramp steps due now.
        let mut tempo_stepped = false;
        let mut k = 0;
        while k < ramps.len() {
            let r = &mut ramps[k];
            if r.next == now {
                let t = ((now - r.start) as f64 / r.len as f64).min(1.0);
                let v = r.from + (r.to - r.from) * t;
                let (target, event) = (r.target, r.event);
                let done = now >= r.start + r.len;
                if !done {
                    r.next = (now + RAMP_STEP_FRAMES).min(r.start + r.len);
                }
                engine.apply(target.command(v)).map_err(|e| fail(event, e.into()))?;
                tempo_stepped |= matches!(target, KnobTarget::Tempo(_));
                if done {
                    ramps.remove(k);
                    continue;
                }
            }
            k += 1;
        }

        // A tempo ramp step is recorded on its own frame, so the tempo series
        // matches the varispeed the audio actually got.
        if now.is_multiple_of(tl_step) || fired.len() > fired_before || tempo_stepped {
            observer.observe(&engine, now);
        }

        let end_in = due_in(plan.end, &engine, now);
        if end_in == Some(0) {
            break;
        }
        // Nothing left can change the engine: every pending event waits on a
        // deck that cannot get there, no ramp is running, and the end cannot
        // arrive from this state either. Say so now, rather than render (and
        // hold in memory) silence all the way to max_ms.
        if end_in.is_none()
            && ramps.is_empty()
            && pending.iter().all(|&idx| due_in(plan.events[idx].at, &engine, now).is_none())
        {
            return Err(ProtoError::new(
                ErrorCode::Invalid,
                format!(
                    "plan.end can never be reached: at {} ms no deck is moving toward it and no event or ramp is left that could change that",
                    now * 1000 / sr as u64
                ),
            ));
        }
        if now >= max_frames {
            return Err(ProtoError::new(
                ErrorCode::Invalid,
                format!("plan.end was not reached within max_ms ({} ms)", plan.max_ms),
            ));
        }

        let mut n = plan.block_frames as u64;
        for &idx in &pending {
            if let Some(d) = due_in(plan.events[idx].at, &engine, now) {
                n = n.min(d);
            }
        }
        for r in &ramps {
            n = n.min(r.next - now);
        }
        if let Some(d) = due_in(plan.end, &engine, now) {
            n = n.min(d);
        }
        n = n.min(tl_step - now % tl_step);
        n = n.min(max_frames - now).max(1);

        let start = pcm.len();
        pcm.resize(start + n as usize * 2, 0.0);
        if opts.deck_outputs {
            for s in scratch.iter_mut() {
                s.resize(n as usize * 2, 0.0);
            }
            let mut split = scratch.each_mut().map(|v| v.as_mut_slice());
            engine.render_split(&mut pcm[start..], &mut split);
            for i in 0..MAX_DECKS {
                if loads_deck[i] {
                    deck_pcm[i].extend_from_slice(&scratch[i]);
                }
            }
        } else {
            engine.render(&mut pcm[start..]);
        }
    }
    let render_wall_s = render_start.elapsed().as_secs_f64();

    let sha256 = sha256_hex(&pcm);
    let (timeline, overlaps, tempo) = observer.finish(engine.frame());
    let decks = if opts.deck_outputs {
        deck_pcm
            .into_iter()
            .enumerate()
            .filter(|(i, _)| loads_deck[*i])
            .map(|(i, pcm)| DeckOutput { deck: i as DeckId + 1, sha256: sha256_hex(&pcm), pcm })
            .collect()
    } else {
        Vec::new()
    };
    Ok(RenderOutput {
        sample_rate: sr,
        frames: engine.frame(),
        pcm,
        fired,
        sha256,
        decode_wall_s,
        render_wall_s,
        timeline,
        overlaps,
        tempo,
        decks,
    })
}
