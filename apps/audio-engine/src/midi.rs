//! MIDI into the engine (plan 20-03): decode controller bytes with the page's
//! own device maps and turn them into engine commands or page events.
//!
//! This module is pure: bytes in, routed actions out, no ports, no threads,
//! no clock. `midi_in` (feature `midi`) feeds it from midir; the `midi_inject`
//! protocol command feeds it recorded bytes, on any clock, with no hardware.
//!
//! The maps are the page's, as data. `maps/device-maps.json` is generated
//! from `DEVICE_MAP_REGISTRY` by `apps/webui/frontend/scripts/export-midi-maps.mjs`
//! and embedded at compile time; `--midi-map FILE` adds maps in the same
//! shape (an onboarded map), and those win over the built-in ones, as the
//! page's 'installed' tier wins over its 'builtin' tier.
//!
//! Dispatch mirrors `webmidi.svelte.ts` `_dispatch` step for step: realtime
//! bytes ignored, the 14-bit LSB pairing checked before the binding lookup,
//! the shift layer first with an unshifted fallback, a stored MSB emitting
//! nothing, pitch bend as 14 bits with its LSB first, and every message that
//! binds nothing reported (`Routed::Unmapped`), never dropped.
//!
//! What happens to a bound action follows `action-glue.svelte.ts`: buttons
//! act on press only, continuous controls send their 0..1 value. The
//! audio-relevant actions become engine commands; the rest (hot cues, channel
//! cue, headphones, master cue, browse) are the page's and are forwarded.

use std::collections::HashMap;

use serde_json::{json, Value};

use crate::deck::MAX_BEATS;
use crate::engine::{DeckId, EngineCmd, EqBand, MAX_DECKS};

/// The page's built-in maps, exported from TypeScript. Never edited by hand.
pub const BUILTIN_MAPS_JSON: &str = include_str!("../maps/device-maps.json");

/// Most bytes one `feed` call takes (a `midi_inject` line or one midir
/// callback). A controller message is 3 bytes; this is room for a burst.
pub const MAX_FEED_BYTES: usize = 4096;

#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub enum Kind {
    Note,
    Cc,
    PitchBend,
}

impl Kind {
    fn as_str(self) -> &'static str {
        match self {
            Kind::Note => "note",
            Kind::Cc => "cc",
            Kind::PitchBend => "pitchbend",
        }
    }
}

/// One physical control's wire identity. `ch` is 1..16 as the maps write it.
#[derive(Clone, Copy, Debug, PartialEq, Eq, Hash)]
pub struct Source {
    pub ch: u8,
    pub kind: Kind,
    pub id: u8,
}

impl Source {
    pub fn to_json(self) -> Value {
        json!({"ch": self.ch, "kind": self.kind.as_str(), "id": self.id})
    }
}

/// `decode.ts` `decodeSource`: None for families outside the P0 scope.
pub fn decode_source(status: u8, data1: u8) -> Option<Source> {
    let ch = (status & 0x0f) + 1;
    match status & 0xf0 {
        0x90 | 0x80 => Some(Source { ch, kind: Kind::Note, id: data1 }),
        0xb0 => Some(Source { ch, kind: Kind::Cc, id: data1 }),
        0xe0 => Some(Source { ch, kind: Kind::PitchBend, id: 0 }),
        _ => None,
    }
}

/// `decode.ts` `decodeRelative`: two's-complement ticks, so 0x7F is -1.
pub fn decode_relative(raw: u8) -> i32 {
    if raw <= 63 {
        raw as i32
    } else {
        raw as i32 - 128
    }
}

/// `decode.ts` `combine14`.
pub fn combine14(msb: u8, lsb: u8) -> u16 {
    ((msb as u16) << 7) | lsb as u16
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ChannelTarget {
    Trim,
    Eq(EqBand),
    Fader,
    Filter,
}

/// A map binding's action, checked when the map loads.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Action {
    PlayToggle(DeckId),
    Cue(DeckId),
    BeatLoop(DeckId, f64),
    /// Not in the page's `MidiAction` union yet; a map can bind it once the
    /// contract gains `deck_beat_jump` (see 20-03-PLAN.md).
    BeatJump(DeckId, f64),
    LoopExit(DeckId),
    Channel(DeckId, ChannelTarget),
    Crossfader,
    MasterVolume,
    /// Tempo fader; `lsb_offset` pairs a 14-bit CC, None is a 7-bit fader.
    Pitch { deck: DeckId, lsb_offset: Option<u8> },
    Shift,
    /// The page's action (hot cue, channel cue, headphones, master cue,
    /// browse): forwarded as a `midi_action` event, never applied here.
    Page,
}

/// The page actions this engine forwards rather than applies.
const PAGE_ACTIONS: &[&str] =
    &["deck_hot_cue", "channel_cue", "headphone_mix", "headphone_level", "master_cue", "browse_encoder", "browse_load"];

#[derive(Clone, Debug)]
pub struct Binding {
    pub source: Source,
    pub shift: bool,
    pub invert: bool,
    pub relative: bool,
    pub action: Action,
    /// The action exactly as the map wrote it, forwarded verbatim so the
    /// page can hand it to its own `handleMidiAction`.
    pub raw: Value,
}

/// A device map as the page registers it, indexed for dispatch.
#[derive(Debug)]
pub struct DeviceMap {
    pub vendor: String,
    pub name_match: String,
    re: regex_lite::Regex,
    bindings: Vec<Binding>,
    /// (shift, source) -> binding index; `webmidi.svelte.ts` `bindingKey`.
    index: HashMap<(bool, Source), usize>,
    /// (ch, LSB controller) -> the 14-bit pitch binding it completes.
    lsb_index: HashMap<(u8, u8), usize>,
    hints: HashMap<Source, String>,
}

type Obj = serde_json::Map<String, Value>;

