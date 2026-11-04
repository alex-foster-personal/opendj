//! End-to-end: synthetic WAVs through decode, resample, analysis and summary.

mod common;

use common::*;
use odj_live_core::{analyze_file, Frame, Summary};

fn run(name: &str, audio: &[f32]) -> (Vec<Frame>, Summary) {
    let dir = tempfile::tempdir().unwrap();
    let path = write_wav(dir.path(), name, audio);
    analyze_file(&path, 10.0).unwrap()
}

fn progression(chords: &[(u8, bool)], seconds: f64) -> Vec<f32> {
    let mut buf = silence(seconds);
    add_progression(&mut buf, chords, 2.0, 0.0, seconds);
    normalize(&mut buf);
    buf
}

#[test]
fn c_major_triads_are_8b() {
    let (frames, summary) = run("c.wav", &progression(C_MAJOR, 24.0));
    assert_eq!(summary.key.as_deref(), Some("8B"), "{:?}", summary.keys);
    assert_eq!(summary.key_openkey.as_deref(), Some("1d"));
    assert_eq!(summary.key_name.as_deref(), Some("C"));
    // The live readout agrees by the end.
    assert_eq!(frames.last().unwrap().key.as_deref(), Some("8B"));
    assert_eq!(summary.keys.len(), 3);
    assert!(summary.margin.unwrap() > 0.0);
}

#[test]
fn a_minor_is_8a() {
    let (frames, summary) = run("am.wav", &progression(A_MINOR, 24.0));
    assert_eq!(summary.key.as_deref(), Some("8A"), "{:?}", summary.keys);
    assert_eq!(summary.key_name.as_deref(), Some("Am"));
    assert_eq!(frames.last().unwrap().key.as_deref(), Some("8A"));
}

#[test]
fn an_e_major_chord_loop_is_12b() {
    let (frames, summary) = run("e.wav", &progression(E_MAJOR, 32.0));
    assert_eq!(summary.key.as_deref(), Some("12B"), "{:?}", summary.keys);
    assert_eq!(frames.last().unwrap().key.as_deref(), Some("12B"));
}

#[test]
fn a_tone_20_cents_sharp_reads_plus_20_cents() {
    let mut buf = silence(10.0);
    add_note(&mut buf, 69.0, 20.0, 0.0, 10.0, 0.3);
    let (frames, summary) = run("detuned.wav", &buf);
    let cents = summary.tuning_cents.unwrap();
    assert!((cents - 20.0).abs() <= 5.0, "summary tuning {cents}");
    let live = frames.last().unwrap().tuning_cents.unwrap();
    assert!((live - 20.0).abs() <= 5.0, "live tuning {live}");
    // Retuned filters put the tone cleanly on A.
    let c = frames.last().unwrap().chroma;
    let best = (0..12).max_by(|&a, &b| c[a].partial_cmp(&c[b]).unwrap()).unwrap();
    assert_eq!(best, 9, "{c:?}");
    // Without retuning, a tone 20 cents sharp leaks about 0.15 into A#
    // (measured); with it, under 0.01.
    assert!(c[10] < 0.05, "A# leak {c:?}");
}

#[test]
fn an_in_tune_tone_reads_zero_cents() {
    // Control for the test above: the offset is measured, not a constant.
    let mut buf = silence(10.0);
    add_note(&mut buf, 69.0, 0.0, 0.0, 10.0, 0.3);
    let (_, summary) = run("intune.wav", &buf);
    assert!(summary.tuning_cents.unwrap().abs() <= 3.0, "{:?}", summary.tuning_cents);
}

#[test]
fn a_click_track_bpm_is_withheld_until_tempo_is_calibrated() {
    // TCLIVE-6567d: the tempo prior is a placeholder, so no BPM is published,
    // in the frames or the summary, even for a clean 124 BPM click track.
    // The estimator's own accuracy is covered in tempo.rs.
    let mut buf = silence(30.0);
    add_clicks(&mut buf, 124.0, 0.8);
    let (frames, summary) = run("clicks.wav", &buf);
    assert!(summary.bpm.is_none(), "{:?}", summary.bpm);
    assert!(frames.iter().all(|f| f.bpm.is_none()));
}

