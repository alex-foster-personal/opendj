#![allow(dead_code)]
//! Shared fixtures: synthetic tones written as real WAV files, so tests go
//! through the same decode path the engine uses for a user's tracks.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU32, Ordering};

use odj_audio::wav;

static N: AtomicU32 = AtomicU32::new(0);

/// A fresh directory under the system temp dir, unique per call.
pub fn temp_dir(tag: &str) -> PathBuf {
    let d = std::env::temp_dir().join(format!(
        "odj-audio-test-{}-{}-{}",
        tag,
        std::process::id(),
        N.fetch_add(1, Ordering::Relaxed)
    ));
    std::fs::create_dir_all(&d).unwrap();
    d
}

/// Interleaved stereo sine, amplitude 0.5.
pub fn sine(sr: u32, hz: f64, secs: f64) -> Vec<f32> {
    let frames = (sr as f64 * secs) as usize;
    let mut pcm = Vec::with_capacity(frames * 2);
    for i in 0..frames {
        let v = (2.0 * std::f64::consts::PI * hz * i as f64 / sr as f64).sin() as f32 * 0.5;
        pcm.push(v);
        pcm.push(v);
    }
    pcm
}

pub fn write_wav(dir: &std::path::Path, name: &str, sr: u32, pcm: &[f32]) -> PathBuf {
    let p = dir.join(name);
    let mut f = std::io::BufWriter::new(std::fs::File::create(&p).unwrap());
    wav::write_i16(&mut f, sr, pcm).unwrap();
    p
}

/// A 120 BPM grid (one beat every 500 ms), downbeat every 4th beat.
pub fn grid_120_json(beats: usize) -> serde_json::Value {
    serde_json::Value::Array(
        (0..beats)
            .map(|i| serde_json::json!({"n": (i % 4) + 1, "time_ms": i as f64 * 500.0}))
            .collect(),
    )
}
