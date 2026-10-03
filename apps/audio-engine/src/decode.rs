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
use crate::wav;

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
    decode_progressive(file, path, sample_rate, max_frames, 0, |_| {})
}

/// Decode `path` and resample it to `sample_rate` when the file's rate differs.
pub fn decode_at(path: &Path, sample_rate: u32) -> Result<Decoded, ProtoError> {
    resample(decode_file(path)?, sample_rate)
        .map_err(|e| ProtoError::new(ErrorCode::Decode, format!("cannot resample {}: {e}", path.display())))
}

/// Input chunk for the FFT resampler. Larger chunks cost memory, not quality.
/// Part of the analysis decode fingerprint (`RESAMPLER` in
/// `apps/analysis/pcm_fingerprint.py`): changing it, the sub-chunk count, or
/// the resampler changes every stored fingerprint, so it moves only with a
/// spec change there.
const RESAMPLE_CHUNK: usize = 4096;

/// Convert interleaved stereo to `to` Hz. The output has exactly
/// `round(frames * to / from)` frames, time-aligned with the input: the
/// resampler's own delay is dropped from the front and its tail flushed.
pub fn resample(d: Decoded, to: u32) -> Result<Decoded, String> {
    if d.sample_rate == to {
        return Ok(d);
    }
    let mut r = StreamResampler::new(d.sample_rate, to)?;
    let mut pcm = Vec::with_capacity(r.frames_for(d.pcm.len() as u64 / 2) as usize * 2);
    r.push(&d.pcm, &mut pcm)?;
    r.finish(&mut pcm)?;
    Ok(Decoded { sample_rate: to, pcm, source: d.source })
}

/// `resample` fed a block at a time, as a decode produces them. Its output
/// is `resample`'s sample for sample (it is what `resample` runs), and every
/// frame it has appended is final: later input only appends, so the start
/// of a track can be played while the rest is still being converted.
pub struct StreamResampler {
    r: FftFixedIn<f32>,
    from: u32,
    to: u32,
    inp: [Vec<f32>; 2],
    /// Frames of `inp` filled.
    fill: usize,
    out: [Vec<f32>; 2],
    /// Output frames of the resampler's own delay still to drop.
    skip: usize,
    /// Input frames pushed so far.
    frames_in: u64,
}

impl StreamResampler {
    pub fn new(from: u32, to: u32) -> Result<StreamResampler, String> {
        let r = FftFixedIn::<f32>::new(from as usize, to as usize, RESAMPLE_CHUNK, 2, 2).map_err(|e| e.to_string())?;
        let skip = r.output_delay();
        let out_max = r.output_frames_max();
        Ok(StreamResampler {
            r,
            from,
            to,
            inp: [vec![0.0; RESAMPLE_CHUNK], vec![0.0; RESAMPLE_CHUNK]],
            fill: 0,
            out: [vec![0.0; out_max], vec![0.0; out_max]],
            skip,
            frames_in: 0,
        })
    }

    /// Output frames for `frames` input frames: `round(frames * to / from)`.
    pub fn frames_for(&self, frames: u64) -> u64 {
        ((frames as u128 * self.to as u128 + self.from as u128 / 2) / self.from as u128) as u64
    }

    /// Feed interleaved stereo, appending what is ready to `pcm`.
    pub fn push(&mut self, block: &[f32], pcm: &mut Vec<f32>) -> Result<(), String> {
        for f in block.chunks_exact(2) {
            self.inp[0][self.fill] = f[0];
            self.inp[1][self.fill] = f[1];
            self.fill += 1;
            if self.fill == RESAMPLE_CHUNK {
                self.chunk(pcm, usize::MAX)?;
            }
        }
        self.frames_in += (block.len() / 2) as u64;
        Ok(())
    }

    /// Flush the tail: zero-pad the last chunk and run on until the output
    /// holds exactly `frames_for` every frame pushed.
    pub fn finish(mut self, pcm: &mut Vec<f32>) -> Result<(), String> {
        let want = self.frames_for(self.frames_in) as usize * 2;
        while pcm.len() < want {
            for ch in self.inp.iter_mut() {
                ch[self.fill..].fill(0.0);
            }
            self.chunk(pcm, want)?;
        }
        pcm.truncate(want);
        Ok(())
    }

