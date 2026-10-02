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
use std::io::{Seek, SeekFrom, Write};
use std::path::Path;

use symphonia::core::codecs::audio::{AudioDecoder, AudioDecoderOptions};
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, FormatReader, TrackType};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;
use symphonia::core::units::TimeBase;

use rubato::{FftFixedIn, Resampler};

use crate::engine::ErrorCode;
use crate::protocol::ProtoError;
use crate::{edit_list, wav};

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
pub fn decode_open_within(file: File, path: &Path, max_frames: u64) -> Result<Decoded, ProtoError> {
    let Opened { mut format, mut decoder, track_id, time_base, codec_rate, source, .. } = open_decoder(file, path)?;
    let dec_err = |what: &str, e: &dyn std::fmt::Display| {
        ProtoError::new(ErrorCode::Decode, format!("{what} {}: {e}", path.display()))
    };

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
    Ok(Decoded { sample_rate, pcm, source })
}

/// A file probed and ready to decode: its demuxer, the decoder for its
/// default audio track, and what the codec parameters say before the first
/// packet (rate and channel count, either of which may be unknown).
pub(crate) struct Opened {
    pub(crate) format: Box<dyn FormatReader>,
    pub(crate) decoder: Box<dyn AudioDecoder>,
    pub(crate) track_id: u32,
    pub(crate) time_base: Option<TimeBase>,
    pub(crate) codec_rate: Option<u32>,
    pub(crate) codec_channels: Option<usize>,
    pub(crate) source: Option<SourceId>,
}

/// Probe `file`, opened from `path` (which names it in errors and gives the
/// format hint), and make the decoder for its default audio track. Shared by
/// the whole-file decode a deck loads with and the streaming
/// [`decode_to_wav`], so both read a file the same way.
pub(crate) fn open_decoder(file: File, path: &Path) -> Result<Opened, ProtoError> {
    // Taken from the file the samples come from, not from its path again.
    let source = SourceId::of(&file).ok();
    let mss = MediaSourceStream::new(Box::new(file), Default::default());
    let mut hint = Hint::new();
    if let Some(ext) = path.extension().and_then(|e| e.to_str()) {
        hint.with_extension(ext);
    }
    let dec_err = |what: &str, e: &dyn std::fmt::Display| {
        ProtoError::new(ErrorCode::Decode, format!("{what} {}: {e}", path.display()))
    };
    let format = symphonia::default::get_probe()
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
    let codec_channels = params.channels.as_ref().map(|c| c.count()).filter(|&n| n > 0);
    let decoder = symphonia::default::get_codecs()
        .make_audio_decoder(params, &AudioDecoderOptions::default())
        .map_err(|e| dec_err("unsupported codec in", &e))?;
    Ok(Opened { format, decoder, track_id, time_base, codec_rate, codec_channels, source })
}

/// What [`decode_to_wav`] wrote: the source's own rate and channel count,
/// how many frames, and what the MP4 edit list (if any) trimmed.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WavWritten {
    pub sample_rate: u32,
    pub channels: u16,
    pub frames: u64,
    /// Encoder priming trimmed from the front (MP4 edit list `media_time`).
    pub trimmed_start: u64,
    /// Frames of whole packets dropped past the edit's end.
    pub dropped_end: u64,
    /// `none`, `applied`, or `ignored: <why>` (decoded untrimmed).
    pub edit: String,
}

/// Which decoded frames `decode_to_wav` keeps, per the file's edit list.
struct Trim {
    edit: Option<edit_list::Edit>,
    /// Frames decoded so far, before trimming (the decoder's timeline).
    pos: u64,
    trimmed_start: u64,
    dropped_end: u64,
}

impl Trim {
    /// Of the next `n` decoded frames, (first kept, how many kept). The edit
    /// list is read once, when the track's rate is first known.
    fn window(&mut self, n: u64, path: &Path, track_id: u32, rate: u32) -> (u64, u64) {
        let edit = self.edit.get_or_insert_with(|| edit_list::read(path, track_id, rate));
        let at = self.pos;
        self.pos += n;
        let edit_list::Edit::Apply { skip, keep } = *edit else { return (0, n) };
        // A packet that starts at or after the edit's end is dropped whole;
        // one that straddles it is kept whole, as ffmpeg keeps it.
        if keep.is_some_and(|k| at >= skip + k) {
            self.dropped_end += n;
            return (0, 0);
        }
        let from = skip.saturating_sub(at).min(n);
        self.trimmed_start += from;
        (from, n - from)
    }

    fn describe(&self) -> String {
        match &self.edit {
            None | Some(edit_list::Edit::None) => "none".into(),
            Some(edit_list::Edit::Apply { .. }) => "applied".into(),
            Some(edit_list::Edit::Ignored(why)) => format!("ignored: {why}"),
        }
    }
}

