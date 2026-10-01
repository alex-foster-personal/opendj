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
use symphonia::core::units::TimeBase;

use rubato::{FftFixedIn, Resampler};

use crate::engine::ErrorCode;
use crate::protocol::ProtoError;

pub struct Decoded {
    pub sample_rate: u32,
    /// Interleaved stereo.
    pub pcm: Vec<f32>,
    /// The file the samples were read from, as opened.
    pub source: Option<SourceId>,
}

/// A file as the volume knows it, taken from the open file itself, so it
/// still names what was read after its path is renamed or replaced: device
/// and inode on Unix; elsewhere the open handle (volume serial and file
/// index), kept open for as long as the id is held. On Unix a file deleted
/// since may have its inode reused, which can only make a later comparison
/// say "the same file" of a new one: a refusal, never an overwrite.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct SourceId(#[cfg(unix)] (u64, u64), #[cfg(not(unix))] std::sync::Arc<same_file::Handle>);

impl SourceId {
    pub fn of(file: &File) -> std::io::Result<SourceId> {
        #[cfg(unix)]
        {
            use std::os::unix::fs::MetadataExt;
            let m = file.metadata()?;
            Ok(SourceId((m.dev(), m.ino())))
        }
        #[cfg(not(unix))]
        {
            Ok(SourceId(std::sync::Arc::new(same_file::Handle::from_file(file.try_clone()?)?)))
        }
    }
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

/// Decode an opened file within `max_frames` and, when `sample_rate` is
/// given and differs from the file's, resample it; the resampled track must
/// fit `max_frames` too.
pub fn decode_open_at(file: File, path: &Path, sample_rate: Option<u32>, max_frames: u64) -> Result<Decoded, ProtoError> {
    let d = decode_open_within(file, path, max_frames)?;
    let Some(to) = sample_rate else { return Ok(d) };
    let d = resample(d, to)
        .map_err(|e| ProtoError::new(ErrorCode::Decode, format!("cannot resample {}: {e}", path.display())))?;
    check_room(d.pcm.len(), 0, max_frames, path)?;
    Ok(d)
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
        for (l, r) in out[0][from_i..got].iter().zip(&out[1][from_i..got]) {
            if pcm.len() == want * 2 {
                break;
            }
            pcm.push(*l);
            pcm.push(*r);
        }
    }
    Ok(Decoded { sample_rate: to, pcm, source: d.source })
}

/// Decode `path` at its own rate. Mono is duplicated to both sides; files with more than two
/// channels keep their first two (front left and right).
pub fn decode_file(path: &Path) -> Result<Decoded, ProtoError> {
    decode_file_within(path, u64::MAX)
}

/// Decode `path`, holding at most `max_frames` frames: a file longer than
/// that fails as soon as it passes the limit, before it is held whole, so a
/// caller with a memory budget never allocates past it.
pub fn decode_file_within(path: &Path, max_frames: u64) -> Result<Decoded, ProtoError> {
    decode_open_within(open(path)?, path, max_frames)
}

/// Open `path` for decoding.
pub fn open(path: &Path) -> Result<File, ProtoError> {
    File::open(path).map_err(|e| ProtoError::new(ErrorCode::Io, format!("cannot open {}: {e}", path.display())))
}

/// Decode `file`, already opened from `path` (which names it in errors and
/// gives the format hint), as `decode_file_within` does: a caller that keyed
/// the file by its open handle decodes exactly the file it keyed.
pub fn decode_open_within(mut file: File, path: &Path, max_frames: u64) -> Result<Decoded, ProtoError> {
    // Taken from the file the samples come from, not from its path again.
    let source = SourceId::of(&file).ok();
    // symphonia does not apply an MP4 edit list, so its priming frames are
    // read here and trimmed after the decode (src/mp4edit.rs).
    let edit = mp4_edit(&mut file, path)?;
    let lead = leading_tag_bytes(&mut file, path)?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let dec_err = |what: &str, e: &dyn std::fmt::Display| {
        ProtoError::new(ErrorCode::Decode, format!("{what} {}: {e}", path.display()))
    };
    let mut format = probe_for(lead)
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
    if let Some(e) = edit.and_then(|e| e.at(sample_rate)) {
        crate::mp4edit::apply_stereo(&mut pcm, e);
    }
    Ok(Decoded { sample_rate, pcm, source })
}

/// The MP4 edit of `file`, leaving it positioned at its start for the decoder.
/// Best effort: a stream that is not a regular file (a pipe cannot be read
/// twice) or a header that cannot be read yields no edit, never a failed load.
fn mp4_edit(file: &mut File, path: &Path) -> Result<Option<crate::mp4edit::RawEdit>, ProtoError> {
    use std::io::{Seek, SeekFrom};
    if !file.metadata().map(|m| m.is_file()).unwrap_or(false) {
        return Ok(None);
    }
    let edit = crate::mp4edit::read_raw_edit(file).ok().flatten();
    file.seek(SeekFrom::Start(0))
        .map_err(|e| ProtoError::new(ErrorCode::Io, format!("cannot read {}: {e}", path.display())))?;
    Ok(edit)
}

/// How far symphonia scans for a format marker when no tag is in front: its
/// own default.
const PROBE_DEPTH: u64 = 1 << 20;

