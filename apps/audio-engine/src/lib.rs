//! odj-audio: Open DJ's audio engine as its own process.
//!
//! One actor (`engine::Engine`) owns playback state. The UI, MIDI, agents and
//! the CLI are equal senders into its mailbox (`protocol`), and it runs on a
//! device clock, a wall clock, or a fake clock (`serve`, `offline`). See
//! `.planning/phases/20-rust-audio-engine/20-CONTEXT.md` for the decisions.

pub mod deck;
pub mod decode;
pub mod edit_list;
#[cfg(feature = "device")]
pub mod device;
pub mod dsp;
pub mod engine;
pub mod fingerprint;
pub mod midi;
#[cfg(feature = "midi")]
pub mod midi_in;
pub mod mixer;
pub mod mp4edit;
pub mod offline;
pub mod plan;
pub mod protocol;
pub mod serve;
pub mod stretch;
pub mod waveform;
pub mod wav;
pub mod ws;
