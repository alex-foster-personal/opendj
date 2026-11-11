//! Control-event capture at the engine command boundary (SETREC-04).
//!
//! Every stamped event is pushed into a fixed slot ring with no heap allocation.
//! The recorder drains it off the audio thread.

use std::sync::atomic::{AtomicU64, Ordering};

use rtrb::{Consumer, Producer, RingBuffer};

use crate::engine::EngineCmd;

pub const CONTROL_RING_SLOTS: usize = 256;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControlKind {
    Set,
    Trigger,
}

impl ControlKind {
    pub fn as_str(self) -> &'static str {
        match self {
            ControlKind::Set => "set",
            ControlKind::Trigger => "trigger",
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum ControlSource {
    User,
    Controller,
    Agent,
    Replay,
}

impl ControlSource {
    pub fn as_str(self) -> &'static str {
        match self {
            ControlSource::User => "user",
            ControlSource::Controller => "controller",
            ControlSource::Agent => "agent",
            ControlSource::Replay => "replay",
        }
    }

    pub fn from_wire(s: &str) -> Option<ControlSource> {
        match s {
            "user" => Some(ControlSource::User),
            "controller" => Some(ControlSource::Controller),
            "agent" => Some(ControlSource::Agent),
            "replay" => Some(ControlSource::Replay),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub enum ControlValue {
    Bool(bool),
    Number(f64),
    Trigger,
}

#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ControlStamp {
    pub sample: u64,
    pub sample_rate: u32,
    pub control: &'static str,
    pub value: ControlValue,
    pub kind: ControlKind,
    pub source: ControlSource,
    pub deck: Option<u8>,
}

impl ControlStamp {
    pub fn to_json_line(&self, session_id: &str, seq: u64) -> String {
        let (value, kind) = match self.value {
            ControlValue::Bool(b) => (serde_json::json!(b), self.kind.as_str()),
            ControlValue::Number(n) => (serde_json::json!(n), self.kind.as_str()),
            ControlValue::Trigger => (serde_json::json!(null), "trigger"),
        };
        let deck = self.deck.map(|d| serde_json::json!(d)).unwrap_or(serde_json::json!(null));
        serde_json::json!({
            "type": "control",
            "v": 2,
            "session_id": session_id,
            "seq": seq,
            "sample": self.sample,
            "sample_rate": self.sample_rate,
            "wall_clock": "",
            "control": self.control,
            "value": value,
            "kind": kind,
            "source": self.source.as_str(),
            "deck": deck,
        })
        .to_string()
    }
}

fn id_play(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.play",
        2 => "deck.2.play",
        3 => "deck.3.play",
        _ => "deck.4.play",
    }
}

fn id_cue(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.cue",
        2 => "deck.2.cue",
        3 => "deck.3.cue",
        _ => "deck.4.cue",
    }
}

fn id_seek(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.seek_ms",
        2 => "deck.2.seek_ms",
        3 => "deck.3.seek_ms",
        _ => "deck.4.seek_ms",
    }
}

fn id_tempo(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.tempo",
        2 => "deck.2.tempo",
        3 => "deck.3.tempo",
        _ => "deck.4.tempo",
    }
}

fn id_tempo_fader(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.tempo_fader",
        2 => "deck.2.tempo_fader",
        3 => "deck.3.tempo_fader",
        _ => "deck.4.tempo_fader",
    }
}

fn id_pitch_range(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.pitch_range",
        2 => "deck.2.pitch_range",
        3 => "deck.3.pitch_range",
        _ => "deck.4.pitch_range",
    }
}

fn id_master_tempo(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.master_tempo",
        2 => "deck.2.master_tempo",
        3 => "deck.3.master_tempo",
        _ => "deck.4.master_tempo",
    }
}

fn id_key_nudge(deck: u8) -> &'static str {
    match deck {
        1 => "deck.1.key_nudge",
        2 => "deck.2.key_nudge",
        3 => "deck.3.key_nudge",
        _ => "deck.4.key_nudge",
    }
}