/// Decode `path` into `out` as a 32-bit float WAV at the file's own sample
/// rate and channel count, one packet at a time: memory stays at one packet
/// whatever the track's length, unlike [`decode_file`], which holds the whole
/// track. This is what lets a stems or vocals worker in the installed app,
/// which ships no ffmpeg, read an MP3 (`odj-audio decode`).
///
/// Packets are read as a deck reads them ([`open_decoder`]): MP3 encoder
/// delay and padding are trimmed (gapless), a corrupt packet of known length
/// becomes silence of that length, and a file none of whose packets decode,
/// whose rate or channel count changes part-way, or which is chained, is an
/// error. Unlike the deck, an MP4's edit list is applied
/// ([`crate::edit_list`]), so an M4A starts where ffmpeg starts it rather
/// than 1024 priming frames early. `out` must be seekable: the header is
/// written last, once the length is known. On an error `out` holds a partial
/// file the caller discards.
pub fn decode_to_wav<W: Write + Seek>(path: &Path, out: &mut W) -> Result<WavWritten, ProtoError> {
    let Opened { mut format, mut decoder, track_id, time_base, codec_rate, codec_channels, .. } =
        open_decoder(open(path)?, path)?;
    let dec_err = |what: &str, e: &dyn std::fmt::Display| {
        ProtoError::new(ErrorCode::Decode, format!("{what} {}: {e}", path.display()))
    };
    let io_err = |e: std::io::Error| ProtoError::new(ErrorCode::Io, format!("cannot write the WAV of {}: {e}", path.display()));
    // The header's place, filled in at the end.
    out.write_all(&[0u8; 44]).map_err(io_err)?;
    let mut sample_rate = 0u32;
    let mut channels = 0usize;
    let mut frames = 0u64;
    let mut decoded_any = false;
    let mut trim = Trim { edit: None, pos: 0, trimmed_start: 0, dropped_end: 0 };
    let mut scratch: Vec<f32> = Vec::new();
    let mut bytes: Vec<u8> = Vec::new();
    // The most frames a WAV of this channel count can hold (32-bit sizes).
    let room = |ch: usize| (u64::from(u32::MAX) - 36) / (ch as u64 * 4);
    let too_long = || ProtoError::new(ErrorCode::Invalid, format!("{} is too long for one WAV file (4 GiB)", path.display()));
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
            Err(SymError::DecodeError(e)) => {
                let rate = if sample_rate != 0 { Some(sample_rate) } else { codec_rate };
                let n = packet_frames(packet.dur.get(), time_base, rate)
                    .ok_or_else(|| dec_err("corrupt packet of unknown length in", &e))?;
                let ch = if channels != 0 { channels } else { codec_channels.ok_or_else(|| dec_err("corrupt packet before the channel count is known in", &e))? };
                if sample_rate == 0 {
                    sample_rate = rate.unwrap_or(0);
                }
                channels = ch;
                let (_, take) = trim.window(n as u64, path, track_id, sample_rate);
                if frames + take > room(ch) {
                    return Err(too_long());
                }
                bytes.clear();
                bytes.resize(take as usize * ch * 4, 0);
                out.write_all(&bytes).map_err(io_err)?;
                frames += take;
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
        if channels != 0 && ch != channels {
            return Err(ProtoError::new(
                ErrorCode::Decode,
                format!("the channel count changes part-way through ({channels}, then {ch}) in {}", path.display()),
            ));
        }
        channels = ch;
        let (from, take) = trim.window(buf.frames() as u64, path, track_id, sample_rate);
        if frames + take > room(ch) {
            return Err(too_long());
        }
        if take == 0 {
            continue;
        }
        scratch.resize(buf.samples_interleaved(), 0.0);
        buf.copy_to_slice_interleaved(&mut scratch[..]);
        let kept = &scratch[from as usize * ch..(from + take) as usize * ch];
        bytes.clear();
        bytes.reserve(kept.len() * 4);
        for s in kept {
            bytes.extend_from_slice(&s.to_le_bytes());
        }
        out.write_all(&bytes).map_err(io_err)?;
        frames += take;
    }
    if !decoded_any {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no packet of {} decoded", path.display())));
    }
    if sample_rate == 0 || channels == 0 || frames == 0 {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    let ch16 = u16::try_from(channels).map_err(|_| dec_err("too many channels in", &channels))?;
    // `room` kept the data under the 32-bit limit.
    let data_bytes = (frames * channels as u64 * 4) as u32;
    out.seek(SeekFrom::Start(0)).map_err(io_err)?;
    wav::write_f32_header(out, ch16, sample_rate, data_bytes).map_err(io_err)?;
    out.flush().map_err(io_err)?;
    Ok(WavWritten {
        sample_rate,
        channels: ch16,
        frames,
        trimmed_start: trim.trimmed_start,
        dropped_end: trim.dropped_end,
        edit: trim.describe(),
    })
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

    /// The windows `decode_to_wav` keeps for 1024-frame packets under `edit`.
    fn windows(edit: edit_list::Edit, packets: usize) -> (Vec<(u64, u64)>, Trim) {
        let mut t = Trim { edit: Some(edit), pos: 0, trimmed_start: 0, dropped_end: 0 };
        let w = (0..packets).map(|_| t.window(1024, Path::new("unused"), 1, 44100)).collect();
        (w, t)
    }

    #[test]
    fn the_edit_list_trims_the_front_and_drops_whole_packets_past_its_end() {
        // ffmpeg: skip 1024 (one packet), then 1500 frames; the packet that
        // straddles the end (starting at 2048 < 2524) is kept whole, the one
        // starting at 3072 is dropped.
        let (w, t) = windows(edit_list::Edit::Apply { skip: 1024, keep: Some(1500) }, 4);
        assert_eq!(w, vec![(1024, 0), (0, 1024), (0, 1024), (0, 0)]);
        assert_eq!((t.trimmed_start, t.dropped_end, t.describe().as_str()), (1024, 1024, "applied"));
        // A skip inside a packet keeps its tail.
        let (w, _) = windows(edit_list::Edit::Apply { skip: 2112, keep: None }, 3);
        assert_eq!(w, vec![(1024, 0), (1024, 0), (64, 960)]);
        // Controls: no edit list and an ignored one keep every frame.
        for e in [edit_list::Edit::None, edit_list::Edit::Ignored("2 edits".into())] {
            let (w, t) = windows(e, 3);
            assert_eq!(w, vec![(0, 1024); 3]);
            assert_eq!((t.trimmed_start, t.dropped_end), (0, 0));
        }
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
