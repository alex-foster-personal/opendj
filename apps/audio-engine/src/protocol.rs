//! Wire protocol v1: newline-delimited JSON.
//!
//! In:  `{"id": "c1", "cmd": {"type": "play", "deck": 1, "playing": true}}`
//! Out: `hello`, `result`, `state` (see `hello_json`, `result_json`, `state_json`).
//!
//! Command names and fields are the audio subset of the page's
//! `PerformanceCommand` (`apps/webui/frontend/src/lib/rb/performance-ipc.svelte.ts`),
//! in the same 0..1 knob units, so anything that already speaks the command
//! bus needs no translation except `load`, which takes a file `path` because
//! the engine does not own the library.
//!
//! Every refusal names its reason: `unsupported` for commands that are not the
//! engine's (the page owns them) or not known at all, `not_implemented` for
//! engine commands this build does not do yet, `invalid` for a bad value.

use serde_json::{json, Map, Value};

use crate::deck::{Beat, MAX_BEATS};
use crate::engine::{DeckId, EngineCmd, EqBand, ErrorCode, KnobTarget, Snapshot, MAX_DECKS};
use crate::mixer::Assign;

pub const PROTOCOL_VERSION: u32 = 1;

/// The longest string id, in bytes. An id is held until its command's
/// result is out, and the engine holds as many commands as its mailbox and
/// load queues take, so an id is kept short; a longer one is refused with a
/// null id, as an id of the wrong type is.
pub const MAX_ID_BYTES: usize = 256;

/// A protocol error with an owned message. Only built off the audio thread.
#[derive(Clone, Debug, PartialEq)]
pub struct ProtoError {
    pub code: ErrorCode,
    pub message: String,
}

impl ProtoError {
    pub fn new(code: ErrorCode, message: impl Into<String>) -> ProtoError {
        ProtoError { code, message: message.into() }
    }
}

impl From<crate::engine::EngineError> for ProtoError {
    fn from(e: crate::engine::EngineError) -> ProtoError {
        ProtoError::new(e.code, e.message)
    }
}

impl From<crate::engine::Rejected> for ProtoError {
    fn from(r: crate::engine::Rejected) -> ProtoError {
        r.error.into()
    }
}

/// A `load` before decoding: the file and its analysis.
#[derive(Clone, Debug, PartialEq)]
pub struct LoadSpec {
    pub deck: DeckId,
    pub path: String,
    pub beats: Vec<Beat>,
    pub bpm: Option<f64>,
}

/// How far a fake clock moves on `engine_advance`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Advance {
    Ms(f64),
    Frames(u64),
}

#[derive(Clone, Debug)]
pub enum Command {
    Load(LoadSpec),
    Apply(EngineCmd),
    /// Accepted and changes nothing: the engine is already in that mode.
    NoOp,
    /// Fake clock only: render this much time now, as fast as possible.
    Advance(Advance),
    /// Ask for one `state` message now.
    State,
    /// Raw MIDI bytes as if they arrived on `port`: the test and replay seam
    /// for plan 20-03. They go through the same decode and dispatch as bytes
    /// the engine reads from a device itself.
    MidiInject { port: String, bytes: Vec<u8> },
    Shutdown,
}

/// Page commands the engine will own in a later plan. Refused with
/// `not_implemented` and the plan that brings them, never silently ignored.
const LATER: &[(&str, &str)] = &[
    ("key_nudge", "key shift needs the time-stretcher (plan 20-04)"),
    ("key_sync", "key shift needs the time-stretcher (plan 20-04)"),
    ("stem_mute", "stems arrive in plan 20-04"),
    ("stem_solo", "stems arrive in plan 20-04"),
    ("stem_gain", "stems arrive in plan 20-04"),
    ("stem_eq_mode", "stems arrive in plan 20-04"),
    ("slip", "slip mode is not built yet"),
    ("beat_sync", "beat sync is not built yet"),
    ("sync_mode", "beat sync is not built yet"),
    ("master", "master election is not built yet"),
    ("hot_cue_trigger", "hot cues are not built yet"),
    ("channel_cue", "the headphone cue bus arrives with device output (plan 20-06)"),
    ("headphone_mix", "the headphone cue bus arrives with device output (plan 20-06)"),
    ("headphone_level", "the headphone cue bus arrives with device output (plan 20-06)"),
    ("head_delay_ms", "the headphone cue bus arrives with device output (plan 20-06)"),
    ("output_mode", "device routing arrives in plan 20-06"),
    ("headphone_output_select", "device routing arrives in plan 20-06"),
    ("headphone_master_select", "device routing arrives in plan 20-06"),
    ("preview_cue", "the headphone cue bus arrives with device output (plan 20-06)"),
    ("preview_stop", "the headphone cue bus arrives with device output (plan 20-06)"),
];

type Obj = Map<String, Value>;

fn invalid(msg: String) -> ProtoError {
    ProtoError::new(ErrorCode::Invalid, msg)
}

/// Refuse any field outside `allowed`, as the page's `_exactKeys` does: a
/// misspelled or newer option ignored here would play something the sender
/// did not ask for, where the page would have refused the command.
pub fn exact_keys(o: &Obj, what: &str, allowed: &[&str]) -> Result<(), ProtoError> {
    let unexpected: Vec<&str> = o.keys().map(String::as_str).filter(|k| !allowed.contains(k)).collect();
    if unexpected.is_empty() {
        Ok(())
    } else {
        Err(invalid(format!("{what} has unexpected fields: {}", unexpected.join(", "))))
    }
}

/// The fields each engine command may carry: the page's `_exactKeys` set for
/// its `PerformanceCommand`, plus this protocol's own (a load's `path`, tag
/// `bpm` and grid). `suppressCommandErrorToast` and `persist` only steer the
/// page's toast and saved settings, so they are accepted and change nothing
/// here.
fn command_keys(ty: &str) -> Option<&'static [&'static str]> {
    Some(match ty {
        "load" => &[
            "type", "deck", "path", "stable_id", "refuseIfMaster", "stems", "suppressCommandErrorToast", "bpm",
            "beatgrid", "beatgrid_ms",
        ],
        "unload" => &["type", "deck", "refuseIfMaster"],
        "play" => &["type", "deck", "playing", "quantize", "start_at_context_sec"],
        "cue" => &["type", "deck"],
        "quantize" | "master_tempo" => &["type", "deck", "enabled"],
        "quantize_grid" | "beat_jump" => &["type", "deck", "beats"],
        "seek" => &["type", "deck", "position_ms"],
        "loop" => &["type", "deck", "loop"],
        "beat_loop" => &["type", "deck", "beats", "start_ms"],
        "tempo" => &["type", "deck", "ratio"],
        "pitch_range" => &["type", "deck", "range"],
        "trim" | "filter" | "fader" => &["type", "deck", "value"],
        "eq" => &["type", "deck", "band", "value"],
        "assign" => &["type", "deck", "assign"],
        "crossfader" | "master_volume" => &["type", "value"],
        "master_mute" => &["type", "muted", "persist"],
        "engine_advance" => &["type", "ms", "frames"],
        "engine_state" | "engine_shutdown" => &["type"],
        "midi_inject" => &["type", "port", "bytes"],
        // Commands this build does not do yet still carry the page's fields,
        // so a malformed one is refused as the page refuses it, not reported
        // as merely not implemented.
        "key_nudge" => &["type", "deck", "semitones"],
        "key_sync" | "stem_eq_mode" | "slip" | "beat_sync" | "channel_cue" => &["type", "deck", "enabled"],
        "stem_mute" => &["type", "deck", "stem", "muted"],
        "stem_solo" => &["type", "deck", "stem", "solo"],
        "stem_gain" => &["type", "deck", "stem", "value"],
        "sync_mode" => &["type", "deck", "mode"],
        "master" => &["type", "deck", "lock"],
        "hot_cue_trigger" => &["type", "deck", "slot"],
        "headphone_mix" | "headphone_level" | "head_delay_ms" => &["type", "value"],
        "output_mode" => &["type", "mode"],
        "headphone_output_select" | "headphone_master_select" => &["type", "device_id"],
        "preview_cue" => &["type", "stable_id", "ratio", "bpm"],
        "preview_stop" => &["type"],
        _ => return None,
    })
}

fn field<'a>(o: &'a Obj, ty: &str, name: &str) -> Result<&'a Value, ProtoError> {
    o.get(name).ok_or_else(|| invalid(format!("{ty}.{name} is required")))
}

