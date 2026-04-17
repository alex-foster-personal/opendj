//! Phase 18 launcher drag-core.
//!
//! Vendor-agnostic `DragAdapter` trait + dispatcher + per-vendor adapters
//! (Rekordbox, Serato, Traktor, djay reference) + sidecar XML safety utility.
//!
//! Tauri + macOS surfaces are abstracted behind the `HostBridge` trait so this
//! crate builds + tests with no OS linkage. Phase 17's launcher bin supplies
//! the real `HostBridge` impl (wiring `tauri-plugin-drag`, `tauri-plugin-
//! clipboard-manager`, `objc2 NSWorkspace.runningApplications`) at integration
//! time.
//!
//! Requirement tags:
//! - `LAUNCH-02b` (Rekordbox drag + sidecar XML fallback)
//! - `LAUNCH-02c` (Serato drag + clipboard fallback)
//! - `LAUNCH-02d` (Traktor drag + sidecar NML fallback)
//!
//! See `.planning/phases/18-launcher-v1-other-apps/` for authoritative design.

pub mod drag;

pub use drag::{
    DragAdapter, DragError, DragEvent, DragOutcome, HostBridge, Track, Vendor,
    dispatcher::Dispatcher,
    djay::DjayAdapter,
    rekordbox::{RekordboxAdapter, RekordboxMajor},
    serato::SeratoAdapter,
    traktor::TraktorAdapter,
};
