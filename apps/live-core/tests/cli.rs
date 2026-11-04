//! The odj-live binary: JSON lines from a file and from raw f32 on stdin.

mod common;

use common::*;
use std::io::Write;
use std::process::{Command, Stdio};

const BIN: &str = env!("CARGO_BIN_EXE_odj-live");

fn lines(stdout: &[u8]) -> Vec<serde_json::Value> {
    String::from_utf8(stdout.to_vec())
        .unwrap()
        .lines()
        .map(|l| serde_json::from_str(l).unwrap())
        .collect()
}

fn c_major(seconds: f64) -> Vec<f32> {
    let mut buf = silence(seconds);
    add_progression(&mut buf, C_MAJOR, 2.0, 0.0, seconds);
    normalize(&mut buf);
    buf
}

#[test]
fn a_file_prints_frames_then_one_summary() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_wav(dir.path(), "c.wav", &c_major(16.0));
    let out = Command::new(BIN)
        .args(["--file", path.to_str().unwrap(), "--hz", "5"])
        .output()
        .unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let v = lines(&out.stdout);
    assert_eq!(v.len(), 16 * 5 + 1);
    assert!(v[..v.len() - 1].iter().all(|f| f["type"] == "frame"));
    let last_frame = &v[v.len() - 2];
    assert_eq!(last_frame["chroma"].as_array().unwrap().len(), 12);
    assert_eq!(last_frame["chroma_slow"].as_array().unwrap().len(), 12);
    assert_eq!(last_frame["keys"].as_array().unwrap().len(), 3);
    for field in ["camelot", "openkey", "name", "score"] {
        assert!(!last_frame["keys"][0][field].is_null(), "{field}");
    }
    assert_eq!(last_frame["key"], "8B");
    for field in ["t", "confidence", "margin", "tuning_cents"] {
        assert!(last_frame[field].is_number(), "{field}: {last_frame}");
    }
    let summary = &v[v.len() - 1];
    assert_eq!(summary["type"], "summary");
    assert_eq!(summary["key"], "8B");
    // Segments are pending until TCLIVE-6567e is calibrated: a block with a
    // status, never a missing field, and no segments published.
    assert_eq!(summary["segments"]["status"], "pending");
    assert!(summary["segments"]["segments"].as_array().unwrap().is_empty());
}

#[test]
fn summary_prints_only_the_summary() {
    let dir = tempfile::tempdir().unwrap();
    let path = write_wav(dir.path(), "c.wav", &c_major(8.0));
    let out = Command::new(BIN)
        .args(["--file", path.to_str().unwrap(), "--summary"])
        .output()
        .unwrap();
    assert!(out.status.success());
    let v = lines(&out.stdout);
    assert_eq!(v.len(), 1);
    assert_eq!(v[0]["type"], "summary");
    assert_eq!(v[0]["frames"], 80);
}

#[test]
fn raw_f32_on_stdin_at_48k_streams_frames() {
    // The same C major progression, rendered at 44.1 kHz and fed as 48 kHz
    // after a plain linear resample: what a live capture hands the CLI.
    let src = c_major(16.0);
    let n = (src.len() as f64 * 48000.0 / f64::from(RATE)) as usize;
    let mut bytes = Vec::with_capacity(n * 4);
    for i in 0..n {
        let x = i as f64 * f64::from(RATE) / 48000.0;
        let j = x.floor() as usize;
        let f = (x - j as f64) as f32;
        let s = src[j] * (1.0 - f) + src.get(j + 1).copied().unwrap_or(0.0) * f;
        bytes.extend_from_slice(&s.to_le_bytes());
    }
    let mut child = Command::new(BIN)
        .args(["--stdin-f32", "--rate", "48000"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    // Written from a thread: the child's stdout fills its pipe while stdin
    // is still being written, so one thread doing both would deadlock.
    let mut stdin = child.stdin.take().unwrap();
    let writer = std::thread::spawn(move || {
        // Odd-sized writes split samples across reads.
        for chunk in bytes.chunks(4093) {
            stdin.write_all(chunk).unwrap();
        }
    });
    let out = child.wait_with_output().unwrap();
    writer.join().unwrap();
    assert!(out.status.success());
    let v = lines(&out.stdout);
    assert_eq!(v.len(), 161, "16 s at 10 Hz plus the summary");
    assert_eq!(v[159]["key"], "8B");
    assert_eq!(v[160]["sample_rate_in"], 48000);
    assert_eq!(v[160]["key"], "8B");
}

#[test]
fn usage_errors_exit_2_and_unreadable_audio_exits_1() {
    for args in [
        &[][..],
        &["--stdin-f32"][..],
        &["--file", "x.wav", "--stdin-f32", "--rate", "1"][..],
        &["--bogus"][..],
        &["--file", "x.wav", "--hz", "0"][..],
        &["--file", "x.wav", "--hz", "NaN"][..],
        &["--file", "x.wav", "--hz", "51"][..],
        &["--stdin-f32", "--rate", "7999"][..],
        &["--stdin-f32", "--rate", "384001"][..],
    ] {
        let out = Command::new(BIN).args(args).output().unwrap();
        assert_eq!(out.status.code(), Some(2), "{args:?}");
    }
    let out = Command::new(BIN)
        .args(["--file", "/nonexistent/odj-live-test.wav"])
        .output()
        .unwrap();
    assert_eq!(out.status.code(), Some(1));
    assert!(String::from_utf8_lossy(&out.stderr).contains("cannot open"));
    // A torn sample at the end of stdin is an error, not a silent drop.
    let mut child = Command::new(BIN)
        .args(["--stdin-f32", "--rate", "22050"])
        .stdin(Stdio::piped())
        .stdout(Stdio::null())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    child.stdin.take().unwrap().write_all(&[0u8; 4 * 1000 + 2]).unwrap();
    let out = child.wait_with_output().unwrap();
    assert_eq!(out.status.code(), Some(1));
}