fn num(o: &Obj, ty: &str, name: &str) -> Result<f64, ProtoError> {
    let v = field(o, ty, name)?
        .as_f64()
        .ok_or_else(|| invalid(format!("{ty}.{name} must be a number")))?;
    if !v.is_finite() {
        return Err(invalid(format!("{ty}.{name} must be finite")));
    }
    Ok(v)
}

/// A beat loop's length (`signed` false: a positive whole number) or a beat
/// jump's distance (`signed` true: a non-zero whole number), as the page's
/// command parser requires, whatever track is loaded. Bounded here, before
/// the command reaches the engine, so beat-index arithmetic on it can neither
/// saturate a cast nor overflow; the deck refuses the same range again.
fn beat_count(o: &Obj, ty: &str, signed: bool) -> Result<f64, ProtoError> {
    let v = num(o, ty, "beats")?;
    if v.fract() != 0.0 || v == 0.0 || (!signed && v < 0.0) {
        let want = if signed { "a non-zero whole number" } else { "a positive whole number" };
        return Err(invalid(format!("{ty}.beats must be {want}, got {v}")));
    }
    if v.abs() > MAX_BEATS {
        return Err(invalid(format!("{ty}.beats must be at most {MAX_BEATS} either way, got {v}")));
    }
    Ok(v)
}


/// An optional boolean as the page's parser takes one: absent is absent,
/// and anything present, `null` included, must be true or false.
fn opt_bool(o: &Obj, ty: &str, name: &str) -> Result<Option<bool>, ProtoError> {
    if o.contains_key(name) {
        boolean(o, ty, name).map(Some)
    } else {
        Ok(None)
    }
}

/// An optional number as the page's parser takes one: absent is absent, and
/// anything present, `null` included, must be a finite number. Every
/// optional field follows this rule, the engine's own (`bpm`, `ms`) too, so
/// one rule covers the protocol: a field sent is a value.
fn opt_num(o: &Obj, ty: &str, name: &str) -> Result<Option<f64>, ProtoError> {
    if o.contains_key(name) {
        num(o, ty, name).map(Some)
    } else {
        Ok(None)
    }
}

fn unit(o: &Obj, ty: &str, name: &str) -> Result<f64, ProtoError> {
    let v = num(o, ty, name)?;
    if !(0.0..=1.0).contains(&v) {
        return Err(invalid(format!("{ty}.{name} must be within 0..1, got {v}")));
    }
    Ok(v)
}

fn boolean(o: &Obj, ty: &str, name: &str) -> Result<bool, ProtoError> {
    field(o, ty, name)?
        .as_bool()
        .ok_or_else(|| invalid(format!("{ty}.{name} must be true or false")))
}

fn string<'a>(o: &'a Obj, ty: &str, name: &str) -> Result<&'a str, ProtoError> {
    field(o, ty, name)?
        .as_str()
        .ok_or_else(|| invalid(format!("{ty}.{name} must be a string")))
}

pub fn deck_of(o: &Obj, ty: &str) -> Result<DeckId, ProtoError> {
    let d = field(o, ty, "deck")?
        .as_u64()
        .filter(|d| (1..=MAX_DECKS as u64).contains(d))
        .ok_or_else(|| invalid(format!("{ty}.deck must be 1..{MAX_DECKS}")))?;
    Ok(d as DeckId)
}

fn band(o: &Obj, ty: &str) -> Result<EqBand, ProtoError> {
    let s = string(o, ty, "band")?;
    EqBand::parse(s).ok_or_else(|| invalid(format!("{ty}.band must be low, mid or high, got {s}")))
}

/// Beatgrid in either shape the repo already uses: the mirror's
/// `beatgrid: [{n, time_ms}]` (n = beat in bar, 1 = downbeat) or a bare
/// `beatgrid_ms: [..]` with every 4th beat from the first taken as a downbeat.
/// A grid that is sent must be one the page's `validateBeatGrid` accepts: at
/// least two beats, times from 0 strictly increasing, and `n` counting
/// 1, 2, 3, 4 around the bar (a missing `n` counts as its place from the
/// first beat). No grid at all is sent by leaving both keys out.
fn beats(o: &Obj, ty: &str) -> Result<Vec<Beat>, ProtoError> {
    if o.contains_key("beatgrid") && o.contains_key("beatgrid_ms") {
        return Err(invalid(format!("{ty} gives beatgrid and beatgrid_ms; give exactly one")));
    }
    // Sized to the grid: a load is charged what its grid holds.
    let mut out = Vec::new();
    if let Some(v) = o.get("beatgrid") {
        let arr = v.as_array().ok_or_else(|| invalid(format!("{ty}.beatgrid must be an array")))?;
        out.reserve_exact(arr.len());
        let mut prev_n = 0;
        for (i, b) in arr.iter().enumerate() {
            let b = b.as_object().ok_or_else(|| invalid(format!("{ty}.beatgrid[{i}] must be an object")))?;
            exact_keys(b, &format!("{ty}.beatgrid[{i}]"), &["n", "time_ms"])?;
            let time_ms = num(b, ty, "time_ms")?;
            // A missing n counts as its place from the first beat; a present
            // one must be a beat number, as the page's validateBeatGrid
            // requires, `null` included.
            let n = match b.get("n") {
                None => (i % 4) as u64 + 1,
                Some(n) => n
                    .as_u64()
                    .filter(|n| (1..=4).contains(n))
                    .ok_or_else(|| invalid(format!("{ty}.beatgrid[{i}].n must be 1..4")))?,
            };
            if i > 0 && n != prev_n % 4 + 1 {
                return Err(invalid(format!(
                    "{ty}.beatgrid[{i}].n is {n} after {prev_n}; beat numbers must count 1, 2, 3, 4 around the bar"
                )));
            }
            prev_n = n;
            out.push(Beat { time_ms, downbeat: n == 1 });
        }
    } else if let Some(v) = o.get("beatgrid_ms") {
        let arr = v.as_array().ok_or_else(|| invalid(format!("{ty}.beatgrid_ms must be an array")))?;
        out.reserve_exact(arr.len());
        for (i, t) in arr.iter().enumerate() {
            let time_ms = t
                .as_f64()
                .filter(|t| t.is_finite())
                .ok_or_else(|| invalid(format!("{ty}.beatgrid_ms[{i}] must be a finite number")))?;
            out.push(Beat { time_ms, downbeat: i % 4 == 0 });
        }
    }
    if (o.contains_key("beatgrid") || o.contains_key("beatgrid_ms")) && out.len() < 2 {
        return Err(invalid(format!(
            "{ty} beatgrid needs at least 2 beats, got {}; leave it out for a track with no grid",
            out.len()
        )));
    }
    // The page's validateBeatGrid refuses a beat before the track starts; a
    // grid point there would be a cue or a quantize target at a negative time.
    if let Some((i, b)) = out.iter().enumerate().find(|(_, b)| b.time_ms < 0.0) {
        return Err(invalid(format!("{ty} beatgrid times must not be negative (beat {i} at {} ms)", b.time_ms)));
    }
    if out.windows(2).any(|w| w[1].time_ms <= w[0].time_ms) {
        return Err(invalid(format!("{ty} beatgrid times must strictly increase")));
    }
    Ok(out)
}

/// The page's opt-in `refuseIfMaster` guard on load and unload refuses to
/// replace or clear the deck that is sync master. This engine has no master
/// election yet, so it cannot honor the guard: `true` is refused rather than
/// acknowledged and ignored, while `false` or leaving it out is the plain
/// command.
fn refuse_if_master(o: &Obj, ty: &str) -> Result<(), ProtoError> {
    match o.get("refuseIfMaster") {
        None | Some(Value::Bool(false)) => Ok(()),
        Some(Value::Bool(true)) => Err(ProtoError::new(
            ErrorCode::NotImplemented,
            format!("{ty}.refuseIfMaster: this engine has no sync master to protect yet; send {ty} without it"),
        )),
        Some(_) => Err(invalid(format!("{ty}.refuseIfMaster must be true or false"))),
    }
}

/// A string field that must be one of `allowed`.
fn one_of(o: &Obj, ty: &str, name: &str, allowed: &[&str]) -> Result<(), ProtoError> {
    let v = string(o, ty, name)?;
    if !allowed.contains(&v) {
        return Err(invalid(format!("{ty}.{name} must be one of {}, got {v}", allowed.join(", "))));
    }
    Ok(())
}

