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
    // Taken in `Drop`, which deletes it checked rather than silently.
    dir: Option<tempfile::TempDir>,
}

/// A directory that will not delete fails the test that made it: a leaked
/// handle or a still-running child must not pass green and leave the tree
/// behind. While a test is already panicking the delete is best-effort, so
/// the original failure is the one reported.
impl Drop for TestDir {
    fn drop(&mut self) {
        let Some(dir) = self.dir.take() else { return };
        let deleted = dir.close();
        if !std::thread::panicking() {
            if let Err(e) = deleted {
                panic!(
                    "could not delete test scratch dir {}: {e}",
                    self.path.display()
                );
            }
        }
    }
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
        dir: Some(dir),
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

pub fn write_wav(dir: &Path, name: &str, sr: u32, pcm: &[f32]) -> PathBuf {
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

#[cfg(unix)]
fn changed_stamp_ns(stamp: (i64, i64)) -> u128 {
    stamp.0 as u128 * 1_000_000_000 + stamp.1.max(0) as u128
}

#[cfg(unix)]
fn changed_stamp_after(observed: (i64, i64), floor: (i64, i64)) -> bool {
    changed_stamp_ns(observed) > changed_stamp_ns(floor)
}

/// Return once the volume's change-time clock is strictly after `floor`.
///
/// Waits on wall clock and a sibling probe write so the case-under-test file
/// is not rewritten while the jiffy still matches the session-cached stamp.
#[cfg(unix)]
pub fn wait_volume_clock_past(
    floor: (i64, i64),
    probe: &Path,
    deadline: std::time::Instant,
) {
    let floor_ns = changed_stamp_ns(floor);
    let margin_ns = 20_000_000u128;
    while std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .expect("clock")
        .as_nanos()
        <= floor_ns + margin_ns
    {
        assert!(
            std::time::Instant::now() < deadline,
            "the wall clock stayed within the session-cached change-time stamp {floor:?} for 5 s"
        );
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
    loop {
        std::fs::write(probe, b"x").unwrap();
        let m = std::fs::metadata(probe).unwrap();
        use std::os::unix::fs::MetadataExt;
        let observed = (m.ctime(), m.ctime_nsec());
        if changed_stamp_after(observed, floor) {
            return;
        }
        assert!(
            std::time::Instant::now() < deadline,
            "the volume change-time clock stayed at or before {floor:?} for 5 s (probe observed {observed:?})"
        );
        std::thread::sleep(std::time::Duration::from_millis(5));
    }
}
