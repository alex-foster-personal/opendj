//! Offline plans: a timeline of commands, at absolute times or at musical
//! positions on a deck's own grid, rendered on the fake clock.
//!
//! ```json
//! {
//!   "sample_rate": 48000,
//!   "end": {"deck": 2, "bar": 33},
//!   "events": [
//!     {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.mp3", "beatgrid": [...]}},
//!     {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
//!     {"at": {"deck": 1, "bar": 97}, "cmd": {"type": "play", "deck": 2, "playing": true}},
//!     {"at": {"deck": 1, "bar": 97}, "ramp": {"type": "crossfader", "to": 1.0, "over": {"beats": 32, "deck": 1}}}
//!   ]
//! }
//! ```
//!
//! A deck-relative time fires the first time that deck's playhead is at or
//! past the position. Events due on the same frame fire in plan order.

use serde_json::{Map, Value};

use crate::engine::{DeckId, KnobTarget};
use crate::protocol::{self, deck_of, Command, ProtoError};
use crate::engine::ErrorCode;

pub const DEFAULT_SAMPLE_RATE: u32 = 48000;
pub const DEFAULT_BLOCK_FRAMES: usize = 256;
/// A plan whose end never arrives stops here with an error, not a hang.
pub const DEFAULT_MAX_MS: f64 = 4.0 * 3600.0 * 1000.0;

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum DeckPos {
    /// 1-based bar on the deck's downbeats; fractions allowed.
    Bar(f64),
    /// 0-based beat index on the deck's grid; fractions allowed.
    Beat(f64),
    /// Track time.
    Ms(f64),
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum At {
    Frame(u64),
    Ms(f64),
    Deck { deck: DeckId, pos: DeckPos },
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Over {
    Frames(u64),
    Ms(f64),
    /// Beats (or bars of four) on a deck's grid, measured from where that deck
    /// is when the ramp starts, at its tempo then.
    Beats { deck: DeckId, beats: f64 },
}

#[derive(Clone, Debug)]
pub struct Ramp {
    pub target: KnobTarget,
    pub to: f64,
    pub over: Over,
}

#[derive(Clone, Debug)]
pub enum Action {
    Cmd(Command),
    Ramp(Ramp),
}

#[derive(Clone, Debug)]
pub struct Event {
    pub at: At,
    pub action: Action,
}

#[derive(Clone, Debug)]
pub struct Plan {
    pub sample_rate: u32,
    pub block_frames: usize,
    pub max_ms: f64,
    pub end: At,
    pub events: Vec<Event>,
}

fn invalid(msg: String) -> ProtoError {
    ProtoError::new(ErrorCode::Invalid, msg)
}

fn obj<'a>(v: &'a Value, what: &str) -> Result<&'a Map<String, Value>, ProtoError> {
    v.as_object().ok_or_else(|| invalid(format!("{what} must be an object")))
}

fn finite(o: &Map<String, Value>, what: &str, key: &str) -> Result<Option<f64>, ProtoError> {
    match o.get(key) {
        None => Ok(None),
        Some(v) => v
            .as_f64()
            .filter(|x| x.is_finite())
            .map(Some)
            .ok_or_else(|| invalid(format!("{what}.{key} must be a finite number"))),
    }
}

/// Refuse a time or length that names more than one of `keys`: taking one
/// and ignoring the rest would render the plan at a place it did not mean.
fn at_most_one(o: &Map<String, Value>, what: &str, keys: &[&str]) -> Result<(), ProtoError> {
    let given: Vec<&str> = keys.iter().copied().filter(|k| o.contains_key(*k)).collect();
    if given.len() > 1 {
        return Err(invalid(format!("{what} gives {}; give exactly one", given.join(" and "))));
    }
    Ok(())
}

fn parse_at(v: &Value, what: &str) -> Result<At, ProtoError> {
    let o = obj(v, what)?;
    at_most_one(o, what, &["bar", "beat", "position_ms", "frame", "ms"])?;
    if o.contains_key("deck") {
        let deck = deck_of(o, what)?;
        // Bars count from 1, beats from 0 and track time from 0: anything
        // lower names a place before the grid or the track, which a deck's
        // playhead is always already past, so it would fire at once.
        let at_least = |x: f64, min: f64, key: &str| {
            if x >= min {
                Ok(x)
            } else {
                Err(invalid(format!("{what}.{key} must be at least {min}, got {x}")))
            }
        };
        let pos = if let Some(b) = finite(o, what, "bar")? {
            DeckPos::Bar(at_least(b, 1.0, "bar")?)
        } else if let Some(b) = finite(o, what, "beat")? {
            DeckPos::Beat(at_least(b, 0.0, "beat")?)
        } else if let Some(ms) = finite(o, what, "position_ms")? {
            DeckPos::Ms(at_least(ms, 0.0, "position_ms")?)
        } else {
            return Err(invalid(format!("{what} with a deck needs bar, beat or position_ms")));
        };
        return Ok(At::Deck { deck, pos });
    }
    if let Some(f) = o.get("frame") {
        return f.as_u64().map(At::Frame).ok_or_else(|| invalid(format!("{what}.frame must be a whole number")));
    }
    match finite(o, what, "ms")? {
        Some(ms) if ms >= 0.0 => Ok(At::Ms(ms)),
        Some(_) => Err(invalid(format!("{what}.ms must not be negative"))),
        None => Err(invalid(format!("{what} needs ms, frame, or a deck position"))),
    }
}