fn non_empty(o: &Obj, ty: &str, name: &str) -> Result<(), ProtoError> {
    if string(o, ty, name)?.trim().is_empty() {
        return Err(invalid(format!("{ty}.{name} must be a non-empty string")));
    }
    Ok(())
}

/// The page parser's field checks for a command this build does not do yet
/// (`performance-ipc.svelte.ts` `_parseCommand`): a command the page would
/// refuse is `invalid` here too, and only a well-formed one is
/// `not_implemented`. Keys are checked by `command_keys` before this runs.
fn check_later(o: &Obj, ty: &str) -> Result<(), ProtoError> {
    const STEMS: &[&str] = &["vocal", "instrumental", "drums"];
    match ty {
        "key_nudge" => {
            deck_of(o, ty)?;
            let s = num(o, ty, "semitones")?;
            if s != -1.0 && s != 1.0 {
                return Err(invalid(format!("{ty}.semitones must be -1 or 1, got {s}")));
            }
        }
        "key_sync" | "stem_eq_mode" | "slip" | "beat_sync" | "channel_cue" => {
            deck_of(o, ty)?;
            boolean(o, ty, "enabled")?;
        }
        "stem_mute" | "stem_solo" | "stem_gain" => {
            deck_of(o, ty)?;
            one_of(o, ty, "stem", STEMS)?;
            match ty {
                "stem_mute" => drop(boolean(o, ty, "muted")?),
                "stem_solo" => drop(boolean(o, ty, "solo")?),
                _ => drop(unit(o, ty, "value")?),
            }
        }
        "sync_mode" => {
            deck_of(o, ty)?;
            one_of(o, ty, "mode", &["beat", "bar"])?;
        }
        "master" => {
            deck_of(o, ty)?;
            if o.contains_key("lock") {
                boolean(o, ty, "lock")?;
            }
        }
        "hot_cue_trigger" => {
            deck_of(o, ty)?;
            one_of(o, ty, "slot", &["A", "B", "C", "D", "E", "F", "G", "H"])?;
        }
        "headphone_mix" | "headphone_level" => drop(unit(o, ty, "value")?),
        "head_delay_ms" => {
            let v = num(o, ty, "value")?;
            if !(0.0..=500.0).contains(&v) {
                return Err(invalid(format!("{ty}.value must be within 0..500, got {v}")));
            }
        }
        "output_mode" => one_of(o, ty, "mode", &["practice", "two_outputs", "split_cable"])?,
        "headphone_output_select" | "headphone_master_select" => non_empty(o, ty, "device_id")?,
        "preview_cue" => {
            non_empty(o, ty, "stable_id")?;
            unit(o, ty, "ratio")?;
            if o.contains_key("bpm") && !o.get("bpm").and_then(Value::as_f64).is_some_and(|b| b > 0.0) {
                return Err(invalid(format!("{ty}.bpm must be a positive number when given")));
            }
        }
        _ => {}
    }
    Ok(())
}

/// Parse one command object (`{"type": ...}`).
pub fn parse_command(v: &Value) -> Result<Command, ProtoError> {
    let o = v.as_object().ok_or_else(|| invalid("cmd must be an object".into()))?;
    let ty = o
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("cmd.type must be a string".into()))?;
    if let Some(keys) = command_keys(ty) {
        exact_keys(o, ty, keys)?;
    }
    let apply = |c: EngineCmd| Ok(Command::Apply(c));
    match ty {
        "load" => {
            let deck = deck_of(o, ty)?;
            if o.contains_key("stable_id") && !o.contains_key("path") {
                return Err(invalid(
                    "load needs a file path; the supervisor resolves stable_id to a path before sending".into(),
                ));
            }
            // The page-only fields are checked as the page checks them, so a
            // malformed one is refused rather than loading anything.
            if o.contains_key("stable_id") {
                non_empty(o, ty, "stable_id")?;
            }
            opt_bool(o, ty, "suppressCommandErrorToast")?;
            // Stems are plan 20-04. Refuse the stem-aware form rather than
            // acknowledge it and load the plain mix file instead.
            if opt_bool(o, ty, "stems")? == Some(true) {
                return Err(ProtoError::new(
                    ErrorCode::NotImplemented,
                    "load.stems: stem loading is not implemented by this engine yet (plan 20-04); send load without it",
                ));
            }
            refuse_if_master(o, ty)?;
            let path = string(o, ty, "path")?.to_string();
            let bpm = opt_num(o, ty, "bpm")?;
            if bpm.is_some_and(|b| b <= 0.0) {
                return Err(invalid("load.bpm must be positive".into()));
            }
            Ok(Command::Load(LoadSpec { deck, path, beats: beats(o, ty)?, bpm }))
        }
        "unload" => {
            let deck = deck_of(o, ty)?;
            refuse_if_master(o, ty)?;
            apply(EngineCmd::Unload { deck })
        }
        "play" => {
            let deck = deck_of(o, ty)?;
            let playing = boolean(o, ty, "playing")?;
            // The page arms a quantized or scheduled launch from these; playing
            // at once instead would start audio off the grid, so refuse them.
            let start_at = opt_num(o, ty, "start_at_context_sec")?;
            if let Some(t) = start_at.filter(|t| *t < 0.0) {
                return Err(invalid(format!("play.start_at_context_sec must be >= 0, got {t}")));
            }
            if opt_bool(o, ty, "quantize")? == Some(true) {
                return Err(ProtoError::new(
                    ErrorCode::NotImplemented,
                    "play.quantize: quantized launch is not scheduled by this engine yet; send play without it",
                ));
            }
            if start_at.is_some() {
                return Err(ProtoError::new(
                    ErrorCode::NotImplemented,
                    "play.start_at_context_sec: scheduled launch is not implemented by this engine yet",
                ));
            }
            apply(EngineCmd::Play { deck, playing })
        }
        "cue" => apply(EngineCmd::Cue { deck: deck_of(o, ty)? }),
        "quantize" => apply(EngineCmd::Quantize { deck: deck_of(o, ty)?, enabled: boolean(o, ty, "enabled")? }),
        "quantize_grid" => {
            let deck = deck_of(o, ty)?;
            match field(o, ty, "beats")? {
                Value::String(p) if p == "phase" => Err(ProtoError::new(
                    ErrorCode::NotImplemented,
                    "quantize_grid.beats 'phase' is not implemented, as on the page",
                )),
                v => match v.as_u64() {
                    Some(b @ (1 | 4 | 8)) => apply(EngineCmd::QuantizeGrid { deck, beats: b as u8 }),
                    _ => Err(invalid("quantize_grid.beats must be 1, 4 or 8".into())),
                },
            }
        }
        "seek" => {
            let deck = deck_of(o, ty)?;
            // As the page's parser: a position before the track is malformed
            // whatever is loaded, so it is refused here, not by the deck.
            let position_ms = num(o, ty, "position_ms")?;
            if position_ms < 0.0 {
                return Err(invalid(format!("seek.position_ms must be >= 0, got {position_ms}")));
            }
            apply(EngineCmd::Seek { deck, position_ms })
        }
        "loop" => {
            let deck = deck_of(o, ty)?;
            let bounds = match field(o, ty, "loop")? {
                Value::Null => None,
                Value::Object(l) => {
                    exact_keys(l, "loop.loop", &["in_ms", "out_ms"])?;
                    Some((num(l, "loop.loop", "in_ms")?, num(l, "loop.loop", "out_ms")?))
                }
                _ => return Err(invalid("loop.loop must be {in_ms, out_ms} or null".into())),
            };
            apply(EngineCmd::Loop { deck, bounds_ms: bounds })
        }
        "beat_loop" => {
            let deck = deck_of(o, ty)?;
            let beats = beat_count(o, ty, false)?;
            // As the page's parser: an anchor before the track is refused,
            // not taken as the first beat.
            let start_ms = opt_num(o, ty, "start_ms")?;
            if let Some(s) = start_ms.filter(|s| *s < 0.0) {
                return Err(invalid(format!("beat_loop.start_ms must be >= 0, got {s}")));
            }
            apply(EngineCmd::BeatLoop { deck, beats, start_ms })
        }
        "beat_jump" => apply(EngineCmd::BeatJump { deck: deck_of(o, ty)?, beats: beat_count(o, ty, true)? }),
        "tempo" => apply(EngineCmd::Tempo { deck: deck_of(o, ty)?, ratio: num(o, ty, "ratio")? }),
        "pitch_range" => apply(EngineCmd::PitchRange { deck: deck_of(o, ty)?, range: num(o, ty, "range")? }),
        "master_tempo" => {
            deck_of(o, ty)?;
            if boolean(o, ty, "enabled")? {
                Err(ProtoError::new(
                    ErrorCode::NotImplemented,
                    "master_tempo (key lock) needs the time-stretcher (plan 20-04); this build plays varispeed only",
                ))
            } else {
                Ok(Command::NoOp)
            }
        }
        "trim" => apply(EngineCmd::Trim { deck: deck_of(o, ty)?, value: unit(o, ty, "value")? }),
        "eq" => apply(EngineCmd::Eq { deck: deck_of(o, ty)?, band: band(o, ty)?, value: unit(o, ty, "value")? }),
        "filter" => apply(EngineCmd::Filter { deck: deck_of(o, ty)?, value: unit(o, ty, "value")? }),
        "fader" => apply(EngineCmd::Fader { deck: deck_of(o, ty)?, value: unit(o, ty, "value")? }),
        "assign" => {
            let deck = deck_of(o, ty)?;
            let s = string(o, ty, "assign")?;
            let assign = Assign::parse(s).ok_or_else(|| invalid(format!("assign.assign must be A, B or THRU, got {s}")))?;
            apply(EngineCmd::Assign { deck, assign })
        }
        "crossfader" => apply(EngineCmd::Crossfader { value: unit(o, ty, "value")? }),
        "master_volume" => apply(EngineCmd::MasterVolume { value: unit(o, ty, "value")? }),
        "master_mute" => {
            if o.get("persist").is_some_and(|p| !p.is_boolean()) {
                return Err(invalid("master_mute.persist must be true or false".into()));
            }
            apply(EngineCmd::MasterMute { muted: boolean(o, ty, "muted")? })
        }
        "engine_advance" => {
            let given = |k: &str| o.contains_key(k);
            if given("ms") && given("frames") {
                return Err(invalid("engine_advance gives ms and frames; give exactly one".into()));
            }
            if let Some(ms) = opt_num(o, ty, "ms")? {
                if ms < 0.0 {
                    return Err(invalid("engine_advance.ms must not be negative".into()));
                }
                Ok(Command::Advance(Advance::Ms(ms)))
            } else {
                let f = field(o, ty, "frames")?
                    .as_u64()
                    .ok_or_else(|| invalid("engine_advance needs ms or a whole number of frames".into()))?;
                Ok(Command::Advance(Advance::Frames(f)))
            }
        }
        "engine_state" => Ok(Command::State),
        "midi_inject" => {
            let port = string(o, ty, "port")?;
            if port.is_empty() {
                return Err(invalid("midi_inject.port must name the port the bytes arrived on".into()));
            }
            let arr = field(o, ty, "bytes")?
                .as_array()
                .ok_or_else(|| invalid("midi_inject.bytes must be an array of bytes".into()))?;
            if arr.is_empty() || arr.len() > crate::midi::MAX_FEED_BYTES {
                return Err(invalid(format!(
                    "midi_inject.bytes must hold 1..{} bytes, got {}",
                    crate::midi::MAX_FEED_BYTES,
                    arr.len()
                )));
            }
            let bytes = arr
                .iter()
                .enumerate()
                .map(|(i, b)| {
                    b.as_u64()
                        .filter(|b| *b <= 255)
                        .map(|b| b as u8)
                        .ok_or_else(|| invalid(format!("midi_inject.bytes[{i}] must be 0..255, got {b}")))
                })
                .collect::<Result<Vec<u8>, _>>()?;
            Ok(Command::MidiInject { port: port.to_string(), bytes })
        }
        "engine_shutdown" => Ok(Command::Shutdown),
        other => {
            if let Some((_, why)) = LATER.iter().find(|(t, _)| *t == other) {
                check_later(o, other)?;
                return Err(ProtoError::new(ErrorCode::NotImplemented, format!("{other}: {why}")));
            }
            Err(ProtoError::new(
                ErrorCode::Unsupported,
                format!("{other} is not an engine command (the page owns UI-only commands)"),
            ))
        }
    }
}

