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
    ("quantize", "quantized launch is not built yet"),
    ("quantize_grid", "quantized launch is not built yet"),
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

/// A beat loop's length or a beat jump's distance. Bounded here, before the
/// command reaches the engine, so beat-index arithmetic on it can neither
/// saturate a cast nor overflow; the deck refuses the same range again.
fn beat_count(o: &Obj, ty: &str) -> Result<f64, ProtoError> {
    let v = num(o, ty, "beats")?;
    if v.abs() > MAX_BEATS {
        return Err(invalid(format!("{ty}.beats must be at most {MAX_BEATS} either way, got {v}")));
    }
    Ok(v)
}

fn opt_num(o: &Obj, ty: &str, name: &str) -> Result<Option<f64>, ProtoError> {
    match o.get(name) {
        None | Some(Value::Null) => Ok(None),
        Some(_) => num(o, ty, name).map(Some),
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
fn beats(o: &Obj, ty: &str) -> Result<Vec<Beat>, ProtoError> {
    let mut out = Vec::new();
    if let Some(v) = o.get("beatgrid") {
        let arr = v.as_array().ok_or_else(|| invalid(format!("{ty}.beatgrid must be an array")))?;
        for (i, b) in arr.iter().enumerate() {
            let b = b.as_object().ok_or_else(|| invalid(format!("{ty}.beatgrid[{i}] must be an object")))?;
            let time_ms = num(b, ty, "time_ms")?;
            let downbeat = match b.get("n") {
                None | Some(Value::Null) => i % 4 == 0,
                Some(n) => {
                    n.as_u64()
                        .filter(|n| (1..=4).contains(n))
                        .ok_or_else(|| invalid(format!("{ty}.beatgrid[{i}].n must be 1..4")))?
                        == 1
                }
            };
            out.push(Beat { time_ms, downbeat });
        }
    } else if let Some(v) = o.get("beatgrid_ms") {
        let arr = v.as_array().ok_or_else(|| invalid(format!("{ty}.beatgrid_ms must be an array")))?;
        for (i, t) in arr.iter().enumerate() {
            let time_ms = t
                .as_f64()
                .filter(|t| t.is_finite())
                .ok_or_else(|| invalid(format!("{ty}.beatgrid_ms[{i}] must be a finite number")))?;
            out.push(Beat { time_ms, downbeat: i % 4 == 0 });
        }
    }
    if out.windows(2).any(|w| w[1].time_ms <= w[0].time_ms) {
        return Err(invalid(format!("{ty} beatgrid times must strictly increase")));
    }
    Ok(out)
}

/// Parse one command object (`{"type": ...}`).
pub fn parse_command(v: &Value) -> Result<Command, ProtoError> {
    let o = v.as_object().ok_or_else(|| invalid("cmd must be an object".into()))?;
    let ty = o
        .get("type")
        .and_then(Value::as_str)
        .ok_or_else(|| invalid("cmd.type must be a string".into()))?;
    let apply = |c: EngineCmd| Ok(Command::Apply(c));
    match ty {
        "load" => {
            let deck = deck_of(o, ty)?;
            if o.contains_key("stable_id") && !o.contains_key("path") {
                return Err(invalid(
                    "load needs a file path; the supervisor resolves stable_id to a path before sending".into(),
                ));
            }
            let path = string(o, ty, "path")?.to_string();
            let bpm = opt_num(o, ty, "bpm")?;
            if bpm.is_some_and(|b| b <= 0.0) {
                return Err(invalid("load.bpm must be positive".into()));
            }
            Ok(Command::Load(LoadSpec { deck, path, beats: beats(o, ty)?, bpm }))
        }
        "unload" => apply(EngineCmd::Unload { deck: deck_of(o, ty)? }),
        "play" => {
            let deck = deck_of(o, ty)?;
            let playing = boolean(o, ty, "playing")?;
            // The page arms a quantized or scheduled launch from these; playing
            // at once instead would start audio off the grid, so refuse them.
            if o.get("quantize").is_some_and(|q| q != &Value::Bool(false)) {
                return Err(ProtoError::new(
                    ErrorCode::NotImplemented,
                    "play.quantize: quantized launch is not scheduled by this engine yet; send play without it",
                ));
            }
            if o.get("start_at_context_sec").is_some_and(|t| !t.is_null()) {
                return Err(ProtoError::new(
                    ErrorCode::NotImplemented,
                    "play.start_at_context_sec: scheduled launch is not implemented by this engine yet",
                ));
            }
            apply(EngineCmd::Play { deck, playing })
        }
        "cue" => apply(EngineCmd::Cue { deck: deck_of(o, ty)? }),
        "seek" => apply(EngineCmd::Seek { deck: deck_of(o, ty)?, position_ms: num(o, ty, "position_ms")? }),
        "loop" => {
            let deck = deck_of(o, ty)?;
            let bounds = match field(o, ty, "loop")? {
                Value::Null => None,
                Value::Object(l) => Some((num(l, "loop.loop", "in_ms")?, num(l, "loop.loop", "out_ms")?)),
                _ => return Err(invalid("loop.loop must be {in_ms, out_ms} or null".into())),
            };
            apply(EngineCmd::Loop { deck, bounds_ms: bounds })
        }
        "beat_loop" => apply(EngineCmd::BeatLoop {
            deck: deck_of(o, ty)?,
            beats: beat_count(o, ty)?,
            start_ms: opt_num(o, ty, "start_ms")?,
        }),
        "beat_jump" => apply(EngineCmd::BeatJump { deck: deck_of(o, ty)?, beats: beat_count(o, ty)? }),
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
        "master_mute" => apply(EngineCmd::MasterMute { muted: boolean(o, ty, "muted")? }),
        "engine_advance" => {
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
        "engine_shutdown" => Ok(Command::Shutdown),
        other => {
            if let Some((_, why)) = LATER.iter().find(|(t, _)| *t == other) {
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

/// An inbound line: `{"id": ..., "cmd": {...}}`. The id is echoed on the result.
pub fn parse_line(line: &str) -> (Option<String>, Result<Command, ProtoError>) {
    let v: Value = match serde_json::from_str(line) {
        Ok(v) => v,
        Err(e) => return (None, Err(invalid(format!("not JSON: {e}")))),
    };
    let id = match v.get("id") {
        Some(Value::String(s)) => Some(s.clone()),
        Some(Value::Number(n)) => Some(n.to_string()),
        _ => None,
    };
    let Some(cmd) = v.get("cmd") else {
        return (id, Err(invalid("message needs a cmd object".into())));
    };
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

pub fn result_json(id: Option<&str>, res: &Result<(), ProtoError>) -> Value {
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

/// The state feed. `rate` is track milliseconds per engine millisecond, so a
/// view can extrapolate `position_ms + rate * elapsed` between messages.
/// `host_time_ns` is the sender's monotonic clock at the moment the snapshot's
/// position is heard: the device clock adds its output latency, so a view
/// extrapolating from it tracks the audio rather than the render cursor. It
/// is null on the fake clock, where wall time means nothing.
pub fn state_json(s: &Snapshot, host_time_ns: Option<u64>) -> Value {
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
            })
        })
        .collect();
    json!({
        "type": "state",
        "frame": s.frame,
        "sample_rate": s.sample_rate,
        "engine_time_ns": (s.frame as u128 * 1_000_000_000u128 / s.sample_rate as u128) as u64,
        "host_time_ns": host_time_ns,
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
    fn refusals_name_their_reason() {
        let e = cmd(json!({"type": "stem_mute", "deck": 1, "stem": "vocals", "muted": true})).unwrap_err();
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
            cmd(json!({"type": "play", "deck": 1, "playing": true, "quantize": false, "start_at_context_sec": null})),
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
        let v = state_json(&s, None);
        assert_eq!(
            v["decks"][0]["loop"],
            json!({"in_ms": 1000.0, "out_ms": 3000.0, "engaged": true, "beat_length": 4.0})
        );
        // A loop set by bounds has no beat length; no loop is null.
        assert_eq!(v["decks"][1]["loop"]["beat_length"], Value::Null);
        assert_eq!(v["decks"][2]["loop"], Value::Null);
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
        for n in [0, 5] {
            let e = cmd(json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid": [{"n": n, "time_ms": 0}]})).unwrap_err();
            assert!(e.message.contains("must be 1..4"), "n {n}: {}", e.message);
        }
    }

    #[test]
    fn lines_echo_their_id() {
        let (id, c) = parse_line(r#"{"id": "c7", "cmd": {"type": "engine_state"}}"#);
        assert_eq!(id.as_deref(), Some("c7"));
        assert!(matches!(c, Ok(Command::State)));
        let (id, c) = parse_line("not json");
        assert!(id.is_none() && c.is_err());
    }
}