fn err<T>(ctx: &str, msg: impl std::fmt::Display) -> Result<T, String> {
    Err(format!("{ctx}: {msg}"))
}

fn uint(o: &Obj, ctx: &str, k: &str, lo: u64, hi: u64) -> Result<u64, String> {
    match o.get(k).and_then(Value::as_u64) {
        Some(v) if (lo..=hi).contains(&v) => Ok(v),
        _ => err(ctx, format!("{k} must be a whole number {lo}..{hi}, got {}", o.get(k).unwrap_or(&Value::Null))),
    }
}

fn flag(o: &Obj, ctx: &str, k: &str) -> Result<bool, String> {
    match o.get(k) {
        None | Some(Value::Null) => Ok(false),
        Some(Value::Bool(b)) => Ok(*b),
        Some(v) => err(ctx, format!("{k} must be true or false, got {v}")),
    }
}

fn source_of(v: &Value, ctx: &str) -> Result<Source, String> {
    let o = v.as_object().ok_or_else(|| format!("{ctx}: source must be an object"))?;
    let ch = uint(o, ctx, "ch", 1, 16)? as u8;
    let kind = match o.get("kind").and_then(Value::as_str) {
        Some("note") => Kind::Note,
        Some("cc") => Kind::Cc,
        Some("pitchbend") => Kind::PitchBend,
        other => return err(ctx, format!("source.kind must be note, cc or pitchbend, got {other:?}")),
    };
    let id = uint(o, ctx, "id", 0, 127)? as u8;
    if kind == Kind::PitchBend && id != 0 {
        return err(ctx, "a pitchbend source carries no id byte, so id must be 0");
    }
    Ok(Source { ch, kind, id })
}

fn deck_in(o: &Obj, ctx: &str) -> Result<DeckId, String> {
    Ok(uint(o, ctx, "deck", 1, MAX_DECKS as u64)? as DeckId)
}

fn beats_in(o: &Obj, ctx: &str) -> Result<f64, String> {
    match o.get("beats").and_then(Value::as_f64) {
        Some(b) if b.is_finite() && b != 0.0 && b.abs() <= MAX_BEATS => Ok(b),
        _ => err(ctx, format!("beats must be a non-zero number within {MAX_BEATS}")),
    }
}

fn action_of(v: &Value, ctx: &str) -> Result<Action, String> {
    let o = v.as_object().ok_or_else(|| format!("{ctx}: action must be an object"))?;
    let ty = o.get("type").and_then(Value::as_str).ok_or_else(|| format!("{ctx}: action.type must be a string"))?;
    let ctx = &format!("{ctx} ({ty})");
    Ok(match ty {
        "deck_play_toggle" => Action::PlayToggle(deck_in(o, ctx)?),
        "deck_cue" => Action::Cue(deck_in(o, ctx)?),
        "deck_beat_loop" => Action::BeatLoop(deck_in(o, ctx)?, beats_in(o, ctx)?),
        "deck_beat_jump" => Action::BeatJump(deck_in(o, ctx)?, beats_in(o, ctx)?),
        "deck_loop_exit" => Action::LoopExit(deck_in(o, ctx)?),
        "mixer_channel" => {
            let deck = deck_in(o, ctx)?;
            let band = o.get("band").filter(|b| !b.is_null());
            let target = match o.get("target").and_then(Value::as_str) {
                Some("eq") => {
                    let b = band.and_then(Value::as_str).and_then(EqBand::parse);
                    ChannelTarget::Eq(b.ok_or_else(|| format!("{ctx}: an eq target needs band low, mid or high"))?)
                }
                Some(t @ ("trim" | "fader" | "filter")) => {
                    if band.is_some() {
                        return err(ctx, format!("band is only for the eq target, not {t}"));
                    }
                    match t {
                        "trim" => ChannelTarget::Trim,
                        "fader" => ChannelTarget::Fader,
                        _ => ChannelTarget::Filter,
                    }
                }
                other => return err(ctx, format!("target must be trim, eq, fader or filter, got {other:?}")),
            };
            Action::Channel(deck, target)
        }
        "mixer_global" => match o.get("target").and_then(Value::as_str) {
            Some("crossfader") => Action::Crossfader,
            Some("master") => Action::MasterVolume,
            other => return err(ctx, format!("target must be crossfader or master, got {other:?}")),
        },
        "deck_pitch" => {
            let deck = deck_in(o, ctx)?;
            let lsb_offset = match o.get("lsbOffset") {
                Some(Value::Null) => None,
                Some(_) => Some(uint(o, ctx, "lsbOffset", 1, 127)? as u8),
                None => return err(ctx, "lsbOffset is required (null for a 7-bit fader)"),
            };
            Action::Pitch { deck, lsb_offset }
        }
        "shift_modifier" => Action::Shift,
        t if PAGE_ACTIONS.contains(&t) => Action::Page,
        other => return err(ctx, format!("unknown action type {other}; the map is newer than this engine")),
    })
}