#[test]
fn a_key_switch_publishes_no_segments_until_they_are_calibrated() {
    // TCLIVE-6567e: the segment constants are placeholders, so the block is
    // pending and empty even for a clear E major to F sharp minor switch.
    // The segmenter's own behavior is covered in segments.rs.
    let half = 48.0;
    let mut buf = silence(2.0 * half);
    add_progression(&mut buf, E_MAJOR, 2.0, 0.0, half);
    add_progression(&mut buf, FS_MINOR, 2.0, half, half);
    add_clicks(&mut buf, 120.0, 0.3);
    normalize(&mut buf);
    let (_, summary) = run("switch.wav", &buf);
    assert_eq!(summary.segments.status, "pending");
    assert_eq!(summary.segments.reason, Some("uncalibrated"));
    assert!(summary.segments.segments.is_empty(), "{:?}", summary.segments);
}

#[test]
fn silence_has_no_key_no_tempo_and_no_tuning() {
    // 20 s is ten 2-second windows, long enough to reach the segmenter.
    let (frames, summary) = run("silence.wav", &silence(20.0));
    assert!(summary.key.is_none() && summary.keys.is_empty());
    assert!(summary.bpm.is_none());
    assert!(summary.tuning_cents.is_none());
    assert!(frames.iter().all(|f| f.key.is_none() && f.bpm.is_none()));
    assert_eq!(summary.segments.status, "pending");
    assert!(summary.segments.segments.is_empty());
}

#[test]
fn frames_come_at_the_asked_rate_with_true_times() {
    let (frames, summary) = run("rate.wav", &progression(C_MAJOR, 12.0));
    assert_eq!(frames.len(), 120);
    assert_eq!(summary.frames, 120);
    for (i, f) in frames.iter().enumerate() {
        assert!((f.t - (i + 1) as f64 * 0.1).abs() < 1e-6, "frame {i} at {}", f.t);
    }
    assert!((summary.duration_s - 12.0).abs() < 1e-3);
    // librosa's frame count for 12 s at hop 512 is 1 + 264600 // 512 = 517.
    assert!((summary.analysis_frames as i64 - 517).abs() <= 1, "{}", summary.analysis_frames);
}

fn best_class(c: &[f64; 12]) -> usize {
    (0..12).max_by(|&a, &b| c[a].partial_cmp(&c[b]).unwrap()).unwrap()
}

#[test]
fn a_tone_drifting_a_semitone_sharp_moves_to_the_next_pitch_class() {
    // A4 drifting up in 8-cent steps every 4 s to +80 cents, which is A#
    // 20 cents flat. The filters follow the drift and wrap past +60, so the
    // tone lands on A#, not on A with filters 80 cents sharp.
    let mut buf = silence(48.0);
    for (i, cents) in [0.0, 8.0, 16.0, 24.0, 32.0, 40.0, 48.0, 56.0, 64.0, 72.0, 80.0, 80.0].iter().enumerate() {
        add_note(&mut buf, 69.0, *cents, i as f64 * 4.0, 4.0, 0.3);
    }
    let (frames, _) = run("drift.wav", &buf);
    let last = frames.last().unwrap();
    assert_eq!(best_class(&last.chroma), 10, "{:?}", last.chroma);
    let live = last.tuning_cents.unwrap();
    assert!((live + 20.0).abs() <= 5.0, "live tuning {live}");
}

#[test]
fn a_tone_hovering_at_a_quarter_tone_keeps_its_pitch_class() {
    // Control for the wrap: a tone wobbling between +42 and +58 cents stays
    // inside the 10 cents of slack, so it does not flip between A and A#.
    let mut buf = silence(40.0);
    for i in 0..10 {
        let cents = if i % 2 == 0 { 42.0 } else { 58.0 };
        add_note(&mut buf, 69.0, cents, i as f64 * 4.0, 4.0, 0.3);
    }
    let (frames, _) = run("hover.wav", &buf);
    for f in frames.iter().filter(|f| f.t >= 5.0) {
        assert_eq!(best_class(&f.chroma), 9, "at {}: {:?}", f.t, f.chroma);
    }
}

