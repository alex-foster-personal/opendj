//! Synthetic test audio, generated in the test: chord progressions, tones,
//! click tracks, written as WAV files into a scratch directory. Nothing is
//! read from the network and no fixture is committed.
#![allow(dead_code)]

use std::io::Write;
use std::path::{Path, PathBuf};

pub const RATE: u32 = 44100;

pub fn midi_hz(midi: f64, cents: f64) -> f64 {
    440.0 * 2f64.powf((midi - 69.0) / 12.0 + cents / 1200.0)
}

/// A bass root plus a close triad an octave up, as MIDI notes.
pub fn triad(root_pc: u8, minor: bool) -> Vec<f64> {
    let r = f64::from(root_pc);
    vec![36.0 + r, 48.0 + r, 48.0 + r + if minor { 3.0 } else { 4.0 }, 48.0 + r + 7.0]
}

/// Add a note with four harmonics (a soft sawtooth), struck at `start_s`
/// for `dur_s`: 10 ms attack, decaying to half, 30 ms release.
pub fn add_note(buf: &mut [f32], midi: f64, cents: f64, start_s: f64, dur_s: f64, amp: f64) {
    let f = midi_hz(midi, cents);
    let start = (start_s * f64::from(RATE)) as usize;
    let len = (dur_s * f64::from(RATE)) as usize;
    for i in 0..len {
        let Some(out) = buf.get_mut(start + i) else { break };
        let t = i as f64 / f64::from(RATE);
        let env = (t / 0.01).min(1.0) * (0.5 + 0.5 * (-t / dur_s * 2.0).exp()) * ((dur_s - t) / 0.03).clamp(0.0, 1.0);
        let mut s = 0.0;
        for h in 1..=4 {
            let fh = f * h as f64;
            if fh < 10_000.0 {
                s += (2.0 * std::f64::consts::PI * fh * t).sin() / h as f64;
            }
        }
        *out += (amp * env * s) as f32;
    }
}

/// `chords` (root pitch class, minor) each held `chord_s`, looped to fill
/// `total_s`, starting at `offset_s`.
pub fn add_progression(buf: &mut [f32], chords: &[(u8, bool)], chord_s: f64, offset_s: f64, total_s: f64) {
    let mut t = 0.0;
    let mut i = 0;
    while t + 1e-9 < total_s {
        let (pc, minor) = chords[i % chords.len()];
        for m in triad(pc, minor) {
            add_note(buf, m, 0.0, offset_s + t, chord_s.min(total_s - t), 0.15);
        }
        t += chord_s;
        i += 1;
    }
}

/// A click every beat: 3 ms of a decaying 2.5 kHz burst plus noise.
pub fn add_clicks(buf: &mut [f32], bpm: f64, amp: f64) {
    let period = 60.0 / bpm;
    let mut seed: u32 = 12345;
    let mut k = 0u64;
    loop {
        let start = (k as f64 * period * f64::from(RATE)).round() as usize;
        if start >= buf.len() {
            break;
        }
        for i in 0..(0.003 * f64::from(RATE)) as usize {
            let Some(out) = buf.get_mut(start + i) else { break };
            seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
            let noise = (seed >> 8) as f64 / f64::from(1u32 << 24) * 2.0 - 1.0;
            let t = i as f64 / f64::from(RATE);
            let env = (-t / 0.001).exp();
            *out += (amp * env * (0.6 * (2.0 * std::f64::consts::PI * 2500.0 * t).sin() + 0.4 * noise)) as f32;
        }
        k += 1;
    }
}

pub fn silence(seconds: f64) -> Vec<f32> {
    vec![0.0; (seconds * f64::from(RATE)) as usize]
}

/// Scale to a 0.5 peak, so mixes never clip.
pub fn normalize(buf: &mut [f32]) {
    let peak = buf.iter().fold(0f32, |m, s| m.max(s.abs()));
    if peak > 0.0 {
        buf.iter_mut().for_each(|s| *s *= 0.5 / peak);
    }
}

/// 16-bit PCM stereo WAV (both channels the same), so every test also goes
/// through the symphonia decode, the downmix and the resampler.
pub fn write_wav(dir: &Path, name: &str, mono: &[f32]) -> PathBuf {
    let path = dir.join(name);
    let mut f = std::fs::File::create(&path).unwrap();
    let data_len = (mono.len() * 4) as u32;
    let mut h = Vec::with_capacity(44);
    h.extend_from_slice(b"RIFF");
    h.extend_from_slice(&(36 + data_len).to_le_bytes());
    h.extend_from_slice(b"WAVEfmt ");
    h.extend_from_slice(&16u32.to_le_bytes());
    h.extend_from_slice(&1u16.to_le_bytes()); // PCM
    h.extend_from_slice(&2u16.to_le_bytes()); // stereo
    h.extend_from_slice(&RATE.to_le_bytes());
    h.extend_from_slice(&(RATE * 4).to_le_bytes());
    h.extend_from_slice(&4u16.to_le_bytes());
    h.extend_from_slice(&16u16.to_le_bytes());
    h.extend_from_slice(b"data");
    h.extend_from_slice(&data_len.to_le_bytes());
    f.write_all(&h).unwrap();
    let mut body = Vec::with_capacity(mono.len() * 4);
    for s in mono {
        let v = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
        body.extend_from_slice(&v.to_le_bytes());
        body.extend_from_slice(&v.to_le_bytes());
    }
    f.write_all(&body).unwrap();
    path
}

// Progressions used across the tests.
pub const C_MAJOR: &[(u8, bool)] = &[(0, false), (5, false), (7, false), (0, false)]; // C F G C
pub const A_MINOR: &[(u8, bool)] = &[(9, true), (2, true), (4, false), (9, true)]; // Am Dm E Am
pub const E_MAJOR: &[(u8, bool)] = &[(4, false), (1, true), (9, false), (11, false)]; // E C#m A B
pub const FS_MINOR: &[(u8, bool)] = &[(6, true), (11, true), (1, false), (6, true)]; // F#m Bm C# F#m