impl DeviceMap {
    /// Load one map (a `DeviceMap` object) and check it the way
    /// `registerDeviceMap` does, failing on the first problem.
    pub fn from_json(v: &Value) -> Result<DeviceMap, String> {
        let o = v.as_object().ok_or("a device map must be an object")?;
        let vendor = o.get("vendor").and_then(Value::as_str).ok_or("a device map needs a vendor")?.to_string();
        let name_match = o
            .get("nameMatch")
            .and_then(Value::as_str)
            .filter(|s| !s.is_empty())
            .ok_or_else(|| format!("device map {vendor}: nameMatch must be a non-empty string"))?
            .to_string();
        let re = regex_lite::Regex::new(&format!("(?i){name_match}"))
            .map_err(|e| format!("device map {vendor}: nameMatch {name_match:?} is not a valid pattern: {e}"))?;
        let arr = o
            .get("bindings")
            .and_then(Value::as_array)
            .ok_or_else(|| format!("device map {vendor}: bindings must be an array"))?;
        let mut map = DeviceMap {
            vendor,
            name_match,
            re,
            bindings: Vec::with_capacity(arr.len()),
            index: HashMap::new(),
            lsb_index: HashMap::new(),
            hints: HashMap::new(),
        };
        for (i, b) in arr.iter().enumerate() {
            let ctx = format!("device map {} binding {i}", map.vendor);
            let bo = b.as_object().ok_or_else(|| format!("{ctx}: must be an object"))?;
            let source = source_of(bo.get("source").unwrap_or(&Value::Null), &ctx)?;
            let raw = bo.get("action").cloned().unwrap_or(Value::Null);
            let action = action_of(&raw, &ctx)?;
            let binding = Binding {
                source,
                shift: flag(bo, &ctx, "shift")?,
                invert: flag(bo, &ctx, "invert")?,
                relative: flag(bo, &ctx, "relative")?,
                action,
                raw,
            };
            if action == Action::Shift && source.kind != Kind::Note {
                return err(&ctx, "shift_modifier must be bound to a note (button) source");
            }
            if map.index.insert((binding.shift, source), i).is_some() {
                return err(&ctx, format!("duplicate binding for {source:?} (shift {})", binding.shift));
            }
            if let Action::Pitch { lsb_offset: Some(off), .. } = action {
                if source.kind != Kind::Cc {
                    return err(&ctx, "a 14-bit deck_pitch must bind a cc source");
                }
                let lsb = source.id as u16 + off as u16;
                if lsb > 127 {
                    return err(&ctx, format!("LSB controller {lsb} is past 127"));
                }
                map.lsb_index.insert((source.ch, lsb as u8), i);
            }
            map.bindings.push(binding);
        }
        if let Some(hints) = o.get("hints").and_then(Value::as_array) {
            for (i, h) in hints.iter().enumerate() {
                let ctx = format!("device map {} hint {i}", map.vendor);
                let src = source_of(h.get("source").unwrap_or(&Value::Null), &ctx)?;
                let label = h.get("label").and_then(Value::as_str).unwrap_or_default().to_string();
                map.hints.insert(src, label);
            }
        }
        Ok(map)
    }

    pub fn matches(&self, port_name: &str) -> bool {
        self.re.is_match(port_name)
    }

    fn lookup(&self, shift_held: bool, src: Source) -> Option<&Binding> {
        let shifted = if shift_held { self.index.get(&(true, src)) } else { None };
        shifted.or_else(|| self.index.get(&(false, src))).map(|&i| &self.bindings[i])
    }
}

/// Every map the engine knows, in precedence order: onboarded maps first,
/// then the built-in export. First match wins, as `_resolveMap` does.
#[derive(Debug, Default)]
pub struct MapSet {
    installed: Vec<DeviceMap>,
    builtin: Vec<DeviceMap>,
}

fn maps_of(v: &Value, what: &str) -> Result<Vec<DeviceMap>, String> {
    // Either the export document ({"maps": [...]}), a bare array, or one map.
    let list: Vec<&Value> = match v {
        Value::Array(a) => a.iter().collect(),
        Value::Object(o) if o.contains_key("maps") => {
            o["maps"].as_array().ok_or_else(|| format!("{what}: maps must be an array"))?.iter().collect()
        }
        Value::Object(_) => vec![v],
        _ => return Err(format!("{what}: expected a device map, an array of them, or {{\"maps\": [...]}}")),
    };
    let maps = list.into_iter().map(DeviceMap::from_json).collect::<Result<Vec<_>, _>>()?;
    let mut seen: Vec<&str> = Vec::new();
    for m in &maps {
        if seen.contains(&m.name_match.as_str()) {
            return Err(format!("{what}: two maps for nameMatch {:?} in one tier", m.name_match));
        }
        seen.push(&m.name_match);
    }
    Ok(maps)
}

impl MapSet {
    /// The page's built-in maps.
    pub fn builtin() -> Result<MapSet, String> {
        let v: Value = serde_json::from_str(BUILTIN_MAPS_JSON).map_err(|e| format!("maps/device-maps.json: {e}"))?;
        let builtin = maps_of(&v, "maps/device-maps.json")?;
        if builtin.is_empty() {
            return Err("maps/device-maps.json holds no maps".into());
        }
        Ok(MapSet { installed: Vec::new(), builtin })
    }

    /// Add onboarded maps (the page's 'installed' tier), which win over the
    /// built-in map for the same controller.
    pub fn install_json(&mut self, v: &Value, what: &str) -> Result<(), String> {
        let maps = maps_of(v, what)?;
        for m in &maps {
            if self.installed.iter().any(|x| x.name_match == m.name_match) {
                return Err(format!("{what}: an installed map for {:?} is already loaded", m.name_match));
            }
        }
        self.installed.extend(maps);
        Ok(())
    }

    /// Which map a port name resolves to, and its index for `Router`.
    pub fn resolve(&self, port_name: &str) -> Option<&DeviceMap> {
        self.installed.iter().chain(self.builtin.iter()).find(|m| m.matches(port_name))
    }

    pub fn len(&self) -> usize {
        self.installed.len() + self.builtin.len()
    }

    pub fn is_empty(&self) -> bool {
        self.len() == 0
    }
}

/// The payload that reaches a handler, as the page's `MidiInputValue`.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum Input {
    Button { pressed: bool, velocity: u8 },
    Continuous { value01: f64, raw: u8 },
    Continuous14 { value01: f64, raw: u16 },
    Relative { delta: i32 },
}

impl Input {
    pub fn to_json(self) -> Value {
        match self {
            Input::Button { pressed, velocity } => json!({"kind": "button", "pressed": pressed, "velocity": velocity}),
            Input::Continuous { value01, raw } => json!({"kind": "continuous", "value01": value01, "raw": raw}),
            Input::Continuous14 { value01, raw } => json!({"kind": "continuous14", "value01": value01, "raw": raw}),
            Input::Relative { delta } => json!({"kind": "relative", "delta": delta}),
        }
    }

