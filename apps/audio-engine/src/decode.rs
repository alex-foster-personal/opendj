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
    decode_progressive(file, path, sample_rate, max_frames, 0, |_| {})
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
    mut on_head: impl FnMut(Head),
) -> Result<Decoded, ProtoError> {
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
    let mut format = symphonia::default::get_probe()
        .probe(&hint, mss, FormatOptions::default(), MetadataOptions::default())
        .map_err(|e| dec_err("unrecognized format in", &e))?;
    let track = format
        .default_track(TrackType::Audio)
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio track in {}", path.display())))?;
    let track_id = track.id;
    let time_base = track.time_base;
    on_track(track.num_frames.filter(|&n| n > 0));
    let params = track
        .codec_params
        .as_ref()
        .and_then(|p| p.audio())
        .ok_or_else(|| ProtoError::new(ErrorCode::Decode, format!("no audio codec parameters in {}", path.display())))?;
    let codec_rate = params.sample_rate;
    let mut decoder = symphonia::default::get_codecs()
        .make_audio_decoder(params, &AudioDecoderOptions::default())
        .map_err(|e| dec_err("unsupported codec in", &e))?;

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
                sink(sample_rate, 0, &block)?;
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
        sink(sample_rate, ch, &block)?;
    }
    if !decoded_any {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no packet of {} decoded", path.display())));
    }
    if sample_rate == 0 {
        return Err(ProtoError::new(ErrorCode::Decode, format!("no audio decoded from {}", path.display())));
    }
    Ok((sample_rate, source))
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
}
