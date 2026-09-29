//! Whole-file decode to interleaved stereo f32 at the file's own sample rate.
//!
//! Runs on a control or worker thread, never the audio thread. Rate conversion
//! to the engine's rate happens at play time inside the deck's interpolator,
//! together with tempo, so a track is never resampled twice.

use std::fs::File;
use std::path::Path;

use symphonia::core::codecs::audio::AudioDecoderOptions;
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, TrackType};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;

use crate::engine::ErrorCode;
use crate::protocol::ProtoError;

pub struct Decoded {
    pub sample_rate: u32,
    /// Interleaved stereo.
    pub pcm: Vec<f32>,
}

/// Decode `path`. Mono is duplicated to both sides; files with more than two
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
        let packet = match end_or_packet(format.next_packet()) {
            Ok(Some(p)) => p,
            Ok(None) => break,
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

/// `Ok(None)` is the end of the stream. A reset part-way through (a chained
/// stream whose tracks or codec change) is an ERROR, not the end: reading it
/// as the end would load a truncated track as a success, and one decoder at
/// one sample rate cannot follow the change anyway.
fn end_or_packet<T>(next: Result<Option<T>, SymError>) -> Result<Option<T>, String> {
    match next {
        Ok(p) => Ok(p),
        Err(SymError::ResetRequired) => {
            Err("the stream changes part-way through (chained stream); refusing a truncated decode".into())
        }
        Err(e) => Err(e.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_reset_part_way_through_is_an_error_not_the_end() {
        assert!(end_or_packet::<()>(Err(SymError::ResetRequired)).is_err());
        // Control: the real end of the stream still ends it cleanly.
        assert_eq!(end_or_packet::<()>(Ok(None)), Ok(None));
        assert_eq!(end_or_packet(Ok(Some(7))), Ok(Some(7)));
    }
}