    fn value01(self) -> Option<f64> {
        match self {
            Input::Continuous { value01, .. } | Input::Continuous14 { value01, .. } => Some(value01),
            _ => None,
        }
    }
}

/// What one complete MIDI message came to.
#[derive(Clone, Debug)]
pub enum Routed {
    /// An audio action: apply it to the engine.
    Engine(EngineCmd),
    /// The page's action: forward it as a `midi_action` line.
    Page { action: Value, value: Input },
    /// Nothing bound it, or a bound action could not take this input. The
    /// page's learn log records these; so does the `midi_unmapped` line.
    Unmapped { status: u8, data1: u8, data2: u8, decoded: Option<Source>, note: String, hint: Option<String> },
}

/// Per-port stream state: running status and a message in progress, so bytes
/// may arrive in any grouping (midir hands over whole messages; a recorded
/// stream or a CoreMIDI packet may hold several, or use running status).
#[derive(Debug, Default)]
struct Port {
    map: Option<usize>,
    /// Stored tempo MSBs, keyed (ch, MSB controller).
    msb: HashMap<(u8, u8), u8>,
    status: Option<u8>,
    data: [u8; 2],
    have: usize,
    in_sysex: bool,
}

/// Data bytes a status byte takes.
fn data_len(status: u8) -> usize {
    match status {
        0xc0..=0xdf | 0xf1 | 0xf3 => 1,
        0xf6 => 0,
        _ => 2,
    }
}

/// Decodes bytes from any number of ports into routed actions.
#[derive(Debug)]
pub struct Router {
    maps: MapSet,
    /// Resolved maps by position: installed first, then built-in.
    ports: HashMap<String, Port>,
    /// One shift layer across devices, as the page's `midiState.shiftHeld`.
    shift_held: bool,
}

impl Router {
    pub fn new(maps: MapSet) -> Router {
        Router { maps, ports: HashMap::new(), shift_held: false }
    }

    pub fn maps(&self) -> &MapSet {
        &self.maps
    }

    fn map_at(&self, i: usize) -> &DeviceMap {
        let n = self.maps.installed.len();
        if i < n {
            &self.maps.installed[i]
        } else {
            &self.maps.builtin[i - n]
        }
    }

    fn resolve_index(&self, port: &str) -> Option<usize> {
        self.maps.installed.iter().chain(self.maps.builtin.iter()).position(|m| m.matches(port))
    }

    /// Feed bytes that arrived on `port`, appending what they route to.
    pub fn feed(&mut self, port: &str, bytes: &[u8], out: &mut Vec<Routed>) {
        if !self.ports.contains_key(port) {
            let map = self.resolve_index(port);
            self.ports.insert(port.to_string(), Port { map, ..Port::default() });
        }
        for &b in bytes {
            let p = self.ports.get_mut(port).expect("inserted above");
            if b >= 0xf8 {
                // Realtime (clock, active sensing): noise, not logged, as the page.
                continue;
            }
            if b == 0xf0 {
                p.in_sysex = true;
                p.status = None;
                continue;
            }
            if b == 0xf7 {
                if p.in_sysex {
                    p.in_sysex = false;
                    out.push(unmapped(0xf0, 0, 0, None, "undecoded status family (out of P0 scope)"));
                }
                continue;
            }
            if b >= 0x80 {
                p.in_sysex = false;
                p.status = Some(b);
                p.have = 0;
                if data_len(b) == 0 {
                    p.status = None;
                    out.push(unmapped(b, 0, 0, None, "undecoded status family (out of P0 scope)"));
                }
                continue;
            }
            if p.in_sysex {
                continue;
            }
            let Some(status) = p.status else {
                out.push(unmapped(0, b, 0, None, "data byte with no status byte before it"));
                continue;
            };
            p.data[p.have] = b;
            p.have += 1;
            if p.have < data_len(status) {
                continue;
            }
            p.have = 0;
            let (d1, d2) = (p.data[0], if data_len(status) == 2 { p.data[1] } else { 0 });
            if status >= 0xf0 {
                // System common cancels running status.
                p.status = None;
            }
            self.message(port, status, d1, d2, out);
        }
    }

    /// One complete message: `webmidi.svelte.ts` `_dispatch`.
    fn message(&mut self, port: &str, status: u8, d1: u8, d2: u8, out: &mut Vec<Routed>) {
        let Some(src) = decode_source(status, d1) else {
            out.push(unmapped(status, d1, d2, None, "undecoded status family (out of P0 scope)"));
            return;
        };
        let Some(mi) = self.ports[port].map else {
            out.push(unmapped(status, d1, d2, Some(src), "no device map matched this port name"));
            return;
        };
        if src.kind == Kind::Cc {
            let pair = self.map_at(mi).lsb_index.get(&(src.ch, src.id)).copied();
            if let Some(bi) = pair {
                let b = self.map_at(mi).bindings[bi].clone();
                let Some(&msb) = self.ports[port].msb.get(&(b.source.ch, b.source.id)) else {
                    out.push(unmapped(status, d1, d2, Some(src), "pitch LSB arrived before any MSB - dropped pair"));
                    return;
                };
                let raw = combine14(msb, d2);
                let v = raw as f64 / 16383.0;
                let value01 = if b.invert { 1.0 - v } else { v };
                self.emit(&b, Input::Continuous14 { value01, raw }, (status, d1, d2, src), out);
                return;
            }
        }
        let map = self.map_at(mi);
        let Some(b) = map.lookup(self.shift_held, src).cloned() else {
            let hint = map.hints.get(&src).cloned();
            out.push(Routed::Unmapped { status, data1: d1, data2: d2, decoded: Some(src), note: "unmapped source".into(), hint });
            return;
        };
        if let Action::Pitch { lsb_offset: Some(_), .. } = b.action {
            if src.kind == Kind::Cc {
                self.ports.get_mut(port).expect("seen").msb.insert((src.ch, src.id), d2);
                return;
            }
        }
        let value = match src.kind {
            Kind::PitchBend => {
                // Pitch bend's wire order is LSB first.
                let raw = combine14(d2, d1);
                let v = raw as f64 / 16383.0;
                Input::Continuous14 { value01: if b.invert { 1.0 - v } else { v }, raw }
            }
            Kind::Note => Input::Button { pressed: status & 0xf0 == 0x90 && d2 > 0, velocity: d2 },
            Kind::Cc if b.relative => Input::Relative { delta: decode_relative(d2) },
            Kind::Cc => {
                let v = d2 as f64 / 127.0;
                Input::Continuous { value01: if b.invert { 1.0 - v } else { v }, raw: d2 }
            }
        };
        self.emit(&b, value, (status, d1, d2, src), out);
    }

