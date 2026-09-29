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
use symphonia::core::units::TimeBase;

use crate::engine::ErrorCode;
use crate::protocol::ProtoError;

pub struct Decoded {
    pub sample_rate: u32,
    /// Interleaved stereo.
    pub pcm: Vec<f32>,
}

/// Make room for `extra` more samples in `v`, growing it geometrically as
/// `Vec` does but never past `limit` samples unless the samples themselves
/// need more. A budget charged in frames is a budget of samples held, and a
/// plain `Vec` may hold nearly twice what it contains: this keeps what it
/// holds inside what the budget charged it.
pub(crate) fn reserve_within(v: &mut Vec<f32>, extra: usize, limit: usize) {
    let need = v.len().saturating_add(extra);
    if need > v.capacity() {
        let want = v.capacity().saturating_mul(2).min(limit).max(need);
        v.reserve_exact(want - v.len());
    }
}

/// Refuse `more` frames on top of the `held` samples when they would pass
/// `max_frames`, before anything is allocated for them.
fn check_room(held: usize, more: usize, max_frames: u64, path: &Path) -> Result<(), ProtoError> {
    let frames = (held / 2) as u64 + more as u64;
    if frames > max_frames {
        return Err(ProtoError::new(
            ErrorCode::Invalid,
            format!(
                "{} decodes past the {max_frames} frames left in the render's memory budget (one WAV file's worth)",
                path.display()
            ),
        ));
    }
    Ok(())
}

/// Decode `path`. Mono is duplicated to both sides; files with more than two
/// channels keep their first two (front left and right).
pub fn decode_file(path: &Path) -> Result<Decoded, ProtoError> {
    decode_file_within(path, u64::MAX)
}

/// Decode `path`, holding at most `max_frames` frames: a file longer than
/// that fails as soon as it passes the limit, before it is held whole, so a
/// caller with a memory budget never allocates past it.
pub fn decode_file_within(path: &Path, max_frames: u64) -> Result<Decoded, ProtoError> {
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
    let time_base = track.time_base;
    let params = track
        .codec_params
        .as_ref()
        .and_then(|p| p.audio())
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio codec parameters in {}", path.display())))?;
    let codec_rate = params.sample_rate;
    let mut decoder = symphonia::default::get_codecs()
        .make_audio_decoder(params, &AudioDecoderOptions::default())
        .map_err(|e| dec_err("unsupported codec in", &e))?;

    let mut pcm: Vec<f32> = Vec::new();
    // What the budget allows the samples to hold, spare capacity included.
    let limit = usize::try_from(max_frames.saturating_mul(2)).unwrap_or(usize::MAX);
    let mut scratch: Vec<f32> = Vec::new();
    let mut sample_rate = 0u32;
    // Silence stands in for a corrupt packet only around real audio: a file
    // none of whose packets decode is undecodable, not a silent track.
    let mut decoded_any = false;
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
            // A corrupt packet plays as silence for exactly its own length,
            // so everything after it (beatgrid, cues, loops) stays where the
            // file puts it. When that length cannot be known, the load fails
            // rather than come back shorter than the file.
            Err(SymError::DecodeError(e)) => {
                let rate = if sample_rate != 0 { Some(sample_rate) } else { codec_rate };
                let frames = packet_frames(packet.dur.get(), time_base, rate).ok_or_else(|| {
                    dec_err("corrupt packet of unknown length in", &e)
                })?;
                check_room(pcm.len(), frames, max_frames, path)?;
                reserve_within(&mut pcm, frames * 2, limit);
                pcm.resize(pcm.len() + frames * 2, 0.0);
                if sample_rate == 0 {
                    sample_rate = rate.unwrap_or(0);
                }
                continue;
            }
            Err(e) => return Err(dec_err("decode error in", &e)),
        };
        decoded_any = true;
        let spec = buf.spec();
        lock_rate(&mut sample_rate, spec.rate()).map_err(|m| ProtoError::new(ErrorCode::Decode, format!("{m} in {}", path.display())))?;
        let ch = spec.channels().count();
        if ch == 0 {
            return Err(ProtoError::new(ErrorCode::Decode, format!("zero channels in {}", path.display())));
        }
        scratch.resize(buf.samples_interleaved(), 0.0);
        buf.copy_to_slice_interleaved(&mut scratch[..]);
        check_room(pcm.len(), buf.frames(), max_frames, path)?;
        reserve_within(&mut pcm, buf.frames() * 2, limit);
        for frame in scratch.chunks_exact(ch) {
            let l = frame[0];
            let r = if ch == 1 { frame[0] } else { frame[1] };
            pcm.push(l);
            pcm.push(r);
        }
    }
    if !decoded_any {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no packet of {} decoded", path.display())));
    }
    if sample_rate == 0 || pcm.is_empty() {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    Ok(Decoded { sample_rate, pcm })
}

