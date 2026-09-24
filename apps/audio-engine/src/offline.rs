//! Render a plan on the fake clock: as fast as the CPU allows, bit for bit the
//! same every time.
//!
//! Blocks are split at every event frame and every ramp step, so what fires
//! when never depends on the block size.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Instant;

use sha2::{Digest, Sha256};

use crate::deck::Track;
use crate::decode::decode_file;
use crate::engine::{Engine, EngineCmd, ErrorCode, KnobTarget};
use crate::plan::{Action, At, DeckPos, Over, Plan};
use crate::protocol::{Command, LoadSpec, ProtoError};

/// Ramps move their knob in steps this far apart; the engine's own parameter
/// smoothing glides between steps.
pub const RAMP_STEP_FRAMES: u64 = 32;

#[derive(Clone, Debug, PartialEq)]
pub struct Fired {
    pub event: usize,
    pub frame: u64,
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
    let mut cache: HashMap<PathBuf, (u32, Arc<Vec<f32>>)> = HashMap::new();
    move |spec: &LoadSpec| {
        let path = base.join(&spec.path);
        let (sr, pcm) = match cache.get(&path) {
            Some((sr, pcm)) => (*sr, pcm.clone()),
            None => {
                let d = decode_file(&path)?;
                let pcm = Arc::new(d.pcm);
                cache.insert(path.clone(), (d.sample_rate, pcm.clone()));
                (d.sample_rate, pcm)
            }
        };
        Ok(Arc::new(Track::new(sr, (*pcm).clone(), spec.beats.clone(), spec.bpm)))
    }
}

pub fn render_plan_files(plan: &Plan, base: &Path) -> Result<RenderOutput, ProtoError> {
    render_plan(plan, file_loader(base.to_path_buf()))
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
            let target = t.ms_to_frames(target_ms);
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
            // The epsilon absorbs float residue so a playhead that lands a hair
            // short of the target after exactly the computed frames still fires.
            let n = ((target - d.pos) / step - 1e-9).ceil().max(0.0);
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
    mut load: impl FnMut(&LoadSpec) -> Result<Arc<Track>, ProtoError>,
) -> Result<RenderOutput, ProtoError> {
    let sr = plan.sample_rate;
    // Decode everything up front, so decode time is reported apart from render
    // time and the render loop itself never waits on IO.
    let decode_start = Instant::now();
    let mut loaded: HashMap<usize, Arc<Track>> = HashMap::new();
    for (i, ev) in plan.events.iter().enumerate() {
        if let Action::Cmd(Command::Load(spec)) = &ev.action {
            loaded.insert(i, load(spec).map_err(|e| fail(i, e))?);
        }
    }
    let decode_wall_s = decode_start.elapsed().as_secs_f64();

    let mut engine = Engine::new(sr);
    let mut pending: Vec<usize> = (0..plan.events.len()).collect();
    let mut ramps: Vec<ActiveRamp> = Vec::new();
    let mut fired = Vec::new();
    let mut pcm: Vec<f32> = Vec::new();
    let max_frames = at_frame_of_ms(plan.max_ms, sr);
    let render_start = Instant::now();

    loop {
        let now = engine.frame();
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
                if done {
                    ramps.remove(k);
                    continue;
                }
            }
            k += 1;
        }

        if due_in(plan.end, &engine, now) == Some(0) {
            break;
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
        n = n.min(max_frames - now).max(1);

        let start = pcm.len();
        pcm.resize(start + n as usize * 2, 0.0);
        engine.render(&mut pcm[start..]);
    }
    let render_wall_s = render_start.elapsed().as_secs_f64();

    let mut h = Sha256::new();
    for s in &pcm {
        h.update(s.to_le_bytes());
    }
    let sha256 = h.finalize().iter().map(|b| format!("{b:02x}")).collect();
    Ok(RenderOutput {
        sample_rate: sr,
        frames: engine.frame(),
        pcm,
        fired,
        sha256,
        decode_wall_s,
        render_wall_s,
    })
}