    /// A bound action with its input: `action-glue.svelte.ts` `handleMidiAction`
    /// for the audio actions, forwarding for the page's.
    fn emit(&mut self, b: &Binding, value: Input, wire: (u8, u8, u8, Source), out: &mut Vec<Routed>) {
        let (status, d1, d2, src) = wire;
        let refuse = |what: &str| unmapped(status, d1, d2, Some(src), &format!("handler error: {what}"));
        let press = match value {
            Input::Button { pressed, .. } => Some(pressed),
            _ => None,
        };
        let cmd = match b.action {
            Action::Shift => {
                // Validated at load: a shift binding is always a note.
                self.shift_held = press == Some(true);
                return;
            }
            Action::Page => {
                out.push(Routed::Page { action: b.raw.clone(), value });
                return;
            }
            Action::PlayToggle(_) | Action::Cue(_) | Action::BeatLoop(..) | Action::BeatJump(..) | Action::LoopExit(_) => {
                match press {
                    None => return out.push(refuse("a button action got a continuous input")),
                    // Buttons act on press; the release is ignored by design.
                    Some(false) => return,
                    Some(true) => match b.action {
                        Action::PlayToggle(deck) => EngineCmd::PlayToggle { deck },
                        Action::Cue(deck) => EngineCmd::Cue { deck },
                        Action::BeatLoop(deck, beats) => EngineCmd::BeatLoop { deck, beats, start_ms: None },
                        Action::BeatJump(deck, beats) => EngineCmd::BeatJump { deck, beats },
                        Action::LoopExit(deck) => EngineCmd::Loop { deck, bounds_ms: None },
                        _ => unreachable!("matched above"),
                    },
                }
            }
            Action::Channel(..) | Action::Crossfader | Action::MasterVolume | Action::Pitch { .. } => {
                let Some(value) = value.value01() else {
                    return out.push(refuse("a continuous action got a button or relative input"));
                };
                match b.action {
                    Action::Channel(deck, ChannelTarget::Trim) => EngineCmd::Trim { deck, value },
                    Action::Channel(deck, ChannelTarget::Eq(band)) => EngineCmd::Eq { deck, band, value },
                    Action::Channel(deck, ChannelTarget::Fader) => EngineCmd::Fader { deck, value },
                    Action::Channel(deck, ChannelTarget::Filter) => EngineCmd::Filter { deck, value },
                    Action::Crossfader => EngineCmd::Crossfader { value },
                    Action::MasterVolume => EngineCmd::MasterVolume { value },
                    Action::Pitch { deck, .. } => EngineCmd::TempoFader { deck, value },
                    _ => unreachable!("matched above"),
                }
            }
        };
        out.push(Routed::Engine(cmd));
    }

    /// The map a port resolves to, for reporting which ports are claimed.
    pub fn map_for(&self, port: &str) -> Option<&DeviceMap> {
        self.maps.resolve(port)
    }
}

fn unmapped(status: u8, data1: u8, data2: u8, decoded: Option<Source>, note: &str) -> Routed {
    Routed::Unmapped { status, data1, data2, decoded, note: note.to_string(), hint: None }
}

/// The `midi_action` line: a page action with its input, verbatim, so the
/// page can run it through its own `handleMidiAction(action, value)`.
pub fn action_json(port: &str, action: &Value, value: Input) -> Value {
    json!({"type": "midi_action", "port": port, "action": action, "value": value.to_json()})
}

/// The `midi_unmapped` line, shaped like the page's `LearnLogEntry`.
pub fn unmapped_json(port: &str, r: &Routed) -> Option<Value> {
    let Routed::Unmapped { status, data1, data2, decoded, note, hint } = r else { return None };
    Some(json!({
        "type": "midi_unmapped",
        "port": port,
        "status": status,
        "data1": data1,
        "data2": data2,
        "decoded": decoded.map(Source::to_json),
        "note": note,
        "hint": hint,
    }))
}

/// The `midi_ports` line: which input ports this engine opened, and which it
/// left alone because no map matches them.
pub fn ports_json(claimed: &[(String, String)], unclaimed: &[String]) -> Value {
    let claimed: Vec<Value> = claimed.iter().map(|(p, m)| json!({"port": p, "map": m})).collect();
    json!({"type": "midi_ports", "claimed": claimed, "unclaimed": unclaimed})
}

#[cfg(test)]
mod tests {
    use super::*;

    const FLX4: &str = "Pioneer DJ DDJ-FLX4 MIDI 1";

    fn router() -> Router {
        Router::new(MapSet::builtin().unwrap())
    }

    fn feed(r: &mut Router, port: &str, bytes: &[u8]) -> Vec<Routed> {
        let mut out = Vec::new();
        r.feed(port, bytes, &mut out);
        out
    }

