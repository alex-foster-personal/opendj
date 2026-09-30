#![allow(dead_code)]
//! Shared fixtures: synthetic tones written as real WAV files, so tests go
//! through the same decode path the engine uses for a user's tracks.

use std::ffi::OsStr;
use std::ops::Deref;
use std::path::{Path, PathBuf};

use odj_audio::wav;

/// A test's scratch directory, deleted with everything in it when this guard
/// drops, so it goes on panic and unwind too. Keep the guard bound for as
/// long as anything, a spawned `odj-audio` included, uses the directory: a
/// helper hands the guard on, never a bare path to it. Derefs to `PathBuf`,
/// so `d.join(..)`, `&d` and `d.clone()` (an owned path) read as before.
pub struct TestDir {
    path: PathBuf,
    // Owns the deletion; `path` is a copy so the guard can deref to `PathBuf`.
    _dir: tempfile::TempDir,
}

impl Deref for TestDir {
    type Target = PathBuf;
    fn deref(&self) -> &PathBuf {
        &self.path
    }
}

impl AsRef<Path> for TestDir {
    fn as_ref(&self) -> &Path {
        &self.path
    }
}

impl AsRef<OsStr> for TestDir {
    fn as_ref(&self) -> &OsStr {
        self.path.as_os_str()
    }
}

/// A fresh directory under the system temp dir (`TMPDIR`), unique per call.
/// Its name keeps the `odj-audio-test-<tag>-<pid>-` prefix so a host reaper
/// can still match what a killed job left behind.
pub fn temp_dir(tag: &str) -> TestDir {
    let dir = tempfile::Builder::new()
        .prefix(&format!("odj-audio-test-{tag}-{}-", std::process::id()))
        .tempdir()
        .unwrap();
    TestDir {
        path: dir.path().to_path_buf(),
        _dir: dir,
    }
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