    /// Convert the input chunk, appending at most up to `limit` samples.
    fn chunk(&mut self, pcm: &mut Vec<f32>, limit: usize) -> Result<(), String> {
        let (_, got) = self.r.process_into_buffer(&self.inp, &mut self.out, None).map_err(|e| e.to_string())?;
        self.fill = 0;
        let from = self.skip.min(got);
        self.skip -= from;
        for (l, r) in self.out[0][from..got].iter().zip(&self.out[1][from..got]) {
            if pcm.len() >= limit {
                break;
            }
            pcm.push(*l);
            pcm.push(*r);
        }
        Ok(())
    }
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
    let mut pcm: Vec<f32> = Vec::new();
    // What the budget allows the samples to hold, spare capacity included.
    let limit = usize::try_from(max_frames.saturating_mul(2)).unwrap_or(usize::MAX);
    let (sample_rate, source) = decode_stream(file, path, |_, _, block| {
        check_room(pcm.len(), block.len() / 2, max_frames, path)?;
        reserve_within(&mut pcm, block.len(), limit);
        pcm.extend_from_slice(block);
        Ok(())
    })?;
    if pcm.is_empty() {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    Ok(Decoded { sample_rate, pcm, source })
}

/// The first part of a track, decoded and converted while the rest still is
/// (`decode_progressive`).
pub struct Head {
    pub sample_rate: u32,
    /// Interleaved stereo: exactly the first frames of the whole decode.
    pub pcm: Vec<f32>,
    /// The track's length in frames at `sample_rate` as its container states
    /// it, when it does: exact for WAV, FLAC, MP4 and an MP3 with a Xing/Info
    /// or VBRI header, estimated from the file size for a CBR MP3 without
    /// one. The whole decode is the truth; this only bounds seeks meanwhile.
    pub frames: Option<u64>,
}

/// Share of a track its head holds at least: `1 / HEAD_SHARE` of the length
/// the container states. The rest of the file must be decoded before a deck
/// started at load plays the head out, at up to double speed; the decode
/// runs 300-600x real time on the machines measured, so a head of 1/40 of
/// the track leaves it several times the room it needs. For a 6-minute track
/// this is 9 s; for a 1-hour mix, 90 s (about 0.2 s of decoding).
pub const HEAD_SHARE: u64 = 40;

/// Decode as `decode_open_at` does, handing `on_head` a copy of the head (at
/// least `head_frames` frames at the output rate, and at least `1 /
/// HEAD_SHARE` of the stated length) as soon as it is decoded and
/// converted, then returning the whole track. A `head_frames` of 0 hands
/// over no head. One pass: the head is a prefix
/// of the result, sample for sample, so a deck can start on the head and
/// swap to the whole track without a seam. A track shorter than the head
/// never calls `on_head`; it is decoded whole by then anyway.
///
/// The conversion runs packet by packet behind the decoder, so the file's own
/// rate is never held whole beside the converted copy, as it was when the
/// whole file was decoded first and then converted.
pub fn decode_progressive(
    file: File,
    path: &Path,
    sample_rate: Option<u32>,
    max_frames: u64,
    head_frames: usize,
    on_head: impl FnMut(Head),
) -> Result<Decoded, ProtoError> {
    decode_progressive_metered(file, path, sample_rate, max_frames, head_frames, on_head, |_| {})
}

/// As `decode_progressive`, also telling `on_grow` the bytes the decode
/// buffer has allocated each time that grows, so a caller can count a
/// buffer whose final size the file does not state while it is filled.
pub fn decode_progressive_metered(
    file: File,
    path: &Path,
    sample_rate: Option<u32>,
    max_frames: u64,
    head_frames: usize,
    mut on_head: impl FnMut(Head),
    mut on_grow: impl FnMut(usize),
) -> Result<Decoded, ProtoError> {
    let mut grown = 0usize;
    let mut grew = |pcm: &Vec<f32>| {
        let bytes = pcm.capacity() * std::mem::size_of::<f32>();
        if bytes > grown {
            grown = bytes;
            on_grow(bytes);
        }
    };
    let limit = usize::try_from(max_frames.saturating_mul(2)).unwrap_or(usize::MAX);
    let resample_err = |e: String| ProtoError::new(ErrorCode::Decode, format!("cannot resample {}: {e}", path.display()));
    let mut pcm: Vec<f32> = Vec::new();
    let mut conv: Option<StreamResampler> = None;
    let stated: std::cell::Cell<Option<u64>> = std::cell::Cell::new(None);
    let mut out_rate = 0u32;
    let mut head_sent = false;
    let (rate, source) = decode_stream_with(
        file,
        path,
        |frames| stated.set(frames),
        |rate, _, block| {
            if out_rate == 0 {
                out_rate = sample_rate.unwrap_or(rate);
                if out_rate != rate {
                    conv = Some(StreamResampler::new(rate, out_rate).map_err(resample_err)?);
                }
            }
            match conv.as_mut() {
                Some(c) => {
                    // At most this much output can follow the block (the
                    // converter lags its input), bounded before it is held.
                    let more = (c.frames_for(c.frames_in + (block.len() / 2) as u64) as usize).saturating_sub(pcm.len() / 2);
                    check_room(pcm.len(), more, max_frames, path)?;
                    reserve_within(&mut pcm, more * 2, limit);
                    c.push(block, &mut pcm).map_err(resample_err)?;
                }
                None => {
                    check_room(pcm.len(), block.len() / 2, max_frames, path)?;
                    reserve_within(&mut pcm, block.len(), limit);
                    pcm.extend_from_slice(block);
                }
            }
            grew(&pcm);
            let frames = || {
                stated.get().map(|n| match conv.as_ref() {
                    Some(c) => c.frames_for(n),
                    None => n,
                })
            };
            let head = head_frames.max(frames().map_or(0, |n| usize::try_from(n / HEAD_SHARE).unwrap_or(usize::MAX)));
            if !head_sent && head_frames > 0 && pcm.len() >= head * 2 {
                head_sent = true;
                on_head(Head { sample_rate: out_rate, pcm: pcm[..head * 2].to_vec(), frames: frames() });
            }
            Ok(())
        },
    )?;
    if let Some(c) = conv {
        c.finish(&mut pcm).map_err(resample_err)?;
        grew(&pcm);
        check_room(pcm.len(), 0, max_frames, path)?;
    }
    if pcm.is_empty() {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    Ok(Decoded { sample_rate: if out_rate == 0 { rate } else { out_rate }, pcm, source })
}

/// Decode `file` one packet at a time, handing `sink` each packet's frames
/// as interleaved stereo at the track's own rate (with the channel count the
/// file itself has, 0 for a corrupt packet's silence), and return that rate. Mono
/// is duplicated to both sides; files with more than two channels keep their
/// first two (front left and right). Nothing is held past one packet, so a
/// caller that only reduces the audio (the waveform peaks) never holds the
/// track whole. `decode_open_within` is this with a sink that keeps it all.
pub fn decode_stream(
    file: File,
    path: &Path,
    sink: impl FnMut(u32, usize, &[f32]) -> Result<(), ProtoError>,
) -> Result<(u32, Option<SourceId>), ProtoError> {
    decode_stream_with(file, path, |_| {}, sink)
}

/// `decode_stream`, telling `on_track` the track's length in frames at its
/// own rate as the container states it (see `Head::frames`) before the first
/// packet is decoded.
fn decode_stream_with(
    file: File,
    path: &Path,
    on_track: impl FnOnce(Option<u64>),
    mut sink: impl FnMut(u32, usize, &[f32]) -> Result<(), ProtoError>,
) -> Result<(u32, Option<SourceId>), ProtoError> {
    let Opened { mut format, mut decoder, track_id, time_base, codec_rate, frames, source, edit, .. } = open_decoder(file, path)?;
    let dec_err = |what: &str, e: &dyn std::fmt::Display| {
        ProtoError::new(ErrorCode::Decode, format!("{what} {}: {e}", path.display()))
    };
    // The stated length is the playable one: the edit's, when it states it.
    let stated = match edit.zip(codec_rate).and_then(|(e, r)| e.at(r)) {
        Some(e) => e.keep.filter(|&k| k > 0).or(frames.map(|n| n.saturating_sub(e.skip))),
        None => frames,
    };
    on_track(stated);
    let mut trim = StreamTrim::new(edit);

    let mut scratch: Vec<f32> = Vec::new();
    let mut block: Vec<f32> = Vec::new();
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
                if sample_rate == 0 {
                    sample_rate = rate.unwrap_or(0);
                }
                block.clear();
                block.resize(frames * 2, 0.0);
                trim.feed(sample_rate, 0, &block, &mut sink)?;
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
        block.clear();
        block.reserve(buf.frames() * 2);
        for frame in scratch.chunks_exact(ch) {
            let l = frame[0];
            let r = if ch == 1 { frame[0] } else { frame[1] };
            block.push(l);
            block.push(r);
        }
        trim.feed(sample_rate, ch, &block, &mut sink)?;
    }
    if !decoded_any {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no packet of {} decoded", path.display())));
    }
    if sample_rate == 0 {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    trim.finish(sample_rate, &mut sink)?;
    Ok((sample_rate, source))
}

/// An MP4 edit ([`crate::mp4edit`]) applied packet by packet at the track's
/// own rate, cut exactly where [`crate::mp4edit::apply_stereo`] cuts a whole
/// decode, so every streamed decode (deck load, head, waveform) keeps the
/// same frames ffmpeg starts the file on. The skipped priming frames are held
/// until the skip is passed: a file that ends inside them is handed over
/// untrimmed, as `apply_stereo` ignores an edit that would leave nothing.
struct StreamTrim {
    raw: Option<crate::mp4edit::RawEdit>,
    /// The edit in frames, resolved once the decoded rate is known.
    edit: Option<Option<crate::mp4edit::Edit>>,
    /// Frames decoded so far, before trimming.
    pos: u64,
    /// Frames handed over so far, after trimming.
    passed: u64,
    held: Vec<f32>,
    held_channels: usize,
}

impl StreamTrim {
    fn new(raw: Option<crate::mp4edit::RawEdit>) -> Self {
        StreamTrim { raw, edit: None, pos: 0, passed: 0, held: Vec::new(), held_channels: 0 }
    }

    fn feed(
        &mut self,
        rate: u32,
        channels: usize,
        block: &[f32],
        sink: &mut impl FnMut(u32, usize, &[f32]) -> Result<(), ProtoError>,
    ) -> Result<(), ProtoError> {
        if self.edit.is_none() && rate != 0 {
            let raw = self.raw;
            self.edit = Some(raw.and_then(|e| e.at(rate)));
        }
        let Some(Some(crate::mp4edit::Edit { skip, keep })) = self.edit else {
            return sink(rate, channels, block);
        };
        let n = (block.len() / 2) as u64;
        let at = self.pos;
        self.pos += n;
        let mut rest = block;
        if at < skip {
            let k = (skip - at).min(n) as usize;
            self.held.extend_from_slice(&block[..k * 2]);
            self.held_channels = self.held_channels.max(channels);
            rest = &block[k * 2..];
        }
        if self.pos > skip && !self.held.is_empty() {
            self.held = Vec::new();
        }
        if let Some(keep) = keep.filter(|&k| k > 0) {
            let room = keep.saturating_sub(self.passed) as usize;
            rest = &rest[..rest.len().min(room * 2)];
        }
        if rest.is_empty() {
            return Ok(());
        }
        self.passed += (rest.len() / 2) as u64;
        sink(rate, channels, rest)
    }

    /// Hand over the held priming frames when the file ended inside them.
    fn finish(
        self,
        rate: u32,
        sink: &mut impl FnMut(u32, usize, &[f32]) -> Result<(), ProtoError>,
    ) -> Result<(), ProtoError> {
        if self.held.is_empty() {
            return Ok(());
        }
        sink(rate, self.held_channels, &self.held)
    }
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
pub(crate) fn leading_tag_bytes(file: &mut File, path: &Path) -> Result<u64, ProtoError> {
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
pub(crate) enum ProbeFor {
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

pub(crate) fn probe_for(lead: u64) -> ProbeFor {
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
    /// The track's length in frames at its own rate as the container states
    /// it, when it does (see `Head::frames`).
    pub(crate) frames: Option<u64>,
    pub(crate) source: Option<SourceId>,
    /// The MP4 edit list as the file states it ([`crate::mp4edit`]), read
    /// before the probe because symphonia does not apply it.
    pub(crate) edit: Option<crate::mp4edit::RawEdit>,
}

/// Probe `file`, opened from `path` (which names it in errors and gives the
/// format hint), and make the decoder for its default audio track. Shared by
/// the whole-file decode a deck loads with and the streaming
/// [`decode_to_wav`], so both read a file the same way.
pub(crate) fn open_decoder(mut file: File, path: &Path) -> Result<Opened, ProtoError> {
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
    let format = probe_for(lead)
        .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
        .map_err(|e| dec_err("unrecognized format in", &e))?;
    let track = format
        .default_track(TrackType::Audio)
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio track in {}", path.display())))?;
    let track_id = track.id;
    let time_base = track.time_base;
    let frames = track.num_frames.filter(|&n| n > 0);
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
    Ok(Opened { format, decoder, track_id, time_base, codec_rate, codec_channels, frames, source, edit })
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
    /// `none` or `applied`.
    pub edit: String,
}

/// Which decoded frames `decode_to_wav` keeps, per the file's edit list.
struct Trim {
    /// The edit as the file states it, converted to frames once the rate is
    /// known (`resolved`).
    raw: Option<crate::mp4edit::RawEdit>,
    resolved: Option<Option<crate::mp4edit::Edit>>,
    /// Frames decoded so far, before trimming (the decoder's timeline).
    pos: u64,
    trimmed_start: u64,
    dropped_end: u64,
}

impl Trim {
    fn new(raw: Option<crate::mp4edit::RawEdit>) -> Self {
        Trim { raw, resolved: None, pos: 0, trimmed_start: 0, dropped_end: 0 }
    }

    /// Of the next `n` decoded frames, (first kept, how many kept). The edit
    /// is converted to frames once, when the track's rate is first known.
    fn window(&mut self, n: u64, rate: u32) -> (u64, u64) {
        let raw = self.raw;
        let edit = *self.resolved.get_or_insert_with(|| raw.and_then(|e| e.at(rate)));
        let at = self.pos;
        self.pos += n;
        let Some(crate::mp4edit::Edit { skip, keep }) = edit else { return (0, n) };
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
        match self.resolved {
            Some(Some(_)) => "applied".into(),
            _ => "none".into(),
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
/// error. An MP4's edit list is applied ([`crate::mp4edit`]), as the deck
/// applies it, so an M4A starts where ffmpeg starts it rather than 1024
/// priming frames early; unlike the deck's exact cut at the edit's end, a
/// packet that straddles the end is kept whole, as ffmpeg keeps it. `out` must be seekable: the header is
/// written last, once the length is known. On an error `out` holds a partial
/// file the caller discards.
pub fn decode_to_wav<W: Write + Seek>(path: &Path, out: &mut W) -> Result<WavWritten, ProtoError> {
    let Opened { mut format, mut decoder, track_id, time_base, codec_rate, codec_channels, edit, .. } =
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
    let mut trim = Trim::new(edit);
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
                let (_, take) = trim.window(n as u64, sample_rate);
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
        let (from, take) = trim.window(buf.frames() as u64, sample_rate);
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

    /// `StreamTrim` over `pcm` in blocks of `sizes` (cycled), collected.
    fn streamed(pcm: &[f32], edit: crate::mp4edit::Edit, sizes: &[usize]) -> Vec<f32> {
        let mut t = StreamTrim::new(None);
        t.edit = Some(Some(edit));
        let mut out = Vec::new();
        let mut sink = |_: u32, _: usize, b: &[f32]| -> Result<(), ProtoError> {
            out.extend_from_slice(b);
            Ok(())
        };
        let mut at = 0;
        for &n in sizes.iter().cycle() {
            if at >= pcm.len() {
                break;
            }
            let end = (at + n * 2).min(pcm.len());
            t.feed(44100, 2, &pcm[at..end], &mut sink).unwrap();
            at = end;
        }
        t.finish(44100, &mut sink).unwrap();
        out
    }

    #[test]
    fn a_streamed_edit_keeps_exactly_what_a_whole_decode_keeps() {
        use crate::mp4edit::{apply_stereo, Edit};
        let pcm: Vec<f32> = (0..2 * 5000).map(|i| i as f32).collect();
        let edits = [
            Edit { skip: 1024, keep: Some(3000) },
            Edit { skip: 1024, keep: None },
            Edit { skip: 0, keep: Some(10) },
            Edit { skip: 4999, keep: Some(1) },
            // Nothing left after the skip: the edit is ignored.
            Edit { skip: 5000, keep: None },
            Edit { skip: 9000, keep: Some(5) },
            // keep 0 or past the end trims nothing at the end.
            Edit { skip: 100, keep: Some(0) },
            Edit { skip: 100, keep: Some(99_999) },
        ];
        for e in edits {
            let mut whole = pcm.clone();
            apply_stereo(&mut whole, e);
            for sizes in [&[1024usize][..], &[1][..], &[7, 1500, 3][..], &[5000][..], &[6000][..]] {
                assert_eq!(streamed(&pcm, e, sizes), whole, "edit {e:?}, blocks {sizes:?}");
            }
        }
    }

    #[test]
    fn a_progressive_load_of_an_m4a_is_trimmed_like_the_whole_decode() {
        let path = Path::new(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/audio/click-250ms-aac.m4a"));
        let whole = decode_at(path, 48000).unwrap();
        let mut head = None;
        let d = decode_progressive(open(path).unwrap(), path, Some(48000), u64::MAX, 4800, |h| head = Some(h)).unwrap();
        assert_eq!(d.pcm.len(), 48000 * 2, "the edit's one playable second at 48 kHz");
        assert_eq!(d.pcm, whole.pcm);
        let head = head.expect("a head");
        assert_eq!(head.frames, Some(48000), "the stated length is the edit's");
        assert_eq!(head.pcm[..], d.pcm[..head.pcm.len()]);
    }

    /// The windows `decode_to_wav` keeps for 1024-frame packets under `edit`.
    fn windows(edit: Option<crate::mp4edit::Edit>, packets: usize) -> (Vec<(u64, u64)>, Trim) {
        let mut t = Trim::new(None);
        t.resolved = Some(edit);
        let w = (0..packets).map(|_| t.window(1024, 44100)).collect();
        (w, t)
    }

    #[test]
    fn the_edit_list_trims_the_front_and_drops_whole_packets_past_its_end() {
        use crate::mp4edit::Edit;
        // ffmpeg: skip 1024 (one packet), then 1500 frames; the packet that
        // straddles the end (starting at 2048 < 2524) is kept whole, the one
        // starting at 3072 is dropped.
        let (w, t) = windows(Some(Edit { skip: 1024, keep: Some(1500) }), 4);
        assert_eq!(w, vec![(1024, 0), (0, 1024), (0, 1024), (0, 0)]);
        assert_eq!((t.trimmed_start, t.dropped_end, t.describe().as_str()), (1024, 1024, "applied"));
        // A skip inside a packet keeps its tail.
        let (w, _) = windows(Some(Edit { skip: 2112, keep: None }), 3);
        assert_eq!(w, vec![(1024, 0), (1024, 0), (64, 960)]);
        // Control: no edit list keeps every frame.
        let (w, t) = windows(None, 3);
        assert_eq!(w, vec![(0, 1024); 3]);
        assert_eq!((t.trimmed_start, t.dropped_end, t.describe().as_str()), (0, 0, "none"));
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

    /// `resample` as it was before it streamed (before
    /// `StreamResampler`), kept verbatim as the reference the streaming form
    /// must equal sample for sample.
    fn resample_whole_reference(d: &Decoded, to: u32) -> Vec<f32> {
        let from = d.sample_rate;
        let n = d.pcm.len() / 2;
        let want = ((n as u128 * to as u128 + from as u128 / 2) / from as u128) as usize;
        let mut r = FftFixedIn::<f32>::new(from as usize, to as usize, RESAMPLE_CHUNK, 2, 2).unwrap();
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
            let (_, got) = r.process_into_buffer(&inp, &mut out, None).unwrap();
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
        pcm
    }

    fn noise(frames: usize) -> Vec<f32> {
        let mut x: u64 = 0x2545F4914F6CDD1D;
        (0..frames * 2)
            .map(|_| {
                x ^= x << 13;
                x ^= x >> 7;
                x ^= x << 17;
                (x >> 40) as f32 / (1u64 << 24) as f32 - 0.5
            })
            .collect()
    }

    #[test]
    fn streamed_resampling_equals_the_whole_file_form_whatever_the_blocks() {
        // Lengths around chunk edges, including shorter than one chunk.
        for frames in [100, RESAMPLE_CHUNK, RESAMPLE_CHUNK + 1, 3 * RESAMPLE_CHUNK - 7, 44100] {
            let d = Decoded { sample_rate: 44100, pcm: noise(frames), source: None };
            let want = resample_whole_reference(&d, 48000);
            for block in [1usize, 1152, 4096, 10000] {
                let mut r = StreamResampler::new(44100, 48000).unwrap();
                let mut got = Vec::new();
                for b in d.pcm.chunks(block * 2) {
                    r.push(b, &mut got).unwrap();
                }
                r.finish(&mut got).unwrap();
                assert!(got == want, "{frames} frames in blocks of {block}: the streamed output differs");
            }
            assert!(resample(d, 48000).unwrap().pcm == want, "{frames} frames: resample changed");
        }
    }

    fn wav(dir: &Path, name: &str, sr: u32, pcm: &[f32]) -> std::path::PathBuf {
        let p = dir.join(name);
        crate::wav::write_f32(&mut std::io::BufWriter::new(File::create(&p).unwrap()), sr, pcm).unwrap();
        p
    }

    #[test]
    fn a_progressive_decode_hands_over_the_exact_head_then_the_whole_track() {
        let tmp = tempfile::Builder::new().prefix("odj-audio-test-progressive-").tempdir().unwrap();
        let src = noise(3 * 44100);
        let p = wav(tmp.path(), "a.wav", 44100, &src);
        let whole = resample_whole_reference(&Decoded { sample_rate: 44100, pcm: src.clone(), source: None }, 48000);
        let mut heads = Vec::new();
        let d = decode_progressive(open(&p).unwrap(), &p, Some(48000), u64::MAX, 48000, |h| heads.push(h)).unwrap();
        assert_eq!(d.sample_rate, 48000);
        assert!(d.pcm == whole, "the whole track is not the whole-file decode and resample");
        assert_eq!(heads.len(), 1, "the head was not handed over exactly once");
        let h = &heads[0];
        assert_eq!(h.sample_rate, 48000);
        assert!(h.pcm == whole[..48000 * 2], "the head is not the first second of the whole track");
        // A WAV states its length; scaled to the output rate it is the
        // whole track's.
        assert_eq!(h.frames, Some((whole.len() / 2) as u64));
        // At the file's own rate there is nothing to convert. A head asked
        // shorter than 1/HEAD_SHARE of the stated length is that share.
        let mut heads = Vec::new();
        let d = decode_progressive(open(&p).unwrap(), &p, None, u64::MAX, 1000, |h| heads.push(h)).unwrap();
        let share = (3 * 44100 / HEAD_SHARE) as usize;
        assert!(share > 1000);
        assert!(d.pcm == src && heads[0].frames == Some(3 * 44100));
        assert!(heads[0].pcm == src[..share * 2], "the head is {} frames, not {share}", heads[0].pcm.len() / 2);
        // A track shorter than the head is handed over only whole.
        let mut called = false;
        let d = decode_progressive(open(&p).unwrap(), &p, Some(48000), u64::MAX, 4 * 48000, |_| called = true).unwrap();
        assert!(!called, "a head longer than the track was handed over");
        assert!(d.pcm == whole);
        // The budget still holds the converted track to `max_frames`.
        let e = decode_progressive(open(&p).unwrap(), &p, Some(48000), 2 * 48000, 48000, |_| {}).err().expect("over budget");
        assert!(e.message.contains("memory budget"), "{}", e.message);
    }

    #[test]
    fn a_metered_decode_reports_its_buffer_as_it_grows_up_to_what_it_returns() {
        // The engine charges a rest whose length the file does not state by
        // these reports (serve.rs `Meter`), so they must rise while the
        // buffer fills and end at exactly what the decode hands back.
        let tmp = tempfile::Builder::new().prefix("odj-audio-test-metered-").tempdir().unwrap();
        let p = wav(tmp.path(), "a.wav", 44100, &noise(3 * 44100));
        for rate in [Some(48000), None] {
            let mut grown = Vec::new();
            let d = decode_progressive_metered(open(&p).unwrap(), &p, rate, u64::MAX, 4800, |_| {}, |b| grown.push(b)).unwrap();
            assert!(grown.len() > 1, "{rate:?}: the growth was reported once, not as it filled: {grown:?}");
            assert!(grown.windows(2).all(|w| w[0] < w[1]), "{rate:?}: reports did not rise: {grown:?}");
            assert_eq!(grown.last().copied(), Some(d.pcm.capacity() * std::mem::size_of::<f32>()), "{rate:?}");
        }
    }
}