    /// Routed holds EngineCmd, which is not PartialEq (a Load carries a
    /// track); what MIDI produces compares exactly through Debug.
    fn same(got: Vec<Routed>, want: Vec<Routed>) {
        assert_eq!(format!("{got:?}"), format!("{want:?}"));
    }

    fn engine(cmd: EngineCmd) -> Vec<Routed> {
        vec![Routed::Engine(cmd)]
    }

    fn note(r: &Routed) -> &str {
        match r {
            Routed::Unmapped { note, .. } => note,
            other => panic!("expected unmapped, got {other:?}"),
        }
    }

    #[test]
    fn the_builtin_export_holds_every_page_map() {
        let m = MapSet::builtin().unwrap();
        let names: Vec<&str> = m.builtin.iter().map(|m| m.name_match.as_str()).collect();
        assert_eq!(names, ["DDJ-FLX10", "DDJ-400", "Mixtour", "DDJ-FLX4"]);
        // Every binding in the export loaded (none silently skipped).
        let v: Value = serde_json::from_str(BUILTIN_MAPS_JSON).unwrap();
        for (map, raw) in m.builtin.iter().zip(v["maps"].as_array().unwrap()) {
            assert_eq!(map.bindings.len(), raw["bindings"].as_array().unwrap().len(), "{}", map.vendor);
            assert!(!map.bindings.is_empty());
        }
    }

    #[test]
    fn ports_resolve_by_name_case_insensitively_and_flx4_is_not_flx10() {
        let m = MapSet::builtin().unwrap();
        assert_eq!(m.resolve(FLX4).unwrap().name_match, "DDJ-FLX4");
        assert_eq!(m.resolve("ddj-flx10").unwrap().name_match, "DDJ-FLX10");
        assert_eq!(m.resolve("Reloop Mixtour Pro").unwrap().name_match, "Mixtour");
        assert!(m.resolve("IAC Driver Bus 1").is_none());
    }

    // Recorded FLX4 wire bytes, from the [PDF] rows the TS map cites
    // (and the Fri 18 Sep 2026 hardware sniff for CFX: B6 17 xx).

    #[test]
    fn play_and_cue_act_on_press_and_ignore_release() {
        let mut r = router();
        same(feed(&mut r, FLX4, &[0x90, 0x0b, 0x7f]), engine(EngineCmd::PlayToggle { deck: 1 }));
        // Release as Note On velocity 0, and as Note Off: nothing.
        same(feed(&mut r, FLX4, &[0x90, 0x0b, 0x00]), vec![]);
        same(feed(&mut r, FLX4, &[0x80, 0x0b, 0x40]), vec![]);
        same(feed(&mut r, FLX4, &[0x91, 0x0c, 0x7f]), engine(EngineCmd::Cue { deck: 2 }));
    }

    #[test]
    fn mixer_controls_send_their_value() {
        let mut r = router();
        // Deck 1 channel fader CC 19 at 127 and 0 (the ends are exact).
        same(feed(&mut r, FLX4, &[0xb0, 0x13, 0x7f]), engine(EngineCmd::Fader { deck: 1, value: 1.0 }));
        same(feed(&mut r, FLX4, &[0xb0, 0x13, 0x00]), engine(EngineCmd::Fader { deck: 1, value: 0.0 }));
        same(feed(&mut r, FLX4, &[0xb1, 0x0f, 64]), engine(EngineCmd::Eq { deck: 2, band: EqBand::Low, value: 64.0 / 127.0 }));
        same(feed(&mut r, FLX4, &[0xb0, 0x04, 10]), engine(EngineCmd::Trim { deck: 1, value: 10.0 / 127.0 }));
        // CFX lives on the global channel 7 (0xB6), not the deck channel.
        same(feed(&mut r, FLX4, &[0xb6, 0x17, 0x37]), engine(EngineCmd::Filter { deck: 1, value: 55.0 / 127.0 }));
        assert_eq!(note(&feed(&mut r, FLX4, &[0xb0, 0x17, 0x37])[0]), "unmapped source");
        same(feed(&mut r, FLX4, &[0xb6, 0x1f, 0x7f]), engine(EngineCmd::Crossfader { value: 1.0 }));
        same(feed(&mut r, FLX4, &[0xb6, 0x08, 0x00]), engine(EngineCmd::MasterVolume { value: 0.0 }));
    }

    #[test]
    fn the_tempo_fader_pairs_msb_then_lsb_into_14_bits() {
        let mut r = router();
        // LSB before any MSB: dropped, and said so.
        assert_eq!(note(&feed(&mut r, FLX4, &[0xb0, 0x20, 0x00])[0]), "pitch LSB arrived before any MSB - dropped pair");
        // The MSB alone emits nothing; the LSB completes the pair.
        same(feed(&mut r, FLX4, &[0xb0, 0x00, 0x40]), vec![]);
        same(feed(&mut r, FLX4, &[0xb0, 0x20, 0x00]), engine(EngineCmd::TempoFader { deck: 1, value: 8192.0 / 16383.0 }));
        // Top of the fader is exactly 1; deck 2's pair is its own.
        same(feed(&mut r, FLX4, &[0xb1, 0x00, 0x7f, 0xb1, 0x20, 0x7f]), engine(EngineCmd::TempoFader { deck: 2, value: 1.0 }));
        // A second LSB reuses the stored MSB, as the page does.
        same(feed(&mut r, FLX4, &[0xb0, 0x20, 0x7f]), engine(EngineCmd::TempoFader { deck: 1, value: (0x40 * 128 + 0x7f) as f64 / 16383.0 }));
    }

