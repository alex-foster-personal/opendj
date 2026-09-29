//! Whole-file decode to interleaved stereo f32, resampled once to the
//! engine's rate.
//!
//! Runs on a control or worker thread, never the audio thread. A file at
//! another rate (most of a real library is 44.1 kHz) is converted at load
//! with rubato's FFT resampler, the way the page's `decodeAudioData`
//! resamples to the context rate. The deck's Hermite interpolator then only
//! does tempo. The 20-05 null test measured Hermite doing both at once:
//! -1.3 dB at 12-16 kHz and -2.4 dB at 16-20 kHz on every 44.1 kHz track.

use std::fs::File;
use std::path::Path;

use symphonia::core::codecs::audio::AudioDecoderOptions;
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, TrackType};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;

use rubato::{FftFixedIn, Resampler};

use crate::engine::ErrorCode;
use crate::protocol::ProtoError;

pub struct Decoded {
    pub sample_rate: u32,
    /// Interleaved stereo.
    pub pcm: Vec<f32>,
}

/// Decode `path` and resample it to `sample_rate` when the file's rate differs.
pub fn decode_at(path: &Path, sample_rate: u32) -> Result<Decoded, ProtoError> {
    resample(decode_file(path)?, sample_rate)
        .map_err(|e| ProtoError::new(ErrorCode::Decode, format!("cannot resample {}: {e}", path.display())))
}

/// Input chunk for the FFT resampler. Larger chunks cost memory, not quality.
const RESAMPLE_CHUNK: usize = 4096;

/// Convert interleaved stereo to `to` Hz. The output has exactly
/// `round(frames * to / from)` frames, time-aligned with the input: the
/// resampler's own delay is dropped from the front and its tail flushed.
pub fn resample(d: Decoded, to: u32) -> Result<Decoded, String> {
    if d.sample_rate == to {
        return Ok(d);
    }
    let from = d.sample_rate;
    let n = d.pcm.len() / 2;
    let want = ((n as u128 * to as u128 + from as u128 / 2) / from as u128) as usize;
    let mut r = FftFixedIn::<f32>::new(from as usize, to as usize, RESAMPLE_CHUNK, 2, 2).map_err(|e| e.to_string())?;
    let delay = r.output_delay();
    let mut inp = [vec![0f32; RESAMPLE_CHUNK], vec![0f32; RESAMPLE_CHUNK]];
    let mut out = [vec![0f32; r.output_frames_max()], vec![0f32; r.output_frames_max()]];
    let mut pcm = Vec::with_capacity(want * 2);
    let mut skip = delay;
    let mut pos = 0;
    while pcm.len() < want * 2 {
        let take = RESAMPLE_CHUNK.min(n.saturating_sub(pos));
        for (c, ch) in inp.iter_mut().enumerate() {
            for (i, s) in ch.iter_mut().enumerate() {
                *s = if i < take { d.pcm[(pos + i) * 2 + c] } else { 0.0 };
            }
        }
        pos += RESAMPLE_CHUNK;
        let (_, got) = r.process_into_buffer(&inp, &mut out, None).map_err(|e| e.to_string())?;
        let from_i = skip.min(got);
        skip -= from_i;
        for i in from_i..got {
            if pcm.len() == want * 2 {
                break;
            }
            pcm.push(out[0][i]);
            pcm.push(out[1][i]);
        }
    }
    Ok(Decoded { sample_rate: to, pcm })
}

