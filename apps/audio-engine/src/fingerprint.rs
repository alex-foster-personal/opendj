//! Chromaprint-compatible audio fingerprints, computed on the device.
//!
//! The duplicate finder (`apps/dedup`) used to shell out to chromaprint's
//! `fpcalc` through pyacoustid, which the packaged app does not ship, so a
//! shipped build could not fingerprint anything. This module produces the
//! same fingerprint `fpcalc` does by default (algorithm 2, the first 120 s,
//! compressed and URL-safe base64 encoded) with rusty-chromaprint (MIT) on
//! top of the engine's own symphonia decode. Nothing leaves the machine:
//! there is no AcoustID lookup.

use std::path::Path;

use rusty_chromaprint::{Configuration, FingerprintCompressor, Fingerprinter};
use symphonia::core::codecs::audio::AudioDecoderOptions;
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, TrackType};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;

use crate::decode::{leading_tag_bytes, open, probe_for};
use crate::engine::ErrorCode;
use crate::protocol::ProtoError;

/// How much audio `fpcalc` fingerprints unless told otherwise.
pub const DEFAULT_LENGTH_S: u32 = 120;

#[derive(Debug, Clone, PartialEq)]
pub struct AudioFingerprint {
    /// Length of the whole file in seconds, not only the part fingerprinted.
    pub duration_s: f64,
    /// Compressed fingerprint, URL-safe base64 without padding (`fpcalc`'s format).
    pub fingerprint: String,
    /// The raw 32-bit sub-fingerprints the string encodes.
    pub raw: Vec<u32>,
}