/// Map one applied engine command to a v2 control stamp, if it is in the transport/tempo slice.
pub fn stamp_from_cmd(cmd: &EngineCmd, sample: u64, sample_rate: u32, source: ControlSource) -> Option<ControlStamp> {
    use EngineCmd::*;
    match cmd {
        Play { deck, playing } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_play(*deck),
            value: ControlValue::Bool(*playing),
            kind: ControlKind::Set,
            source,
            deck: Some(*deck),
        }),
        Cue { deck } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_cue(*deck),
            value: ControlValue::Trigger,
            kind: ControlKind::Trigger,
            source,
            deck: Some(*deck),
        }),
        Seek { deck, position_ms, .. } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_seek(*deck),
            value: ControlValue::Number(*position_ms),
            kind: ControlKind::Set,
            source,
            deck: Some(*deck),
        }),
        Tempo { deck, ratio } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_tempo(*deck),
            value: ControlValue::Number(*ratio),
            kind: ControlKind::Set,
            source,
            deck: Some(*deck),
        }),
        TempoFader { deck, value } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_tempo_fader(*deck),
            value: ControlValue::Number(*value),
            kind: ControlKind::Set,
            source,
            deck: Some(*deck),
        }),
        PitchRange { deck, range } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_pitch_range(*deck),
            value: ControlValue::Number(*range),
            kind: ControlKind::Set,
            source,
            deck: Some(*deck),
        }),
        MasterTempo { deck, enabled } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_master_tempo(*deck),
            value: ControlValue::Bool(*enabled),
            kind: ControlKind::Set,
            source,
            deck: Some(*deck),
        }),
        KeyNudge { deck, semitones } => Some(ControlStamp {
            sample,
            sample_rate,
            control: id_key_nudge(*deck),
            value: ControlValue::Number(f64::from(*semitones)),
            kind: ControlKind::Trigger,
            source,
            deck: Some(*deck),
        }),
        _ => None,
    }
}

pub struct ControlRing {
    tx: Producer<ControlStamp>,
    rx: Consumer<ControlStamp>,
    dropped: AtomicU64,
}

impl ControlRing {
    pub fn new() -> Self {
        let (tx, rx) = RingBuffer::<ControlStamp>::new(CONTROL_RING_SLOTS);
        ControlRing {
            tx,
            rx,
            dropped: AtomicU64::new(0),
        }
    }

    pub fn producer(&mut self) -> &mut Producer<ControlStamp> {
        &mut self.tx
    }

    pub fn consumer(&mut self) -> &mut Consumer<ControlStamp> {
        &mut self.rx
    }

    pub fn push_stamp(&mut self, stamp: ControlStamp) -> bool {
        if self.tx.push(stamp).is_ok() {
            return true;
        }
        self.dropped.fetch_add(1, Ordering::Relaxed);
        false
    }

    pub fn drain(&mut self) -> Vec<ControlStamp> {
        let mut out = Vec::new();
        while let Ok(stamp) = self.rx.pop() {
            out.push(stamp);
        }
        out
    }

    pub fn dropped(&self) -> u64 {
        self.dropped.load(Ordering::Relaxed)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::engine::EngineCmd;

    #[test]
    fn transport_cmds_map_to_stable_ids() {
        let sr = 48_000;
        let s = 12_345_u64;
        let play = stamp_from_cmd(&EngineCmd::Play { deck: 2, playing: true }, s, sr, ControlSource::User).unwrap();
        assert_eq!(play.control, "deck.2.play");
        assert_eq!(play.kind, ControlKind::Set);
        let cue = stamp_from_cmd(&EngineCmd::Cue { deck: 1 }, s, sr, ControlSource::Controller).unwrap();
        assert_eq!(cue.kind, ControlKind::Trigger);
        let tempo = stamp_from_cmd(&EngineCmd::Tempo { deck: 1, ratio: 1.04 }, s, sr, ControlSource::User).unwrap();
        assert_eq!(tempo.control, "deck.1.tempo");
    }
}