/// Decode `path` at its own rate. Mono is duplicated to both sides; files with more than two
/// channels keep their first two (front left and right).
pub fn decode_file(path: &Path) -> Result<Decoded, ProtoError> {
    let file = File::open(path)
        .map_err(|e| ProtoError::new(ErrorCode::Io, format!("cannot open {}: {e}", path.display())))?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let dec_err = |what: &str, e: &dyn std::fmt::Display| {
        ProtoError::new(ErrorCode::Decode, format!("{what} {}: {e}", path.display()))
    };
    let mut format = symphonia::default::get_probe()
        .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
        .map_err(|e| dec_err("unrecognized format in", &e))?;
    let track = format
        .default_track(TrackType::Audio)
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio track in {}", path.display())))?;
    let track_id = track.id;
    let params = track
        .codec_params
        .as_ref()
        .and_then(|p| p.audio())
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio codec parameters in {}", path.display())))?;
    let mut decoder = symphonia::default::get_codecs()
        .make_audio_decoder(params, &AudioDecoderOptions::default())
        .map_err(|e| dec_err("unsupported codec in", &e))?;

    let mut pcm: Vec<f32> = Vec::new();
    let mut scratch: Vec<f32> = Vec::new();
    let mut sample_rate = 0u32;
    loop {
        let packet = match format.next_packet() {
            Ok(Some(p)) => p,
            Ok(None) => break,
            Err(SymError::ResetRequired) => break,
            Err(e) => return Err(dec_err("read error in", &e)),
        };
        if packet.track_id != track_id {
            continue;
        }
        let buf = match decoder.decode(&packet) {
            Ok(b) => b,
            // A corrupt packet is skipped, as every player does; anything
            // else stops the decode with an error rather than a short track.
            Err(SymError::DecodeError(_)) => continue,
            Err(e) => return Err(dec_err("decode error in", &e)),
        };
        let spec = buf.spec();
        sample_rate = spec.rate();
        let ch = spec.channels().count();
        if ch == 0 {
            return Err(ProtoError::new(ErrorCode::Decode, format!("zero channels in {}", path.display())));
        }
        scratch.resize(buf.samples_interleaved(), 0.0);
        buf.copy_to_slice_interleaved(&mut scratch[..]);
        pcm.reserve(buf.frames() * 2);
        for frame in scratch.chunks_exact(ch) {
            let l = frame[0];
            let r = if ch == 1 { frame[0] } else { frame[1] };
            pcm.push(l);
            pcm.push(r);
        }
    }
    if sample_rate == 0 || pcm.is_empty() {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    Ok(Decoded { sample_rate, pcm })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(sr: u32, hz: f64, frames: usize) -> Vec<f32> {
        (0..frames)
            .flat_map(|i| {
                let v = (2.0 * std::f64::consts::PI * hz * i as f64 / sr as f64).sin() as f32 * 0.5;
                [v, v]
            })
            .collect()
    }

    fn err_db(got: &[f32], want: &[f32], skip: usize) -> f64 {
        let n = got.len().min(want.len());
        let (mut e, mut s) = (0.0f64, 0.0f64);
        for i in skip * 2..n - skip * 2 {
            e += ((got[i] - want[i]) as f64).powi(2);
            s += (want[i] as f64).powi(2);
        }
        10.0 * (e / s).log10()
    }

    #[test]
    fn resampling_44k1_to_48k_keeps_the_top_octave_and_the_timing() {
        // A 15 kHz tone is where Hermite varispeed lost 1.3 dB; resampled
        // at load it must come out as the same tone at 48 kHz, in phase,
        // with the length scaled exactly.
        for hz in [1000.0, 15000.0] {
            let d = Decoded { sample_rate: 44100, pcm: sine(44100, hz, 44100) };
            let out = resample(d, 48000).unwrap();
            assert_eq!(out.sample_rate, 48000);
            assert_eq!(out.pcm.len(), 48000 * 2);
            let want = sine(48000, hz, 48000);
            // Edges carry the resampler's start-up and the file's hard stop.
            let e = err_db(&out.pcm, &want, 4096);
            assert!(e < -60.0, "{hz} Hz: error {e:.1} dB");
        }
    }

    #[test]
    fn a_file_at_the_engine_rate_is_not_touched() {
        let pcm = sine(48000, 1000.0, 4800);
        let out = resample(Decoded { sample_rate: 48000, pcm: pcm.clone() }, 48000).unwrap();
        assert_eq!(out.pcm, pcm);
        // Control: a different rate is converted.
        let out = resample(Decoded { sample_rate: 44100, pcm: sine(44100, 1000.0, 4410) }, 48000).unwrap();
        assert_eq!(out.pcm.len(), 4800 * 2);
    }
}