/// Fingerprint the first `length_s` seconds of `path` (0 means the whole file).
pub fn fingerprint_file(path: &Path, length_s: u32) -> Result<AudioFingerprint, ProtoError> {
    let mut file = open(path)?;
    // Probe past a large leading ID3 tag the way deck load does, so a file
    // the decoder plays is never "unrecognized" here.
    let lead = leading_tag_bytes(&mut file, path)?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let err = |what: &str, e: &dyn std::fmt::Display| ProtoError::new(ErrorCode::Decode, format!("{what} {}: {e}", path.display()));
    let mut format = probe_for(lead)
        .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
        .map_err(|e| err("unrecognized format in", &e))?;
    let track = format
        .default_track(TrackType::Audio)
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio track in {}", path.display())))?;
    let track_id = track.id;
    let time_base = track.time_base;
    let track_frames = track.num_frames;
    let params = track
        .codec_params
        .as_ref()
        .and_then(|p| p.audio())
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio codec parameters in {}", path.display())))?;
    let mut decoder = symphonia::default::get_codecs()
        .make_audio_decoder(params, &AudioDecoderOptions::default())
        .map_err(|e| err("unsupported codec in", &e))?;

    let config = Configuration::preset_test2();
    let mut printer = Fingerprinter::new(&config);
    let mut rate = 0u32;
    let mut channels = 0usize;
    // Frames handed to the fingerprinter, and frames seen in the whole file.
    let mut fed: u64 = 0;
    let mut budget: u64 = u64::MAX;
    let mut total_ticks: u64 = 0;
    let mut decoded_frames: u64 = 0;
    let mut scratch: Vec<f32> = Vec::new();
    let mut pcm: Vec<i16> = Vec::new();
    loop {
        let packet = match format.next_packet() {
            Ok(Some(p)) => p,
            Ok(None) => break,
            // A chained stream that changes part-way through ends the
            // fingerprinted part; everything before it is still valid.
            Err(SymError::ResetRequired) => break,
            Err(e) => return Err(err("read error in", &e)),
        };
        if packet.track_id != track_id {
            continue;
        }
        total_ticks = total_ticks.saturating_add(packet.dur.get());
        if fed >= budget {
            // Past the fingerprinted part, only the length is still wanted:
            // demuxing gives it without decoding.
            continue;
        }
        let buf = match decoder.decode(&packet) {
            Ok(b) => b,
            // One bad packet is skipped, as fpcalc's ffmpeg decode does.
            Err(SymError::DecodeError(_)) => continue,
            Err(e) => return Err(err("decode error in", &e)),
        };
        let spec = buf.spec();
        if rate == 0 {
            rate = spec.rate();
            channels = spec.channels().count().min(2);
            if channels == 0 {
                return Err(ProtoError::new(ErrorCode::Decode, format!("zero channels in {}", path.display())));
            }
            printer
                .start(rate, channels as u32)
                .map_err(|e| err("cannot fingerprint", &format!("{e:?}")))?;
            if length_s > 0 {
                budget = u64::from(rate) * u64::from(length_s);
            }
        } else if spec.rate() != rate {
            break;
        }
        let ch = spec.channels().count();
        scratch.resize(buf.samples_interleaved(), 0.0);
        buf.copy_to_slice_interleaved(&mut scratch[..]);
        let frames = buf.frames() as u64;
        decoded_frames += frames;
        let take = frames.min(budget - fed) as usize;
        pcm.clear();
        for frame in scratch.chunks_exact(ch).take(take) {
            for s in &frame[..channels] {
                pcm.push((s.clamp(-1.0, 1.0) * 32767.0).round() as i16);
            }
        }
        printer.consume(&pcm);
        fed += take as u64;
    }
    if rate == 0 {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    printer.finish();
    let raw = printer.fingerprint().to_vec();
    if raw.is_empty() {
        return Err(ProtoError::new(ErrorCode::Decode, format!("{} is too short to fingerprint", path.display())));
    }
    let compressed = FingerprintCompressor::from(&config).compress(&raw);
    let duration_s = file_seconds(track_frames, total_ticks, time_base, decoded_frames, rate);
    Ok(AudioFingerprint { duration_s, fingerprint: base64url(&compressed), raw })
}

/// The whole file's length: the container's frame count when it states one,
/// else the summed packet durations, else the frames actually decoded.
fn file_seconds(
    track_frames: Option<u64>,
    ticks: u64,
    time_base: Option<symphonia::core::units::TimeBase>,
    decoded_frames: u64,
    rate: u32,
) -> f64 {
    if let Some(n) = track_frames.filter(|n| *n > 0) {
        return n as f64 / f64::from(rate);
    }
    if let (Some(tb), true) = (time_base, ticks > 0) {
        return ticks as f64 * f64::from(tb.numer.get()) / f64::from(tb.denom.get());
    }
    decoded_frames as f64 / f64::from(rate)
}

/// URL-safe base64 without padding, the alphabet `fpcalc` prints.
pub fn base64url(bytes: &[u8]) -> String {
    const A: &[u8; 64] = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";
    let mut out = String::with_capacity(bytes.len().div_ceil(3) * 4);
    for chunk in bytes.chunks(3) {
        let n = chunk.iter().enumerate().fold(0u32, |acc, (i, b)| acc | u32::from(*b) << (16 - 8 * i));
        let keep = chunk.len() + 1;
        for i in 0..keep {
            out.push(A[(n >> (18 - 6 * i) & 63) as usize] as char);
        }
    }
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn base64url_matches_the_rfc_vectors_without_padding() {
        assert_eq!(base64url(b""), "");
        assert_eq!(base64url(b"f"), "Zg");
        assert_eq!(base64url(b"fo"), "Zm8");
        assert_eq!(base64url(b"foo"), "Zm9v");
        assert_eq!(base64url(b"foobar"), "Zm9vYmFy");
        assert_eq!(base64url(&[0xfb, 0xff]), "-_8");
    }

    fn tone_wav(dir: &Path, name: &str, secs: f32, rate: u32, gain: f32) -> std::path::PathBuf {
        let n = (secs * rate as f32) as usize;
        let mut pcm = Vec::with_capacity(n * 2);
        for i in 0..n {
            let t = i as f32 / rate as f32;
            // A melody that moves through pitch classes, so the chroma changes.
            let f = 220.0 * 2f32.powf(((t * 2.0) as u32 % 12) as f32 / 12.0);
            let s = gain * ((2.0 * std::f32::consts::PI * f * t).sin() * 0.5 + (2.0 * std::f32::consts::PI * f * 1.5 * t).sin() * 0.3);
            pcm.push(s);
            pcm.push(s);
        }
        let p = dir.join(name);
        crate::wav::write_i16(&mut std::fs::File::create(&p).unwrap(), rate, &pcm).unwrap();
        p
    }

    #[test]
    fn same_audio_matches_and_other_audio_does_not() {
        let d = tempfile::tempdir().unwrap();
        let a = fingerprint_file(&tone_wav(d.path(), "a.wav", 20.0, 44_100, 0.8), DEFAULT_LENGTH_S).unwrap();
        // The same music, quieter and at another rate: still the same fingerprint, near enough.
        let b = fingerprint_file(&tone_wav(d.path(), "b.wav", 20.0, 48_000, 0.5), DEFAULT_LENGTH_S).unwrap();
        assert!((a.duration_s - 20.0).abs() < 0.01, "duration {}", a.duration_s);
        assert!(a.fingerprint.starts_with("AQ"), "algorithm byte 1 encodes as AQ: {}", a.fingerprint);
        let sim = |x: &[u32], y: &[u32]| {
            let n = x.len().min(y.len());
            let diff: u32 = x[..n].iter().zip(&y[..n]).map(|(p, q)| (p ^ q).count_ones()).sum();
            1.0 - f64::from(diff) / (n as f64 * 32.0)
        };
        assert!(sim(&a.raw, &b.raw) > 0.9, "same music scored {}", sim(&a.raw, &b.raw));

        // Different music (noise) must score clearly lower, so the check can say no.
        let n = 20 * 44_100;
        let mut seed = 1u32;
        let noise: Vec<f32> = (0..n * 2)
            .map(|_| {
                seed = seed.wrapping_mul(1_664_525).wrapping_add(1_013_904_223);
                (seed >> 8) as f32 / (1u32 << 24) as f32 - 0.5
            })
            .collect();
        let np = d.path().join("noise.wav");
        crate::wav::write_i16(&mut std::fs::File::create(&np).unwrap(), 44_100, &noise).unwrap();
        let c = fingerprint_file(&np, DEFAULT_LENGTH_S).unwrap();
        assert!(sim(&a.raw, &c.raw) < 0.75, "different audio scored {}", sim(&a.raw, &c.raw));
    }

    #[test]
    fn length_limits_the_fingerprint_but_not_the_duration() {
        let d = tempfile::tempdir().unwrap();
        let p = tone_wav(d.path(), "long.wav", 30.0, 22_050, 0.8);
        let short = fingerprint_file(&p, 10).unwrap();
        let whole = fingerprint_file(&p, 0).unwrap();
        assert!((short.duration_s - 30.0).abs() < 0.01);
        assert!(short.raw.len() * 2 < whole.raw.len(), "{} vs {}", short.raw.len(), whole.raw.len());
        assert_eq!(&whole.raw[..short.raw.len() - 4], &short.raw[..short.raw.len() - 4]);
    }

    #[test]
    fn a_file_that_is_not_audio_is_an_error() {
        let d = tempfile::tempdir().unwrap();
        let p = d.path().join("x.mp3");
        std::fs::write(&p, b"not audio at all").unwrap();
        assert!(fingerprint_file(&p, DEFAULT_LENGTH_S).is_err());
    }
}
