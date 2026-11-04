//! File decode to mono f32 with symphonia, streamed a packet at a time.
//!
//! The formats are apps/audio-engine's (`mp3 flac wav aiff pcm aac alac
//! isomp4 ogg vorbis`). Channels are averaged to mono. What this decode does
//! NOT do that odj-audio's does: trim MP4 edit lists and iTunes gapless
//! priming (tens of milliseconds at the start), which moves no key, tuning
//! or tempo reading; frame times of such a file are that much late.
//!
//! A corrupt packet among good ones plays as silence for exactly its own
//! length, as odj-audio's `decode_stream` does, so every frame time after it
//! stays where the file puts it; a corrupt packet whose length cannot be
//! known fails the decode rather than shorten the stream.

use std::fs::File;
use std::path::Path;

use symphonia::core::codecs::audio::AudioDecoderOptions;
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, TrackType};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::units::TimeBase;

/// Decode `path`, calling `sink(sample_rate, mono_block)` for every decoded
/// packet. The rate is the first decoded packet's and may not change.
pub fn decode_mono<F: FnMut(u32, &[f32]) -> Result<(), String>>(path: &Path, mut sink: F) -> Result<DecodeInfo, String> {
    let file = File::open(path).map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let mut format = symphonia::default::get_probe()
        .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
        .map_err(|e| format!("unrecognized format in {}: {e}", path.display()))?;
    let track = format
        .default_track(TrackType::Audio)
        .ok_or_else(|| format!("no audio track in {}", path.display()))?;
    let track_id = track.id;
    let time_base = track.time_base;
    let params = track
        .codec_params
        .as_ref()
        .and_then(|p| p.audio())
        .ok_or_else(|| format!("no audio codec parameters in {}", path.display()))?;
    let codec_rate = params.sample_rate;
    let mut decoder = symphonia::default::get_codecs()
        .make_audio_decoder(params, &AudioDecoderOptions::default())
        .map_err(|e| format!("unsupported codec in {}: {e}", path.display()))?;
    let mut rate = 0u32;
    let mut frames = 0u64;
    let mut interleaved: Vec<f32> = Vec::new();
    let mut mono: Vec<f32> = Vec::new();
    let mut decoded_any = false;
    let mut corrupt_packets = 0u64;
    loop {
        let packet = match format.next_packet() {
            Ok(Some(p)) => p,
            Ok(None) => break,
            Err(SymError::ResetRequired) => {
                return Err(format!(
                    "the stream changes part-way through in {}; refusing a truncated decode",
                    path.display()
                ))
            }
            Err(e) => return Err(format!("read error in {}: {e}", path.display())),
        };
        if packet.track_id != track_id {
            continue;
        }
        let buf = match decoder.decode(&packet) {
            Ok(b) => b,
            // Silence for the packet's own length keeps the clock; an
            // unknown length fails rather than shorten the stream.
            Err(SymError::DecodeError(e)) => {
                let r = if rate != 0 { Some(rate) } else { codec_rate };
                let n = packet_frames(packet.dur.get(), time_base, r)
                    .ok_or_else(|| format!("corrupt packet of unknown length in {}: {e}", path.display()))?;
                if rate == 0 {
                    rate = r.unwrap_or(0);
                }
                mono.clear();
                mono.resize(n, 0.0);
                frames += n as u64;
                corrupt_packets += 1;
                sink(rate, &mono)?;
                continue;
            }
            Err(e) => return Err(format!("decode error in {}: {e}", path.display())),
        };
        let spec = buf.spec();
        let r = spec.rate();
        if rate != 0 && r != rate {
            return Err(format!(
                "the sample rate changes part-way through ({rate} Hz, then {r} Hz) in {}",
                path.display()
            ));
        }
        rate = r;
        let ch = spec.channels().count();
        if ch == 0 {
            return Err(format!("zero channels in {}", path.display()));
        }
        interleaved.resize(buf.samples_interleaved(), 0.0);
        buf.copy_to_slice_interleaved(&mut interleaved[..]);
        mono.clear();
        mono.extend(interleaved.chunks_exact(ch).map(|f| f.iter().sum::<f32>() / ch as f32));
        frames += mono.len() as u64;
        decoded_any = true;
        sink(rate, &mono)?;
    }
    if !decoded_any || rate == 0 {
        return Err(format!("no audio decoded from {}", path.display()));
    }
    Ok(DecodeInfo {
        sample_rate: rate,
        frames,
        corrupt_packets,
    })
}

/// Frames at `rate` spanned by a packet `dur` ticks long in `time_base`
/// (odj-audio's `packet_frames`): `None` when either is unknown, the length
/// is zero, or it claims more than 10 s, which no real packet does.
fn packet_frames(dur: u64, time_base: Option<TimeBase>, rate: Option<u32>) -> Option<usize> {
    let (tb, rate) = (time_base?, rate?);
    if dur == 0 || rate == 0 {
        return None;
    }
    let frames = u128::from(dur) * u128::from(tb.numer.get()) * u128::from(rate) / u128::from(tb.denom.get());
    (frames > 0 && frames <= u128::from(rate) * 10).then_some(frames as usize)
}

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct DecodeInfo {
    pub sample_rate: u32,
    /// Frames at `sample_rate`, the silence of corrupt packets included.
    pub frames: u64,
    /// Packets that failed to decode and were replaced by silence.
    pub corrupt_packets: u64,
}