    #[test]
    fn running_status_and_split_messages_decode_like_whole_ones() {
        let mut whole = router();
        let mut want = Vec::new();
        for v in [10u8, 20, 30] {
            whole.feed(FLX4, &[0xb0, 0x13, v], &mut want);
        }
        // Running status: one status byte, three data pairs.
        let mut r = router();
        same(feed(&mut r, FLX4, &[0xb0, 0x13, 10, 0x13, 20, 0x13, 30]), want.clone());
        // The same bytes one at a time, with a clock tick in the middle.
        let mut r = router();
        let mut got = Vec::new();
        for b in [0xb0, 0x13, 0xf8, 10, 0x13, 20, 0x13, 30] {
            r.feed(FLX4, &[b], &mut got);
        }
        same(got, want);
        // Control: a data byte with no status before it is reported, not guessed.
        let mut r = router();
        assert_eq!(note(&feed(&mut r, FLX4, &[0x13, 10])[0]), "data byte with no status byte before it");
    }

    #[test]
    fn page_actions_are_forwarded_verbatim_with_their_input() {
        let mut r = router();
        // Hot cue pad A on deck 1 (ch 8, note 0).
        let got = feed(&mut r, FLX4, &[0x97, 0x00, 0x7f]);
        same(
            got,
            vec![Routed::Page {
                action: json!({"type": "deck_hot_cue", "deck": 1, "slot": "A"}),
                value: Input::Button { pressed: true, velocity: 0x7f }
            }]
        );
        // The release is forwarded too: the page decides (master_cue hold needs it).
        assert_eq!(feed(&mut r, FLX4, &[0x97, 0x00, 0x00]).len(), 1);
        // Browse encoder: relative, two's complement.
        let got = feed(&mut r, FLX4, &[0xb6, 0x40, 0x7f]);
        same(got, vec![Routed::Page { action: json!({"type": "browse_encoder"}), value: Input::Relative { delta: -1 } }]);
        let line = action_json(FLX4, &json!({"type": "browse_encoder"}), Input::Relative { delta: -1 });
        assert_eq!(line, json!({"type": "midi_action", "port": FLX4, "action": {"type": "browse_encoder"}, "value": {"kind": "relative", "delta": -1}}));
    }

    #[test]
    fn unbound_traffic_is_reported_with_the_maps_hint() {
        let mut r = router();
        // FLX4 deck 1 jog touch (note 0x36) is hinted, not bound.
        let got = feed(&mut r, FLX4, &[0x90, 0x36, 0x7f]);
        let Routed::Unmapped { hint, note: why, decoded, .. } = &got[0] else { panic!("{got:?}") };
        assert_eq!(why, "unmapped source");
        assert_eq!(hint.as_deref(), Some("JOG touch (deck 1)"));
        assert_eq!(*decoded, Some(Source { ch: 1, kind: Kind::Note, id: 0x36 }));
        let line = unmapped_json(FLX4, &got[0]).unwrap();
        assert_eq!(line["decoded"], json!({"ch": 1, "kind": "note", "id": 0x36}));
        // A port with no map, a program change, and sysex are each reported.
        assert_eq!(note(&feed(&mut r, "Some Keyboard", &[0x90, 0x3c, 0x40])[0]), "no device map matched this port name");
        assert_eq!(note(&feed(&mut r, FLX4, &[0xc0, 0x05])[0]), "undecoded status family (out of P0 scope)");
        assert_eq!(feed(&mut r, FLX4, &[0xf0, 0x00, 0x40, 0x05, 0xf7]).len(), 1);
    }

    #[test]
    fn beat_loop_and_loop_exit_pads() {
        let mut r = router();
        // Deck 2 beat-loop pad 5 (ch 10, note 0x64) is a 4-beat loop.
        same(feed(&mut r, FLX4, &[0x99, 0x64, 0x7f]), engine(EngineCmd::BeatLoop { deck: 2, beats: 4.0, start_ms: None }));
        // 4 BEAT / EXIT (note 0x4D).
        same(feed(&mut r, FLX4, &[0x90, 0x4d, 0x7f]), engine(EngineCmd::Loop { deck: 1, bounds_ms: None }));
    }

    fn custom(bindings: Value) -> Router {
        let mut m = MapSet::builtin().unwrap();
        m.install_json(&json!({"vendor": "Test", "nameMatch": "DDJ-FLX4", "bindings": bindings}), "test").unwrap();
        Router::new(m)
    }

    #[test]
    fn an_installed_map_wins_over_the_builtin_one() {
        let mut r = custom(json!([{"source": {"ch": 1, "kind": "note", "id": 0x0b}, "action": {"type": "deck_cue", "deck": 3}}]));
        same(feed(&mut r, FLX4, &[0x90, 0x0b, 0x7f]), engine(EngineCmd::Cue { deck: 3 }));
        // Control: a port only the builtin tier matches still uses it.
        same(feed(&mut r, "DDJ-400", &[0x90, 0x0b, 0x7f]), engine(EngineCmd::PlayToggle { deck: 1 }));
    }

    #[test]
    fn the_shift_layer_wins_while_held_and_falls_back_when_not() {
        let mut r = custom(json!([
            {"source": {"ch": 1, "kind": "note", "id": 0x3f}, "action": {"type": "shift_modifier"}},
            {"source": {"ch": 1, "kind": "note", "id": 0x10}, "action": {"type": "deck_beat_jump", "deck": 1, "beats": 4}},
            {"source": {"ch": 1, "kind": "note", "id": 0x10}, "shift": true, "action": {"type": "deck_beat_jump", "deck": 1, "beats": -4}},
            {"source": {"ch": 1, "kind": "note", "id": 0x11}, "action": {"type": "deck_cue", "deck": 1}}
        ]));
        let jump = |beats| engine(EngineCmd::BeatJump { deck: 1, beats });
        same(feed(&mut r, FLX4, &[0x90, 0x10, 0x7f]), jump(4.0));
        same(feed(&mut r, FLX4, &[0x90, 0x3f, 0x7f]), vec![]);
        same(feed(&mut r, FLX4, &[0x90, 0x10, 0x7f]), jump(-4.0));
        // No shifted twin: the unshifted binding still fires while held.
        same(feed(&mut r, FLX4, &[0x90, 0x11, 0x7f]), engine(EngineCmd::Cue { deck: 1 }));
        // Released: back to the unshifted layer.
        same(feed(&mut r, FLX4, &[0x90, 0x3f, 0x00]), vec![]);
        same(feed(&mut r, FLX4, &[0x90, 0x10, 0x7f]), jump(4.0));
    }