#[test]
fn silence_after_a_track_clears_the_live_key() {
    let mut buf = silence(16.0);
    add_progression(&mut buf, C_MAJOR, 2.0, 0.0, 10.0);
    normalize(&mut buf);
    let (frames, summary) = run("stop.wav", &buf);
    let at = |t: f64| frames.iter().find(|f| (f.t - t).abs() < 1e-6).unwrap();
    // The whole-stream tuning survives the stop.
    assert!(summary.tuning_cents.is_some());
    // Control: the key holds through the track and the first moments of
    // silence (a short gap is not a stop).
    assert_eq!(at(10.0).key.as_deref(), Some("8B"));
    assert_eq!(at(10.5).key.as_deref(), Some("8B"));
    assert!(at(10.5).tuning_cents.is_some(), "tuning holds through a short gap");
    // The 1.49 s constant-Q window is all zeros from 11.49 s, so the key and
    // the recent tuning clear at 12.49 s, not held for minutes while the
    // averages decay.
    for f in frames.iter().filter(|f| f.t >= 12.5) {
        assert!(f.key.is_none() && f.keys.is_empty(), "at {}: {:?}", f.t, f.key);
        assert!(f.tuning_cents.is_none(), "at {}: tuning {:?}", f.t, f.tuning_cents);
    }
}

/// Deterministic uniform noise at `rms` (uniform on [-a, a] has RMS a/sqrt 3).
fn noise(seconds: f64, rms: f32) -> Vec<f32> {
    let a = rms * 3f32.sqrt();
    let mut x: u32 = 0x1234_5678;
    (0..(seconds * f64::from(RATE)) as usize)
        .map(|_| {
            x = x.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            (x as f32 / u32::MAX as f32 * 2.0 - 1.0) * a
        })
        .collect()
}

#[test]
fn idle_noise_after_a_track_clears_the_live_key() {
    // A stopped deck into a live interface is -80 dBFS hiss, not zeros.
    let mut buf = silence(16.0);
    add_progression(&mut buf, C_MAJOR, 2.0, 0.0, 10.0);
    normalize(&mut buf);
    let start = (10.0 * f64::from(RATE)) as usize;
    for (s, n) in buf[start..].iter_mut().zip(noise(6.0, 1e-4)) {
        *s += n;
    }
    let (frames, _) = run("hiss.wav", &buf);
    assert_eq!(frames.iter().find(|f| (f.t - 10.0).abs() < 1e-6).unwrap().key.as_deref(), Some("8B"));
    for f in frames.iter().filter(|f| f.t >= 12.5) {
        assert!(f.key.is_none() && f.tuning_cents.is_none(), "at {}: {:?} {:?}", f.t, f.key, f.tuning_cents);
    }
}

#[test]
fn hiss_alone_has_no_summary_tuning() {
    // Hiss below the floor is silence for the summary tuner too, not a
    // whole-stream histogram of noise peaks.
    let (frames, summary) = run("hiss-only.wav", &noise(20.0, 1e-4));
    assert!(summary.tuning_cents.is_none(), "{:?}", summary.tuning_cents);
    assert!(summary.key.is_none());
    assert!(frames.iter().all(|f| f.tuning_cents.is_none()));
}

#[test]
fn a_quiet_track_above_the_noise_floor_keeps_its_key() {
    // Control for the gate: a track 40 dB down (-46 dBFS peak) is music, not hiss.
    let mut buf = progression(C_MAJOR, 16.0);
    buf.iter_mut().for_each(|s| *s *= 0.01);
    let (frames, summary) = run("quiet.wav", &buf);
    assert_eq!(summary.key.as_deref(), Some("8B"));
    assert_eq!(frames.last().unwrap().key.as_deref(), Some("8B"));
    assert!(summary.tuning_cents.is_some(), "the tuner still hears a quiet track");
}

#[test]
fn a_short_dropout_keeps_the_live_key() {
    // Control for the test above: a 2.2 s gap mid-track (0.7 s of all-zero
    // columns once the 1.49 s window has cleared) does not clear the key.
    let mut buf = silence(20.0);
    add_progression(&mut buf, C_MAJOR, 2.0, 0.0, 10.0);
    let mut tail = silence(20.0);
    add_progression(&mut tail, C_MAJOR, 2.0, 12.2, 20.0);
    for (a, b) in buf.iter_mut().zip(&tail) {
        *a += b;
    }
    normalize(&mut buf);
    let (frames, _) = run("dropout.wav", &buf);
    for f in frames.iter().filter(|f| f.t >= 6.0) {
        assert_eq!(f.key.as_deref(), Some("8B"), "at {}", f.t);
    }
}