/// Parse a knob name for ramps: `{"type": "eq", "deck": 1, "band": "low"}`.
pub fn parse_knob(o: &Obj) -> Result<KnobTarget, ProtoError> {
    let ty = o
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("ramp.type must be a string".into()))?;
    // A ramp names its knob beside its own `to` and `over`.
    let keys: &[&str] = match ty {
        "eq" => &["type", "deck", "band", "to", "over"],
        "crossfader" | "master_volume" => &["type", "to", "over"],
        _ => &["type", "deck", "to", "over"],
    };
    exact_keys(o, "ramp", keys)?;
    Ok(match ty {
        "crossfader" => KnobTarget::Crossfader,
        "master_volume" => KnobTarget::MasterVolume,
        "trim" => KnobTarget::Trim(deck_of(o, ty)?),
        "eq" => KnobTarget::Eq(deck_of(o, ty)?, band(o, ty)?),
        "filter" => KnobTarget::Filter(deck_of(o, ty)?),
        "fader" => KnobTarget::Fader(deck_of(o, ty)?),
        "tempo" => KnobTarget::Tempo(deck_of(o, ty)?),
        other => return Err(invalid(format!("{other} cannot be ramped"))),
    })
}

/// An inbound line: `{"id": ..., "cmd": {...}}`. The id is echoed on the
/// result as the same JSON value it arrived as, so a client that matches `1`
/// against its pending map finds `1`, not `"1"`. An id that is not a string
/// or a number is refused before the command runs: it could not be echoed
/// in a form the client would recognize.
pub fn parse_line(line: &str) -> (Option<Value>, Result<Command, ProtoError>) {
    let v: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => return (None, Err(invalid(format!("not JSON: {e}")))),
    };
    let id = match v.get("id") {
        Some(Value::String(s)) if s.len() > MAX_ID_BYTES => {
            return (None, Err(invalid(format!("id is {} bytes; at most {MAX_ID_BYTES}", s.len()))));
        }
        Some(id @ (Value::String(_) | Value::Number(_))) => Some(id.clone()),
        None | Some(Value::Null) => None,
        Some(other) => return (None, Err(invalid(format!("id must be a string or a number, not {other}")))),
    };
    let Some(cmd) = v.get("cmd") else {
        return (id, Err(invalid("message needs a cmd object".into())));
    };
    if let Some(o) = v.as_object() {
        if let Err(e) = exact_keys(o, "message", &["id", "cmd"]) {
            return (id, Err(e));
        }
    }
    (id, parse_command(cmd))
}

pub fn hello_json(clock: &str, sample_rate: u32) -> Value {
    json!({
        "type": "hello",
        "protocol": PROTOCOL_VERSION,
        "engine": concat!("odj-audio ", env!("CARGO_PKG_VERSION")),
        "clock": clock,
        "sample_rate": sample_rate,
        "decks": MAX_DECKS,
    })
}

/// The `ok` for an `engine_state`: `state_seq` numbers the request, and the
/// state that answers it is the first whose own `state_seq` is at least this.
pub fn state_ok_json(id: Option<&Value>, state_seq: u64) -> Value {
    json!({"type": "result", "id": id, "ok": true, "state_seq": state_seq})
}

pub fn result_json(id: Option<&Value>, res: &Result<(), ProtoError>) -> Value {
    match res {
        Ok(()) => json!({"type": "result", "id": id, "ok": true}),
        Err(e) => json!({
            "type": "result",
            "id": id,
            "ok": false,
            "error": {"code": e.code.as_str(), "message": e.message},
        }),
    }
}

/// When a threaded engine's snapshot is heard, on the engine's own monotonic
/// clock. That clock's origin is private to the engine process, so the feed
/// also carries `sent_ns`, the same clock read as the line is written: a
/// receiver maps it onto its own clock with `heard_in_ns`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct HostTime {
    pub heard_ns: u64,
    pub sent_ns: u64,
}