/// Bytes of ID3v2 tag at the front of `file` (several stacked tags summed),
/// leaving it positioned at its start. symphonia's probe counts a leading tag
/// against its 1 MiB scan limit, so an MP3 with a few MB of embedded artwork
/// failed to open with "no suitable format reader found" while ffmpeg read it
/// (found by the Platinum Notes thread on a real 320k MP3, Thu 1 Oct 2026).
/// Best effort, like the MP4 edit: anything unreadable is no tag.
fn leading_tag_bytes(file: &mut File, path: &Path) -> Result<u64, ProtoError> {
    use std::io::{Read, Seek, SeekFrom};
    if !file.metadata().map(|m| m.is_file()).unwrap_or(false) {
        return Ok(0);
    }
    let mut lead = 0u64;
    // A file can carry more than one tag back to back; a few is plenty.
    for _ in 0..8 {
        let mut h = [0u8; 10];
        if file.seek(SeekFrom::Start(lead)).is_err() || file.read_exact(&mut h).is_err() {
            break;
        }
        let Some(n) = id3v2_tag_len(&h) else { break };
        lead += n;
    }
    file.seek(SeekFrom::Start(0))
        .map_err(|e| ProtoError::new(ErrorCode::Io, format!("cannot read {}: {e}", path.display())))?;
    Ok(lead)
}

/// The full length of an ID3v2 tag from its 10-byte header (header, body and
/// footer), or `None` when `h` is not one.
fn id3v2_tag_len(h: &[u8; 10]) -> Option<u64> {
    if &h[..3] != b"ID3" || h[3] == 0xff || h[4] == 0xff || h[6..].iter().any(|b| b & 0x80 != 0) {
        return None;
    }
    let body = h[6..].iter().fold(0u64, |acc, &b| (acc << 7) | u64::from(b));
    let footer = if h[5] & 0x10 != 0 { 10 } else { 0 };
    Some(10 + body + footer)
}

/// A probe that scans past `lead` bytes of leading tag plus its usual depth.
/// The shared default probe is used when the tag is small, so a file that is
/// not audio costs no deeper a scan than before.
enum ProbeFor {
    Default(&'static symphonia::core::formats::probe::Probe),
    Deep(Box<symphonia::core::formats::probe::Probe>),
}

impl std::ops::Deref for ProbeFor {
    type Target = symphonia::core::formats::probe::Probe;
    fn deref(&self) -> &Self::Target {
        match self {
            ProbeFor::Default(p) => p,
            ProbeFor::Deep(p) => p,
        }
    }
}

fn probe_for(lead: u64) -> ProbeFor {
    use symphonia::core::formats::probe::{Probe, ProbeOptions};
    // Under half the default depth, the tag leaves the default scan room.
    if lead <= PROBE_DEPTH / 2 {
        return ProbeFor::Default(symphonia::default::get_probe());
    }
    let depth = u32::try_from(lead + PROBE_DEPTH).unwrap_or(u32::MAX);
    let mut p = Probe::new_with_options(&ProbeOptions { max_probe_depth: depth, ..Default::default() });
    symphonia::default::register_enabled_formats(&mut p);
    ProbeFor::Deep(Box::new(p))
}

/// What a file's container states about its length, read without decoding.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Probe {
    pub sample_rate: Option<u32>,
    /// Playable frames (encoder delay and padding excluded), when stated.
    pub frames: Option<u64>,
    /// Leading encoder frames the reader skips, when stated.
    pub delay: Option<u32>,
    /// Trailing encoder frames the reader skips, when stated.
    pub padding: Option<u32>,
}

/// Read `path`'s header: rate and playable length when the container states
/// them. Never an estimate: an MP3 with no Xing/Info header states no length,
/// and `frames` is then `None` for the caller to decode and count.
pub fn probe_file(path: &Path) -> Result<Probe, ProtoError> {
    let mut file = open(path)?;
    let edit = mp4_edit(&mut file, path)?;
    let lead = leading_tag_bytes(&mut file, path)?;
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let format = probe_for(lead)
        .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
        .map_err(|e| ProtoError::new(ErrorCode::Decode, format!("unrecognized format in {}: {e}", path.display())))?;
    let track = format
        .default_track(TrackType::Audio)
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio track in {}", path.display())))?;
    let sample_rate = track.codec_params.as_ref().and_then(|p| p.audio()).and_then(|a| a.sample_rate);
    let mut frames = track.num_frames.filter(|&n| n > 0);
    let mut delay = track.delay;
    // An MP4 edit states the playable length; symphonia's count includes the
    // priming and padding frames the edit excludes.
    if let Some(e) = edit.zip(sample_rate).and_then(|(e, r)| e.at(r)) {
        delay = Some(e.skip as u32);
        if let Some(keep) = e.keep {
            frames = Some(keep);
        }
    }
    Ok(Probe { sample_rate, frames, delay, padding: track.padding })
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
    fn an_id3v2_header_gives_the_whole_tag_length() {
        // Body 0x7f syncsafe bytes: 2^28 - 1; here 1 << 7 = 128 bytes.
        let h = *b"ID3\x03\x00\x00\x00\x00\x01\x00";
        assert_eq!(id3v2_tag_len(&h), Some(10 + 128));
        // The footer flag adds another 10 bytes.
        let f = *b"ID3\x04\x00\x10\x00\x00\x01\x00";
        assert_eq!(id3v2_tag_len(&f), Some(10 + 128 + 10));
        // Not a tag: wrong magic, or a size byte with its high bit set.
        assert_eq!(id3v2_tag_len(b"RIFF\x00\x00\x00\x00\x01\x00"), None);
        assert_eq!(id3v2_tag_len(b"ID3\x03\x00\x00\x80\x00\x01\x00"), None);
    }

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
            let d = Decoded { sample_rate: 44100, pcm: sine(44100, hz, 44100), source: None };
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
        let out = resample(Decoded { sample_rate: 48000, pcm: pcm.clone(), source: None }, 48000).unwrap();
        assert_eq!(out.pcm, pcm);
        // Control: a different rate is converted.
        let out = resample(Decoded { sample_rate: 44100, pcm: sine(44100, 1000.0, 4410), source: None }, 48000).unwrap();
        assert_eq!(out.pcm.len(), 4800 * 2);
    }
}
