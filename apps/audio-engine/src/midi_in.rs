//! The engine's own MIDI input, through midir (feature `midi`; CoreMIDI,
//! WinMM, ALSA). Plan 20-03.
//!
//! Only ports a device map matches are opened, so a controller the engine
//! cannot drive stays the page's alone. Each open port hands its bytes to
//! the control thread (`serve::MidiSink`), where `midi::Router` decodes them
//! exactly as it decodes `midi_inject` lines; nothing here decodes.
//!
//! Ports are enumerated once, at start. A controller plugged in later is the
//! page's until the engine restarts (hot-plug is a follow-up, see
//! 20-03-PLAN.md).

use std::any::Any;

use midir::{Ignore, MidiInput};

use crate::midi::{self, MapSet};
use crate::serve::{MidiOpener, MidiSink};

/// The client name the engine registers with the OS MIDI system.
const CLIENT: &str = "odj-audio";

/// Open every input port a map matches. Fails when the OS MIDI system is
/// unavailable or a matched port will not open: `--midi` was asked for, so
/// a silent start without it would leave the controller dead.
pub fn opener() -> MidiOpener {
    Box::new(|maps: &MapSet, sink: MidiSink| {
        let probe = MidiInput::new(CLIENT).map_err(|e| format!("MIDI input is unavailable: {e}"))?;
        let mut claimed = Vec::new();
        let mut unclaimed = Vec::new();
        let mut open = Vec::new();
        for port in probe.ports() {
            let name = probe.port_name(&port).map_err(|e| format!("cannot read a MIDI port name: {e}"))?;
            let Some(map) = maps.resolve(&name) else {
                unclaimed.push(name);
                continue;
            };
            let map_name = map.name_match.clone();
            let mut input = MidiInput::new(CLIENT).map_err(|e| format!("MIDI input is unavailable: {e}"))?;
            // Sysex, timing clock and active sensing are outside the maps'
            // scope; the router would ignore them anyway.
            input.ignore(Ignore::All);
            let port = input
                .find_port_by_id(&port.id())
                .ok_or_else(|| format!("MIDI port {name} went away while opening"))?;
            let s = sink.clone();
            let n = name.clone();
            let conn = input
                .connect(&port, "odj-audio-in", move |_stamp_us, bytes, _| {
                    let _ = s.send(&n, bytes);
                }, ())
                .map_err(|e| format!("cannot open MIDI input {name}: {e}"))?;
            open.push(conn);
            claimed.push((name, map_name));
        }
        let keep: Box<dyn Any> = Box::new(open);
        Ok((keep, midi::ports_json(&claimed, &unclaimed)))
    })
}
