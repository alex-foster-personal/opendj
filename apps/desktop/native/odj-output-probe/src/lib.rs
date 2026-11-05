//! macOS CoreAudio code shared by both desktop shells.
//!
//! The Tauri shell links this crate and runs the output-health probe and the
//! headphone cue sink in process. The Electron shell does not link CoreAudio:
//! it runs this crate's `odj-output-probe` binary from the engine payload
//! (`payload/bin/odj-output-probe`, ELECTRON-6272). Keeping the code here, and
//! not inside `apps/desktop/src-tauri`, is what lets the Tauri shell be deleted
//! without losing the helper.

pub mod clock;
pub mod cue_sink;
pub mod output_health;
pub mod speaker_link;