/// The state feed. `rate` is track milliseconds per engine millisecond, so a
/// view can extrapolate `position_ms + rate * elapsed` between messages.
///
/// `host_time_ns` is the engine's monotonic clock at the moment the
/// snapshot's position is heard (the device clock adds its output latency),
/// and `sent_ns` is that clock as the line was written. Their difference,
/// `heard_in_ns`, is how long after the line was written its position is
/// heard (negative once it has been): a receiver adds it to its own clock at
/// receipt, which is off only by the pipe's transit time, and so tracks the
/// audio rather than the render cursor without sharing the engine's clock.
/// All three are null on the fake clock, where wall time means nothing.
///
/// `state_seq` is the last `engine_state` whose preceding commands this state
/// reflects, numbered as its `ok` numbered it (0 before any). States carry no
/// request id and may be written after a later request's `ok`, so a receiver
/// waiting on request `n` takes the first state with `state_seq >= n`.
pub fn state_json(s: &Snapshot, host: Option<HostTime>, state_seq: u64) -> Value {
    let decks: Vec<Value> = s
        .decks
        .iter()
        .enumerate()
        .map(|(i, d)| {
            json!({
                "deck": i + 1,
                "loaded": d.loaded,
                "playing": d.playing,
                "position_ms": d.position_ms,
                "duration_ms": d.duration_ms,
                "rate": if d.playing { d.tempo } else { 0.0 },
                "tempo": d.tempo,
                "cue_ms": d.cue_ms,
                // The page's LoopState: null when no loop is engaged, so
                // `engaged` is always true here and `beat_length` is null for
                // a loop set by bounds.
                "loop": d.loop_ms.map(|(a, b)| json!({
                    "in_ms": a,
                    "out_ms": b,
                    "engaged": true,
                    "beat_length": d.loop_beats,
                })),
                "trim": d.trim,
                "eq": {"low": d.eq[0], "mid": d.eq[1], "high": d.eq[2]},
                "filter": d.filter,
                "fader": d.fader,
                "assign": d.assign.as_str(),
                "pitch_range": d.pitch_range,
                "quantize": {"enabled": d.quantize, "grid_beats": d.quantize_grid},
            })
        })
        .collect();
    json!({
        "type": "state",
        "frame": s.frame,
        "sample_rate": s.sample_rate,
        "engine_time_ns": (s.frame as u128 * 1_000_000_000u128 / s.sample_rate as u128) as u64,
        "host_time_ns": host.map(|h| h.heard_ns),
        "sent_ns": host.map(|h| h.sent_ns),
        "heard_in_ns": host.map(|h| h.heard_ns as i64 - h.sent_ns as i64),
        "state_seq": state_seq,
        "decks": decks,
        "mixer": {"crossfader": s.crossfader, "master_volume": s.master_volume},
        "master": {"muted": s.master_muted},
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn cmd(v: Value) -> Result<Command, ProtoError> {
        parse_command(&v)
    }

    #[test]
    fn page_command_shapes_parse() {
        assert!(matches!(
            cmd(json!({"type": "play", "deck": 1, "playing": true})),
            Ok(Command::Apply(EngineCmd::Play { deck: 1, playing: true }))
        ));
        assert!(matches!(
            cmd(json!({"type": "eq", "deck": 2, "band": "low", "value": 0.0})),
            Ok(Command::Apply(EngineCmd::Eq { deck: 2, band: EqBand::Low, .. }))
        ));
        assert!(matches!(
            cmd(json!({"type": "loop", "deck": 1, "loop": null})),
            Ok(Command::Apply(EngineCmd::Loop { bounds_ms: None, .. }))
        ));
        assert!(matches!(
            cmd(json!({"type": "loop", "deck": 1, "loop": {"in_ms": 1.0, "out_ms": 2.0}})),
            Ok(Command::Apply(EngineCmd::Loop { bounds_ms: Some((1.0, 2.0)), .. }))
        ));
        assert!(matches!(cmd(json!({"type": "master_tempo", "deck": 1, "enabled": false})), Ok(Command::NoOp)));
    }

    #[test]
    fn a_command_not_built_yet_is_checked_as_the_page_checks_it() {
        // Codex's case: a deferred command skipped every field check, so a
        // misspelled or malformed one came back not_implemented, a different
        // answer from the page's. Each is now checked as the page's parser
        // checks it; only a well-formed one is not_implemented.
        let good = [
            json!({"type": "key_nudge", "deck": 1, "semitones": -1}),
            json!({"type": "key_sync", "deck": 1, "enabled": true}),
            json!({"type": "stem_mute", "deck": 1, "stem": "vocal", "muted": true}),
            json!({"type": "stem_solo", "deck": 2, "stem": "drums", "solo": false}),
            json!({"type": "stem_gain", "deck": 3, "stem": "instrumental", "value": 0.5}),
            json!({"type": "stem_eq_mode", "deck": 1, "enabled": false}),
            json!({"type": "slip", "deck": 1, "enabled": true}),
            json!({"type": "beat_sync", "deck": 1, "enabled": true}),
            json!({"type": "sync_mode", "deck": 1, "mode": "bar"}),
            json!({"type": "master", "deck": 1}),
            json!({"type": "hot_cue_trigger", "deck": 4, "slot": "H"}),
            json!({"type": "channel_cue", "deck": 1, "enabled": true}),
            json!({"type": "headphone_mix", "value": 0}),
            json!({"type": "headphone_level", "value": 1}),
            json!({"type": "head_delay_ms", "value": 500}),
            json!({"type": "output_mode", "mode": "split_cable"}),
            json!({"type": "headphone_output_select", "device_id": "d"}),
            json!({"type": "headphone_master_select", "device_id": "d"}),
            json!({"type": "preview_cue", "stable_id": "s", "ratio": 0.25}),
            json!({"type": "preview_stop"}),
        ];
        // Every deferred command is covered here, and has a field list.
        for (t, _) in LATER {
            assert!(good.iter().any(|g| g["type"] == *t), "{t} has no well-formed case");
            assert!(command_keys(t).is_some(), "{t} has no field list");
        }
        for g in &good {
            assert_eq!(cmd(g.clone()).unwrap_err().code, ErrorCode::NotImplemented, "{g}");
        }
        // Optional fields the page takes are taken too.
        for g in [json!({"type": "master", "deck": 1, "lock": true}), json!({"type": "preview_cue", "stable_id": "s", "ratio": 1, "bpm": 124})] {
            assert_eq!(cmd(g.clone()).unwrap_err().code, ErrorCode::NotImplemented, "{g}");
        }
        for bad in [
            json!({"type": "stem_mute", "deck": 1, "stem": "vocal", "muted": true, "quantise": true}),
            json!({"type": "stem_mute", "deck": 1, "stem": "vocals", "muted": true}),
            json!({"type": "stem_mute", "deck": 1, "stem": "vocal"}),
            json!({"type": "stem_gain", "deck": 1, "stem": "drums", "value": 1.5}),
            json!({"type": "key_nudge", "deck": 1, "semitones": 2}),
            json!({"type": "key_sync", "deck": 5, "enabled": true}),
            json!({"type": "slip", "deck": 1, "enabled": "yes"}),
            json!({"type": "sync_mode", "deck": 1, "mode": "phase"}),
            json!({"type": "master", "deck": 1, "lock": 1}),
            json!({"type": "hot_cue_trigger", "deck": 1, "slot": "I"}),
            json!({"type": "headphone_mix", "value": -0.1}),
            json!({"type": "head_delay_ms", "value": 501}),
            json!({"type": "output_mode", "mode": "mono"}),
            json!({"type": "headphone_output_select", "device_id": "  "}),
            json!({"type": "preview_cue", "stable_id": "s", "ratio": 0.5, "bpm": 0}),
            json!({"type": "preview_cue", "ratio": 0.5}),
            json!({"type": "preview_stop", "deck": 1}),
        ] {
            assert_eq!(cmd(bad.clone()).unwrap_err().code, ErrorCode::Invalid, "{bad}");
        }
    }

    #[test]
    fn refusals_name_their_reason() {
        let e = cmd(json!({"type": "stem_mute", "deck": 1, "stem": "vocal", "muted": true})).unwrap_err();
        assert_eq!(e.code, ErrorCode::NotImplemented);
        let e = cmd(json!({"type": "browser_select_playlist", "playlist_id": "x"})).unwrap_err();
        assert_eq!(e.code, ErrorCode::Unsupported);
        let e = cmd(json!({"type": "eq", "deck": 1, "band": "low", "value": 1.2})).unwrap_err();
        assert_eq!(e.code, ErrorCode::Invalid);
        assert!(e.message.contains("eq.value"), "{}", e.message);
        let e = cmd(json!({"type": "master_tempo", "deck": 1, "enabled": true})).unwrap_err();
        assert_eq!(e.code, ErrorCode::NotImplemented);
        let e = cmd(json!({"type": "play", "deck": 1, "playing": true, "quantize": true})).unwrap_err();
        assert_eq!(e.code, ErrorCode::NotImplemented);
        assert!(e.message.contains("quantize"), "{}", e.message);
        let e = cmd(json!({"type": "play", "deck": 1, "playing": true, "start_at_context_sec": 12.5})).unwrap_err();
        assert_eq!(e.code, ErrorCode::NotImplemented);
        assert!(e.message.contains("start_at_context_sec"), "{}", e.message);
        // Control: the unscheduled forms the page also sends still play.
        assert!(matches!(
            cmd(json!({"type": "play", "deck": 1, "playing": true, "quantize": false})),
            Ok(Command::Apply(EngineCmd::Play { playing: true, .. }))
        ));
        let e = cmd(json!({"type": "play", "deck": 5, "playing": true})).unwrap_err();
        assert_eq!(e.code, ErrorCode::Invalid);
        let e = cmd(json!({"type": "load", "deck": 1, "stable_id": "abc"})).unwrap_err();
        assert!(e.message.contains("stable_id"), "{}", e.message);
        let e = cmd(json!({"type": "eq", "deck": 1, "band": "sub", "value": 0.5})).unwrap_err();
        assert_eq!(e.code, ErrorCode::Invalid);
    }

    #[test]
    fn beat_counts_are_bounded_before_dispatch() {
        for (ty, beats) in [("beat_loop", 1e300), ("beat_loop", 65537.0), ("beat_jump", -1e19), ("beat_jump", 65537.0)] {
            let e = cmd(json!({"type": ty, "deck": 1, "beats": beats})).unwrap_err();
            assert_eq!(e.code, ErrorCode::Invalid, "{ty} {beats}");
            assert!(e.message.contains(&format!("{ty}.beats")), "{}", e.message);
        }
        // An anchor before the track is refused, as the page's parser does;
        // 0 and no anchor are fine (controls).
        let e = cmd(json!({"type": "beat_loop", "deck": 1, "beats": 4, "start_ms": -0.5})).unwrap_err();
        assert!(e.message.contains("beat_loop.start_ms"), "{}", e.message);
        assert!(matches!(cmd(json!({"type": "beat_loop", "deck": 1, "beats": 4, "start_ms": 0})), Ok(Command::Apply(EngineCmd::BeatLoop { .. }))));
        assert!(matches!(cmd(json!({"type": "beat_loop", "deck": 1, "beats": 4})), Ok(Command::Apply(EngineCmd::BeatLoop { start_ms: None, .. }))));
        // Control: the bound itself still reaches the engine, either way.
        assert!(matches!(
            cmd(json!({"type": "beat_jump", "deck": 1, "beats": -65536})),
            Ok(Command::Apply(EngineCmd::BeatJump { beats, .. })) if beats == -65536.0
        ));
        assert!(matches!(
            cmd(json!({"type": "beat_loop", "deck": 1, "beats": 65536})),
            Ok(Command::Apply(EngineCmd::BeatLoop { beats, .. })) if beats == 65536.0
        ));
    }

    #[test]
    fn beat_counts_must_be_whole_before_dispatch() {
        for (ty, beats) in [("beat_loop", 0.5), ("beat_loop", 0.0), ("beat_loop", -4.0), ("beat_jump", 0.0), ("beat_jump", 1.5)] {
            let e = cmd(json!({"type": ty, "deck": 1, "beats": beats})).unwrap_err();
            assert_eq!(e.code, ErrorCode::Invalid, "{ty} {beats}");
            assert!(e.message.contains("whole number"), "{}", e.message);
        }
        // Control: a negative whole jump is a real jump backwards.
        assert!(matches!(
            cmd(json!({"type": "beat_jump", "deck": 1, "beats": -4})),
            Ok(Command::Apply(EngineCmd::BeatJump { beats, .. })) if beats == -4.0
        ));
    }

    #[test]
    fn a_stem_aware_load_is_refused_not_loaded_as_the_mix() {
        let e = cmd(json!({"type": "load", "deck": 1, "path": "/x.wav", "stems": true})).unwrap_err();
        assert_eq!(e.code, ErrorCode::NotImplemented);
        assert!(e.message.contains("load.stems"), "{}", e.message);
        // Control: without stems, or with them off, it is an ordinary load.
        let r = cmd(json!({"type": "load", "deck": 1, "path": "/x.wav", "stems": false}));
        assert!(matches!(r, Ok(Command::Load(_))));
        assert!(matches!(cmd(json!({"type": "load", "deck": 1, "path": "/x.wav"})), Ok(Command::Load(_))));
    }

    #[test]
    fn an_optional_page_field_present_is_checked_as_the_page_checks_it() {
        // Codex's case: `"stems": null` loaded the plain mix, and other odd
        // values came back not_implemented, where the page's parser refuses
        // any present optional field that is not the right type. The same
        // holds for every optional field the page reads.
        for bad in [
            json!({"type": "load", "deck": 1, "path": "a.wav", "stems": null}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "stems": "yes"}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "suppressCommandErrorToast": null}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "stable_id": ""}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "stable_id": 7}),
            json!({"type": "play", "deck": 1, "playing": true, "quantize": null}),
            json!({"type": "play", "deck": 1, "playing": true, "quantize": 1}),
            json!({"type": "play", "deck": 1, "playing": true, "start_at_context_sec": null}),
            json!({"type": "play", "deck": 1, "playing": true, "start_at_context_sec": "now"}),
            json!({"type": "play", "deck": 1, "playing": true, "start_at_context_sec": -1}),
            json!({"type": "beat_loop", "deck": 1, "beats": 4, "start_ms": null}),
            // The engine's own optional fields follow the same rule.
            json!({"type": "load", "deck": 1, "path": "a.wav", "bpm": null}),
            json!({"type": "engine_advance", "ms": null, "frames": 480}),
            // Codex's case: a beat number sent as null is not a missing one.
            json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid": [{"n": 1, "time_ms": 0}, {"n": null, "time_ms": 500}]}),
        ] {
            let e = cmd(bad.clone()).unwrap_err();
            assert_eq!(e.code, ErrorCode::Invalid, "{bad}: {}", e.message);
        }
        // Controls: the well-formed values keep their meaning.
        for (good, want) in [
            (json!({"type": "load", "deck": 1, "path": "a.wav", "stems": false, "suppressCommandErrorToast": true, "stable_id": "s"}), None),
            (json!({"type": "play", "deck": 1, "playing": true, "quantize": false}), None),
            (json!({"type": "load", "deck": 1, "path": "a.wav", "stems": true}), Some(ErrorCode::NotImplemented)),
            (json!({"type": "play", "deck": 1, "playing": true, "quantize": true}), Some(ErrorCode::NotImplemented)),
            (json!({"type": "play", "deck": 1, "playing": true, "start_at_context_sec": 0}), Some(ErrorCode::NotImplemented)),
        ] {
            assert_eq!(cmd(good.clone()).err().map(|e| e.code), want, "{good}");
        }
    }

    #[test]
    fn a_master_guarded_load_or_unload_is_refused_not_ignored() {
        // Codex's case: the page's refuseIfMaster guard can't be honored with
        // no master election, so it is refused on both commands.
        for c in [
            json!({"type": "load", "deck": 1, "path": "/x.wav", "refuseIfMaster": true}),
            json!({"type": "unload", "deck": 1, "refuseIfMaster": true}),
        ] {
            let e = cmd(c.clone()).unwrap_err();
            assert_eq!(e.code, ErrorCode::NotImplemented, "{c}");
            assert!(e.message.contains("refuseIfMaster"), "{}", e.message);
        }
        // Controls: false or absent is the plain command; anything else is
        // malformed, as the page's parser has it.
        for c in [
            json!({"type": "load", "deck": 1, "path": "/x.wav", "refuseIfMaster": false}),
            json!({"type": "load", "deck": 1, "path": "/x.wav"}),
        ] {
            assert!(matches!(cmd(c.clone()), Ok(Command::Load(_))), "{c}");
        }
        for c in [json!({"type": "unload", "deck": 2, "refuseIfMaster": false}), json!({"type": "unload", "deck": 2})] {
            assert!(matches!(cmd(c.clone()), Ok(Command::Apply(EngineCmd::Unload { deck: 2 }))), "{c}");
        }
        let e = cmd(json!({"type": "unload", "deck": 1, "refuseIfMaster": "yes"})).unwrap_err();
        assert_eq!(e.code, ErrorCode::Invalid);
    }

    #[test]
    fn the_state_feed_carries_the_loop_as_the_page_holds_it() {
        let mut s = Snapshot {
            frame: 0,
            sample_rate: 48000,
            decks: Default::default(),
            crossfader: 0.5,
            master_volume: 1.0,
            master_muted: false,
        };
        s.decks[0].loop_ms = Some((1000.0, 3000.0));
        s.decks[0].loop_beats = Some(4.0);
        s.decks[1].loop_ms = Some((500.0, 900.0));
        let v = state_json(&s, None, 0);
        assert_eq!(
            v["decks"][0]["loop"],
            json!({"in_ms": 1000.0, "out_ms": 3000.0, "engaged": true, "beat_length": 4.0})
        );
        // A loop set by bounds has no beat length; no loop is null.
        assert_eq!(v["decks"][1]["loop"]["beat_length"], Value::Null);
        assert_eq!(v["decks"][2]["loop"], Value::Null);
    }

    #[test]
    fn quantize_state_is_the_decks_and_phase_is_refused() {
        assert!(matches!(
            cmd(json!({"type": "quantize", "deck": 2, "enabled": false})).unwrap(),
            Command::Apply(EngineCmd::Quantize { deck: 2, enabled: false })
        ));
        for b in [1, 4, 8] {
            assert!(matches!(
                cmd(json!({"type": "quantize_grid", "deck": 1, "beats": b})).unwrap(),
                Command::Apply(EngineCmd::QuantizeGrid { deck: 1, beats }) if beats as u64 == b
            ));
        }
        let e = cmd(json!({"type": "quantize_grid", "deck": 1, "beats": "phase"})).unwrap_err();
        assert_eq!(e.code, ErrorCode::NotImplemented);
        for bad in [json!(2), json!(0), json!("4"), json!(4.5)] {
            let e = cmd(json!({"type": "quantize_grid", "deck": 1, "beats": bad})).unwrap_err();
            assert_eq!(e.code, ErrorCode::Invalid, "{bad}");
        }
        let mut s = Snapshot {
            frame: 0,
            sample_rate: 48000,
            decks: Default::default(),
            crossfader: 0.5,
            master_volume: 1.0,
            master_muted: false,
        };
        s.decks[0].quantize = true;
        s.decks[0].quantize_grid = 4;
        let v = state_json(&s, None, 0);
        assert_eq!(v["decks"][0]["quantize"], json!({"enabled": true, "grid_beats": 4}));
    }

    #[test]
    fn the_state_feed_says_when_its_position_is_heard_relative_to_sending() {
        let s = Snapshot {
            frame: 0,
            sample_rate: 48000,
            decks: Default::default(),
            crossfader: 0.5,
            master_volume: 1.0,
            master_muted: false,
        };
        let v = state_json(&s, Some(HostTime { heard_ns: 5_060_000_000, sent_ns: 5_000_000_000 }), 0);
        assert_eq!(v["host_time_ns"], 5_060_000_000u64);
        assert_eq!(v["sent_ns"], 5_000_000_000u64);
        assert_eq!(v["heard_in_ns"], 60_000_000);
        // A line written after its position was heard says so.
        let v = state_json(&s, Some(HostTime { heard_ns: 100, sent_ns: 250 }), 0);
        assert_eq!(v["heard_in_ns"], -150);
        // Control: the fake clock has no host time at all.
        let v = state_json(&s, None, 0);
        for k in ["host_time_ns", "sent_ns", "heard_in_ns"] {
            assert_eq!(v[k], Value::Null, "{k}");
        }
    }

    #[test]
    fn beatgrids_parse_in_both_shapes() {
        let Command::Load(l) = cmd(json!({
            "type": "load", "deck": 1, "path": "a.wav",
            "beatgrid": [{"n": 4, "time_ms": 0}, {"n": 1, "time_ms": 500}]
        }))
        .unwrap() else { panic!() };
        assert_eq!(l.beats, vec![Beat { time_ms: 0.0, downbeat: false }, Beat { time_ms: 500.0, downbeat: true }]);
        let Command::Load(l) = cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid_ms": [0, 500, 1000, 1500, 2000]})).unwrap() else {
            panic!()
        };
        assert_eq!(l.beats.iter().filter(|b| b.downbeat).count(), 2);
        assert!(cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid_ms": [0, 500, 400]})).is_err());
        // No grid point before the track starts, in either shape, as the
        // page's validateBeatGrid; a grid starting at 0 is fine (control).
        let e = cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid_ms": [-100, 400]})).unwrap_err();
        assert!(e.message.contains("must not be negative"), "{}", e.message);
        let e = cmd(json!({"type": "load", "deck": 1, "path": "a.wav",
            "beatgrid": [{"n": 1, "time_ms": -0.5}, {"n": 2, "time_ms": 400}]}))
        .unwrap_err();
        assert!(e.message.contains("must not be negative"), "{}", e.message);
        assert!(cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid_ms": [0, 400]})).is_ok());
        for n in [0, 5] {
            let e = cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid": [{"n": n, "time_ms": 0}]})).unwrap_err();
            assert!(e.message.contains("must be 1..4"), "n {n}: {}", e.message);
        }
    }

    #[test]
    fn midi_inject_takes_a_port_and_bytes() {
        let Command::MidiInject { port, bytes } =
            cmd(json!({"type": "midi_inject", "port": "DDJ-FLX4", "bytes": [144, 11, 127]})).unwrap()
        else {
            panic!()
        };
        assert_eq!((port.as_str(), bytes), ("DDJ-FLX4", vec![0x90, 0x0b, 0x7f]));
        for (bad, want) in [
            (json!({"type": "midi_inject", "port": "", "bytes": [144]}), "port"),
            (json!({"type": "midi_inject", "port": "p", "bytes": []}), "1..4096"),
            (json!({"type": "midi_inject", "port": "p", "bytes": [256]}), "bytes[0]"),
            (json!({"type": "midi_inject", "port": "p", "bytes": [-1]}), "bytes[0]"),
            (json!({"type": "midi_inject", "port": "p", "bytes": vec![0; 4097]}), "1..4096"),
            (json!({"type": "midi_inject", "port": "p", "bytes": [144], "channel": 1}), "unexpected fields: channel"),
        ] {
            let e = cmd(bad).unwrap_err();
            assert_eq!(e.code, ErrorCode::Invalid);
            assert!(e.message.contains(want), "{want}: {}", e.message);
        }
        // Control: both ends of the byte range and the length bound are taken.
        assert!(cmd(json!({"type": "midi_inject", "port": "p", "bytes": [0, 255]})).is_ok());
        assert!(cmd(json!({"type": "midi_inject", "port": "p", "bytes": vec![0; 4096]})).is_ok());
    }

    #[test]
    fn a_command_naming_two_amounts_is_refused() {
        // Codex's case: an advance with both ms and frames used ms and
        // dropped frames. The same holds for a load with both grid shapes.
        for c in [
            json!({"type": "engine_advance", "ms": 10, "frames": 480}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid": [{"n": 1, "time_ms": 0}, {"n": 2, "time_ms": 500}], "beatgrid_ms": [0, 400]}),
        ] {
            let e = cmd(c.clone()).unwrap_err();
            assert!(e.message.contains("give exactly one"), "{c}: {}", e.message);
        }
        // Controls: each on its own is fine.
        for c in [json!({"type": "engine_advance", "ms": 10}), json!({"type": "engine_advance", "frames": 480})] {
            assert!(matches!(cmd(c.clone()), Ok(Command::Advance(_))), "{c}");
        }
        assert!(matches!(cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid_ms": [0, 400]})), Ok(Command::Load(_))));
    }

    #[test]
    fn a_field_the_page_would_refuse_is_refused() {
        // Codex's case: a misspelled launch option was ignored and the deck
        // started at once, where the page's `_exactKeys` refuses it.
        let e = cmd(json!({"type": "play", "deck": 1, "playing": true, "quantise": true})).unwrap_err();
        assert_eq!(e.code, ErrorCode::Invalid);
        assert!(e.message.contains("unexpected fields: quantise"), "{}", e.message);
        // Every command, and the objects nested in one.
        for c in [
            json!({"type": "seek", "deck": 1, "position_ms": 10, "quantize": false}),
            json!({"type": "fader", "deck": 1, "value": 0.5, "ramp_ms": 100}),
            json!({"type": "crossfader", "value": 0.5, "deck": 1}),
            json!({"type": "engine_state", "verbose": true}),
            json!({"type": "loop", "deck": 1, "loop": {"in_ms": 0, "out_ms": 500, "beats": 1}}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid": [{"n": 1, "time_ms": 0}, {"n": 2, "time_ms": 500, "bpm": 120}]}),
        ] {
            let e = cmd(c.clone()).unwrap_err();
            assert!(e.message.contains("unexpected fields"), "{c}: {}", e.message);
        }
        let (id, r) = parse_line(r#"{"id": "x", "cmd": {"type": "engine_state"}, "urgent": true}"#);
        assert_eq!(id, Some(json!("x")));
        assert!(r.unwrap_err().message.contains("unexpected fields: urgent"));
        // Controls: each command with every field it may carry is accepted,
        // including the page-only ones that steer only the page.
        for c in [
            json!({"type": "play", "deck": 1, "playing": true, "quantize": false}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "stable_id": "s", "refuseIfMaster": false, "stems": false, "suppressCommandErrorToast": true, "bpm": 120, "beatgrid_ms": [0, 500]}),
            json!({"type": "master_mute", "muted": true, "persist": true}),
            json!({"type": "beat_loop", "deck": 1, "beats": 4, "start_ms": 0}),
            json!({"type": "eq", "deck": 1, "band": "low", "value": 0.5}),
            json!({"type": "loop", "deck": 1, "loop": {"in_ms": 0, "out_ms": 500}}),
            json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid": [{"n": 1, "time_ms": 0}, {"time_ms": 500}]}),
        ] {
            assert!(cmd(c.clone()).is_ok(), "{c}: {:?}", cmd(c.clone()).err().map(|e| e.message));
        }
        assert!(parse_line(r#"{"id": 1, "cmd": {"type": "engine_state"}}"#).1.is_ok());
        assert!(cmd(json!({"type": "master_mute", "muted": true, "persist": 1})).is_err());
    }

    #[test]
    fn a_grid_the_page_would_refuse_is_refused() {
        let load = |grid: Value| cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid": grid}));
        let beat = |n: u64, t: u64| json!({"n": n, "time_ms": t});
        // Codex's case: two downbeats in a row, and the n count broken
        // anywhere else, including at the bar wrap.
        for grid in [
            json!([beat(1, 0), beat(1, 500)]),
            json!([beat(1, 0), beat(3, 500)]),
            json!([beat(3, 0), beat(4, 500), beat(2, 1000)]),
            json!([beat(2, 0), beat(3, 500), {"time_ms": 1000}]),
        ] {
            let e = load(grid.clone()).unwrap_err();
            assert!(e.message.contains("count 1, 2, 3, 4"), "{grid}: {}", e.message);
        }
        // Controls: a count starting anywhere and wrapping 4 to 1 is a grid,
        // and a missing n counts as its place from the first beat.
        for grid in [
            json!([beat(3, 0), beat(4, 500), beat(1, 1000), beat(2, 1500)]),
            json!([beat(1, 0), beat(2, 500), beat(3, 1000), beat(4, 1500), beat(1, 2000)]),
            json!([{"time_ms": 0}, {"time_ms": 500}, beat(3, 1000)]),
        ] {
            assert!(matches!(load(grid.clone()), Ok(Command::Load(_))), "{grid}");
        }
        // Fewer than two beats is no grid, in either shape; leaving the grid
        // out is the way to load without one (control).
        for (key, grid) in [("beatgrid", json!([])), ("beatgrid", json!([beat(1, 0)])), ("beatgrid_ms", json!([])), ("beatgrid_ms", json!([0]))] {
            let e = cmd(json!({"type": "load", "deck": 1, "path": "a.wav", key: grid})).unwrap_err();
            assert!(e.message.contains("at least 2 beats"), "{key} {grid}: {}", e.message);
        }
        let Command::Load(l) = cmd(json!({"type": "load", "deck": 1, "path": "a.wav"})).unwrap() else { panic!() };
        assert!(l.beats.is_empty());
    }

    #[test]
    fn lines_echo_their_id() {
        let (id, c) = parse_line(r#"{"id": "c7", "cmd": {"type": "engine_state"}}"#);
        assert_eq!(id, Some(json!("c7")));
        assert!(matches!(c, Ok(Command::State)));
        let (id, c) = parse_line("not json");
        assert!(id.is_none() && c.is_err());
    }

    #[test]
    fn an_id_is_held_short_and_a_grid_is_held_at_its_size() {
        // Codex on 6d03ee97: the engine holds a command's id until its
        // result is out, for as many commands as it queues, so an id's size
        // is bounded, as a load's grid is by the bytes it is charged.
        let line = |id: Value| json!({"id": id, "cmd": {"type": "engine_state"}}).to_string();
        let (id, c) = parse_line(&line(json!("x".repeat(MAX_ID_BYTES))));
        assert!(c.is_ok(), "an id at the bound is refused");
        assert_eq!(id.and_then(|v| v.as_str().map(str::len)), Some(MAX_ID_BYTES));
        let (id, c) = parse_line(&line(json!("x".repeat(MAX_ID_BYTES + 1))));
        assert!(id.is_none(), "a refused id is echoed");
        assert!(c.unwrap_err().message.contains(&format!("at most {MAX_ID_BYTES}")));
        // A number is fixed-size, however it is spelled.
        assert!(parse_line(&line(json!(u64::MAX))).1.is_ok());
        for key in ["beatgrid_ms", "beatgrid"] {
            let grid: Vec<Value> = (0..5)
                .map(|i| if key == "beatgrid" { json!({"time_ms": i * 500}) } else { json!(i * 500) })
                .collect();
            let (_, c) = parse_line(&json!({"cmd": {"type": "load", "deck": 1, "path": "a.wav", key: grid}}).to_string());
            let Ok(Command::Load(spec)) = c else { panic!("{c:?}") };
            assert_eq!((spec.beats.len(), spec.beats.capacity()), (5, 5), "{key}");
        }
    }

    #[test]
    fn a_seek_before_the_track_is_malformed_whatever_is_loaded() {
        // Codex on b16e9fa7: `position_ms: -1` reached the deck, which on an
        // empty deck answered no_track, where the page's parser refuses the
        // command itself as invalid.
        let seek = |ms: Value| parse_command(&json!({"type": "seek", "deck": 1, "position_ms": ms}));
        for ms in [json!(-1), json!(-0.001)] {
            let e = seek(ms.clone()).unwrap_err();
            assert_eq!(e.code, ErrorCode::Invalid, "{ms}");
            assert!(e.message.contains("position_ms must be >= 0"), "{ms}: {}", e.message);
        }
        // Control: 0 and later parse, and are the deck's to judge.
        for ms in [json!(0), json!(-0.0), json!(1500.5)] {
            assert!(matches!(seek(ms.clone()), Ok(Command::Apply(EngineCmd::Seek { deck: 1, .. }))), "{ms}");
        }
    }

    #[test]
    fn a_numeric_id_is_echoed_as_a_number() {
        // Codex's case: `{"id": 1}` came back as `"id": "1"`, which a client
        // keyed on the number never matches.
        for (line, want) in [
            (r#"{"id": 1, "cmd": {"type": "engine_state"}}"#, json!(1)),
            (r#"{"id": -2.5, "cmd": {"type": "engine_state"}}"#, json!(-2.5)),
            (r#"{"id": "1", "cmd": {"type": "engine_state"}}"#, json!("1")),
        ] {
            let (id, c) = parse_line(line);
            assert!(c.is_ok(), "{line}");
            assert_eq!(id.as_ref(), Some(&want), "{line}");
            assert_eq!(result_json(id.as_ref(), &Ok(()))["id"], want, "{line}");
        }
        // No id, or a null one, answers with a null id (control).
        for line in [r#"{"cmd": {"type": "engine_state"}}"#, r#"{"id": null, "cmd": {"type": "engine_state"}}"#] {
            let (id, c) = parse_line(line);
            assert!(id.is_none() && c.is_ok(), "{line}");
            assert_eq!(result_json(id.as_ref(), &Ok(()))["id"], Value::Null);
        }
        // An id that cannot be echoed as sent is refused before the command
        // runs, not run with its id dropped.
        for line in [
            r#"{"id": true, "cmd": {"type": "engine_shutdown"}}"#,
            r#"{"id": {"n": 1}, "cmd": {"type": "engine_shutdown"}}"#,
            r#"{"id": [1], "cmd": {"type": "engine_shutdown"}}"#,
        ] {
            let (id, c) = parse_line(line);
            assert!(id.is_none(), "{line}");
            assert!(c.unwrap_err().message.contains("id must be a string or a number"), "{line}");
        }
    }
}