/// The first rate decoded is the track's rate. A later buffer at another
/// rate is refused: relabeling the audio before it would play that part at
/// the wrong speed and move its beatgrid, cues and loops.
fn lock_rate(locked: &mut u32, rate: u32) -> Result<(), String> {
    if *locked != 0 && rate != *locked {
        return Err(format!("the sample rate changes part-way through ({} Hz, then {rate} Hz)", *locked));
    }
    *locked = rate;
    Ok(())
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

/// Frames at `rate` spanned by a packet `dur` ticks long in `time_base`.
/// None when either is unknown, the packet claims no length, or the length
/// is implausible for one packet (over 10 s), so a bogus header cannot make
/// the decode allocate unbounded silence.
fn packet_frames(dur: u64, time_base: Option<TimeBase>, rate: Option<u32>) -> Option<usize> {
    let (tb, rate) = (time_base?, rate?);
    if dur == 0 || rate == 0 {
        return None;
    }
    let frames = u128::from(dur) * u128::from(tb.numer.get()) * u128::from(rate) / u128::from(tb.denom.get());
    (frames > 0 && frames <= u128::from(rate) * 10).then_some(frames as usize)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_first_decoded_rate_is_the_tracks() {
        let mut rate = 0;
        lock_rate(&mut rate, 44100).unwrap();
        assert_eq!(rate, 44100);
        // Same rate again is fine; a change part-way through is refused.
        lock_rate(&mut rate, 44100).unwrap();
        let e = lock_rate(&mut rate, 48000).unwrap_err();
        assert!(e.contains("44100 Hz, then 48000 Hz"), "{e}");
        assert_eq!(rate, 44100, "a refused rate must not relabel the track");
    }

    fn tb(numer: u32, denom: u32) -> Option<TimeBase> {
        TimeBase::try_new(numer, denom)
    }

    #[test]
    fn a_corrupt_packet_keeps_its_length_when_the_length_is_known() {
        // An MP3 frame: 1152 ticks of 1/44100 s is 1152 frames at 44.1 kHz.
        assert_eq!(packet_frames(1152, tb(1, 44100), Some(44100)), Some(1152));
        // A millisecond time base (e.g. Matroska) still lands on frames.
        assert_eq!(packet_frames(24, tb(1, 1000), Some(48000)), Some(1152));
        // Unknown length: the caller fails the load instead.
        assert_eq!(packet_frames(0, tb(1, 44100), Some(44100)), None);
        assert_eq!(packet_frames(1152, None, Some(44100)), None);
        assert_eq!(packet_frames(1152, tb(1, 44100), None), None);
        // A bogus header claiming an hour-long packet is refused too.
        assert_eq!(packet_frames(3600, tb(1, 1), Some(44100)), None);
    }

    #[test]
    fn a_reset_part_way_through_is_an_error_not_the_end() {
        assert!(end_or_packet::<()>(Err(SymError::ResetRequired)).is_err());
        // Control: the real end of the stream still ends it cleanly.
        assert_eq!(end_or_packet::<()>(Ok(None)), Ok(None));
        assert_eq!(end_or_packet(Ok(Some(7))), Ok(Some(7)));
    }
}