fn parse_over(v: &Value, what: &str) -> Result<Over, ProtoError> {
    let o = obj(v, what)?;
    at_most_one(o, what, &["beats", "bars", "ms", "frames"])?;
    let positive = |x: f64, k: &str| {
        if x > 0.0 {
            Ok(x)
        } else {
            Err(invalid(format!("{what}.{k} must be positive")))
        }
    };
    if let Some(b) = finite(o, what, "beats")? {
        return Ok(Over::Beats { deck: deck_of(o, what)?, beats: positive(b, "beats")? });
    }
    if let Some(b) = finite(o, what, "bars")? {
        return Ok(Over::Beats { deck: deck_of(o, what)?, beats: positive(b, "bars")? * 4.0 });
    }
    if let Some(ms) = finite(o, what, "ms")? {
        return Ok(Over::Ms(positive(ms, "ms")?));
    }
    if let Some(f) = o.get("frames") {
        return f
            .as_u64()
            .filter(|&f| f > 0)
            .map(Over::Frames)
            .ok_or_else(|| invalid(format!("{what}.frames must be a positive whole number")));
    }
    Err(invalid(format!("{what} needs ms, frames, beats or bars")))
}

pub fn parse_plan(v: &Value) -> Result<Plan, ProtoError> {
    let o = obj(v, "plan")?;
    let sample_rate = match o.get("sample_rate") {
        None => DEFAULT_SAMPLE_RATE,
        Some(s) => s
            .as_u64()
            .filter(|s| (8000..=384000).contains(s))
            .ok_or_else(|| invalid("plan.sample_rate must be 8000..384000".into()))? as u32,
    };
    let block_frames = match o.get("block_frames") {
        None => DEFAULT_BLOCK_FRAMES,
        Some(b) => b
            .as_u64()
            .filter(|b| (1..=crate::engine::MAX_BLOCK as u64).contains(b))
            .ok_or_else(|| invalid(format!("plan.block_frames must be 1..{}", crate::engine::MAX_BLOCK)))?
            as usize,
    };
    let max_ms = finite(o, "plan", "max_ms")?.unwrap_or(DEFAULT_MAX_MS);
    let end = parse_at(o.get("end").ok_or_else(|| invalid("plan.end is required".into()))?, "plan.end")?;
    let raw = o
        .get("events")
        .and_then(Value::as_array)
        .ok_or_else(|| invalid("plan.events must be an array".into()))?;
    let mut events = Vec::with_capacity(raw.len());
    for (i, e) in raw.iter().enumerate() {
        let what = format!("events[{i}]");
        let eo = obj(e, &what)?;
        let at = parse_at(eo.get("at").ok_or_else(|| invalid(format!("{what}.at is required")))?, &format!("{what}.at"))?;
        let action = match (eo.get("cmd"), eo.get("ramp")) {
            (Some(c), None) => {
                let cmd = protocol::parse_command(c).map_err(|e| ProtoError::new(e.code, format!("{what}: {}", e.message)))?;
                if matches!(cmd, Command::Advance(_) | Command::State | Command::Shutdown) {
                    return Err(invalid(format!("{what}: clock and session commands do not belong in a plan")));
                }
                Action::Cmd(cmd)
            }
            (None, Some(r)) => {
                let ro = obj(r, &format!("{what}.ramp"))?;
                let target = protocol::parse_knob(ro).map_err(|e| ProtoError::new(e.code, format!("{what}.ramp: {}", e.message)))?;
                let to = finite(ro, &format!("{what}.ramp"), "to")?
                    .ok_or_else(|| invalid(format!("{what}.ramp.to is required")))?;
                check_ramp_to(target, to).map_err(|m| invalid(format!("{what}.ramp.to {m}")))?;
                let over = parse_over(
                    ro.get("over").ok_or_else(|| invalid(format!("{what}.ramp.over is required")))?,
                    &format!("{what}.ramp.over"),
                )?;
                Action::Ramp(Ramp { target, to, over })
            }
            _ => return Err(invalid(format!("{what} needs exactly one of cmd or ramp"))),
        };
        events.push(Event { at, action });
    }
    Ok(Plan { sample_rate, block_frames, max_ms, end, events })
}

/// A ramp's end value must be one its knob accepts, checked when the plan is
/// read: every step of a ramp lies between where it starts (a value the knob
/// already holds) and this, so a bad endpoint would otherwise fail part-way
/// through a render. A tempo also has to fit the deck's pitch range when the
/// ramp starts; that is checked then, since the range can change in a plan.
fn check_ramp_to(target: KnobTarget, to: f64) -> Result<(), String> {
    match target {
        KnobTarget::Tempo(_) if !(to > 0.0 && to <= 2.0) => {
            Err(format!("must be a tempo ratio within 0..2 (the widest pitch range), got {to}"))
        }
        KnobTarget::Tempo(_) => Ok(()),
        _ if !(0.0..=1.0).contains(&to) => Err(format!("must be within 0..1 for this knob, got {to}")),
        _ => Ok(()),
    }
}