    #[test]
    fn invert_and_pitch_bend_and_seven_bit_pitch() {
        let mut r = custom(json!([
            {"source": {"ch": 1, "kind": "cc", "id": 0x13}, "invert": true, "action": {"type": "mixer_channel", "deck": 1, "target": "fader"}},
            {"source": {"ch": 2, "kind": "pitchbend", "id": 0}, "action": {"type": "deck_pitch", "deck": 2, "lsbOffset": null}},
            {"source": {"ch": 3, "kind": "cc", "id": 0x05}, "action": {"type": "deck_pitch", "deck": 3, "lsbOffset": null}}
        ]));
        same(feed(&mut r, FLX4, &[0xb0, 0x13, 0x7f]), engine(EngineCmd::Fader { deck: 1, value: 0.0 }));
        // Pitch bend: d1 is the LSB. 0x00 0x40 is 8192, the center.
        same(feed(&mut r, FLX4, &[0xe1, 0x00, 0x40]), engine(EngineCmd::TempoFader { deck: 2, value: 8192.0 / 16383.0 }));
        same(feed(&mut r, FLX4, &[0xe1, 0x7f, 0x7f]), engine(EngineCmd::TempoFader { deck: 2, value: 1.0 }));
        // A 7-bit pitch CC emits at once; nothing is stored waiting for an LSB.
        same(feed(&mut r, FLX4, &[0xb2, 0x05, 0x7f]), engine(EngineCmd::TempoFader { deck: 3, value: 1.0 }));
    }

    #[test]
    fn maps_are_refused_on_the_first_problem() {
        let bad = [
            (json!({"vendor": "X", "nameMatch": "", "bindings": []}), "nameMatch"),
            (json!({"vendor": "X", "nameMatch": "(", "bindings": []}), "not a valid pattern"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_cue", "deck": 1}},
                {"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_cue", "deck": 2}}]}), "duplicate binding"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_pitch", "deck": 1, "lsbOffset": 32}}]}), "must bind a cc"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 1, "kind": "cc", "id": 1}, "action": {"type": "mixer_channel", "deck": 1, "target": "eq"}}]}), "needs band"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 1, "kind": "cc", "id": 1}, "action": {"type": "mixer_channel", "deck": 1, "target": "fader", "band": "low"}}]}), "only for the eq"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_cue", "deck": 5}}]}), "deck must be"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 17, "kind": "note", "id": 1}, "action": {"type": "deck_cue", "deck": 1}}]}), "ch must be"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_scratch", "deck": 1}}]}), "unknown action type"),
            (json!({"vendor": "X", "nameMatch": "X", "bindings": [
                {"source": {"ch": 1, "kind": "cc", "id": 1}, "action": {"type": "shift_modifier"}}]}), "shift_modifier must be bound to a note"),
        ];
        for (map, want) in bad {
            let e = DeviceMap::from_json(&map).unwrap_err();
            assert!(e.contains(want), "{want}: {e}");
        }
        // Control: the same shapes, fixed, load.
        let ok = json!({"vendor": "X", "nameMatch": "X", "bindings": [
            {"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_cue", "deck": 1}},
            {"source": {"ch": 1, "kind": "note", "id": 1}, "shift": true, "action": {"type": "deck_cue", "deck": 2}},
            {"source": {"ch": 1, "kind": "cc", "id": 0}, "action": {"type": "deck_pitch", "deck": 1, "lsbOffset": 32}},
            {"source": {"ch": 1, "kind": "cc", "id": 1}, "action": {"type": "mixer_channel", "deck": 4, "target": "eq", "band": "mid"}}]});
        assert!(DeviceMap::from_json(&ok).is_ok());
        // Two installed maps for one controller is a wiring bug, as on the page.
        let mut m = MapSet::builtin().unwrap();
        m.install_json(&ok, "a").unwrap();
        assert!(m.install_json(&ok, "b").unwrap_err().contains("already loaded"));
    }

    #[test]
    fn a_button_bound_to_a_fader_is_reported_not_applied() {
        let mut r = custom(json!([
            {"source": {"ch": 1, "kind": "cc", "id": 0x0b}, "action": {"type": "deck_play_toggle", "deck": 1}},
            {"source": {"ch": 1, "kind": "note", "id": 0x13}, "action": {"type": "mixer_channel", "deck": 1, "target": "fader"}}
        ]));
        assert!(note(&feed(&mut r, FLX4, &[0xb0, 0x0b, 0x7f])[0]).starts_with("handler error"));
        assert!(note(&feed(&mut r, FLX4, &[0x90, 0x13, 0x7f])[0]).starts_with("handler error"));
    }

    /// Dispatch cost per message, printed, for 20-VERIFICATION.md. Run with
    /// `cargo test --release --lib midi::tests::dispatch_cost -- --ignored --nocapture`.
    #[test]
    #[ignore]
    fn dispatch_cost() {
        let mut r = router();
        let mut out = Vec::with_capacity(8);
        let n = 1_000_000u32;
        let t = std::time::Instant::now();
        for i in 0..n {
            out.clear();
            r.feed(FLX4, &[0xb0, 0x13, (i % 128) as u8], &mut out);
            std::hint::black_box(&out);
        }
        let per = t.elapsed().as_nanos() as f64 / n as f64;
        println!("midi dispatch: {per:.0} ns per fader message");
    }
}
