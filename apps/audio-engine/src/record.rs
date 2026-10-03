//! Set recording to disk: rolling 16-bit PCM WAV segments named by their UTC
//! start, `audio_YYYY-MM-DDTHH-MM-SS.wav`, the names the sets recorder
//! (`apps/sets`) reads a segment's start time from.
//!
//! This half needs no device, so Linux CI tests it: the device half
//! (`capture`, feature `device`) only feeds it interleaved f32 frames. It is
//! what lets REC record in the installed app, which bundles no ffmpeg
//! (`docs/decisions/*-set-recording-without-ffmpeg.md`).
//!
//! Every file is created new (`create_new`), never replaced, so a recording
//! can never overwrite anything, a library file included. The header's sizes
//! are rewritten about once a second, so a process killed mid-segment still
//! leaves a WAV that plays up to its last second.

use std::fs::{File, OpenOptions};
use std::io::{self, BufWriter, Seek, SeekFrom, Write};
use std::path::{Path, PathBuf};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

/// The longest segment `record` accepts: an hour of 16-bit stereo at 192 kHz
/// is about 2.6 GiB, inside a WAV file's 4 GiB.
pub const MAX_SEGMENT_SECONDS: u32 = 3600;

/// How many later seconds a segment name may move to when its own is taken
/// (REC restarted on a session within the same second, or a writer catching
/// up through two short segments in one second). The file there is never
/// touched; past this the recording fails rather than guess.
pub const NAME_TRIES: u64 = 5;

/// How long the input may deliver nothing before the recording counts it as
/// gone: a device that vanishes without an error otherwise leaves a live
/// process writing nothing.
pub const STALL_LIMIT: Duration = Duration::from_secs(5);

/// `audio_YYYY-MM-DDTHH-MM-SS.wav` for a time `secs` after the Unix epoch, in
/// UTC (proleptic Gregorian, days from Howard Hinnant's civil_from_days).
pub fn segment_name(secs: u64) -> String {
    let days = (secs / 86_400) as i64;
    let rem = secs % 86_400;
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z.rem_euclid(146_097);
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = yoe + era * 400 + i64::from(month <= 2);
    format!(
        "audio_{year:04}-{month:02}-{day:02}T{:02}-{:02}-{:02}.wav",
        rem / 3600,
        rem % 3600 / 60,
        rem % 60
    )
}

/// The 44-byte header of a 16-bit PCM WAV (format 1).
pub fn write_pcm16_header(w: &mut impl Write, channels: u16, sr: u32, data_bytes: u32) -> io::Result<()> {
    if channels == 0 {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "a WAV file needs at least one channel"));
    }
    if data_bytes > u32::MAX - 36 {
        return Err(io::Error::new(io::ErrorKind::InvalidInput, "audio too long for a WAV file (4 GiB)"));
    }
    let block_align = channels as u32 * 2;
    w.write_all(b"RIFF")?;
    w.write_all(&(36 + data_bytes).to_le_bytes())?;
    w.write_all(b"WAVEfmt ")?;
    w.write_all(&16u32.to_le_bytes())?;
    w.write_all(&1u16.to_le_bytes())?;
    w.write_all(&channels.to_le_bytes())?;
    w.write_all(&sr.to_le_bytes())?;
    w.write_all(&(sr * block_align).to_le_bytes())?;
    w.write_all(&(block_align as u16).to_le_bytes())?;
    w.write_all(&16u16.to_le_bytes())?;
    w.write_all(b"data")?;
    w.write_all(&data_bytes.to_le_bytes())
}

fn to_i16(s: f32) -> i16 {
    (s.clamp(-1.0, 1.0) * 32767.0).round() as i16
}

struct Open {
    w: BufWriter<File>,
    frames: u64,
    since_patch: u64,
}

/// Writes interleaved f32 frames of `source_channels` channels as WAV
/// segments of `segment_frames` frames each in `dir`. Mono stays mono; a
/// source with more than two channels keeps its first two (a loopback such
/// as BlackHole 16ch carries the master on 1 and 2).
pub struct SegmentWriter {
    dir: PathBuf,
    source_channels: usize,
    channels: u16,
    rate: u32,
    segment_frames: u64,
    clock: Box<dyn FnMut() -> SystemTime + Send>,
    frames_total: u64,
    open: Option<Open>,
    segments: Vec<String>,
}

impl SegmentWriter {
    /// Each segment is named for `clock()` when its first frame is written
    /// (`SystemTime::now` in `record`), as ffmpeg's segmenter names them: a
    /// dropped buffer then shifts audio inside one segment, never the start
    /// of every segment after it.
    pub fn new(
        dir: &Path,
        source_channels: u16,
        rate: u32,
        segment_seconds: u32,
        clock: impl FnMut() -> SystemTime + Send + 'static,
    ) -> Result<Self, String> {
        if source_channels == 0 || rate == 0 {
            return Err(format!("cannot record {source_channels} channels at {rate} Hz"));
        }
        if !(1..=MAX_SEGMENT_SECONDS).contains(&segment_seconds) {
            return Err(format!("segment length {segment_seconds}s is outside 1..={MAX_SEGMENT_SECONDS}"));
        }
        if !dir.is_dir() {
            return Err(format!("{} is not a directory", dir.display()));
        }
        Ok(Self {
            dir: dir.to_path_buf(),
            source_channels: source_channels as usize,
            channels: source_channels.min(2),
            rate,
            segment_frames: u64::from(segment_seconds) * u64::from(rate),
            clock: Box::new(clock),
            frames_total: 0,
            open: None,
            segments: Vec::new(),
        })
    }

    /// Channels and rate of the files written.
    pub fn format(&self) -> (u16, u32) {
        (self.channels, self.rate)
    }

    pub fn frames(&self) -> u64 {
        self.frames_total
    }

    /// Names of the segments created so far, oldest first.
    pub fn segments(&self) -> &[String] {
        &self.segments
    }

    fn open_next(&mut self) -> io::Result<Open> {
        let secs = (self.clock)().duration_since(UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0);
        let mut tried = 0;
        let (name, file) = loop {
            let name = segment_name(secs + tried);
            let path = self.dir.join(&name);
            match OpenOptions::new().write(true).create_new(true).open(&path) {
                Ok(f) => break (name, f),
                Err(e) if e.kind() == io::ErrorKind::AlreadyExists && tried + 1 < NAME_TRIES => tried += 1,
                Err(e) => {
                    return Err(io::Error::new(
                        e.kind(),
                        format!("cannot create {}: {e} (recording never replaces a file)", path.display()),
                    ))
                }
            }
        };
        let mut w = BufWriter::new(file);
        write_pcm16_header(&mut w, self.channels, self.rate, 0)?;
        self.segments.push(name);
        Ok(Open { w, frames: 0, since_patch: 0 })
    }

    /// Append whole frames. A trailing partial frame is an error: the
    /// capture side only ever hands over whole frames.
    pub fn push(&mut self, samples: &[f32]) -> io::Result<()> {
        if samples.len() % self.source_channels != 0 {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("{} samples is not a whole number of {}-channel frames", samples.len(), self.source_channels),
            ));
        }
        for frame in samples.chunks_exact(self.source_channels) {
            if self.open.as_ref().is_some_and(|o| o.frames >= self.segment_frames) {
                self.close_current()?;
            }
            if self.open.is_none() {
                self.open = Some(self.open_next()?);
            }
            let rate = u64::from(self.rate);
            let channels = self.channels;
            let o = self.open.as_mut().expect("opened above");
            for s in &frame[..channels as usize] {
                o.w.write_all(&to_i16(*s).to_le_bytes())?;
            }
            o.frames += 1;
            o.since_patch += 1;
            self.frames_total += 1;
            if o.since_patch >= rate {
                o.since_patch = 0;
                patch(o, channels)?;
            }
        }
        Ok(())
    }

    fn close_current(&mut self) -> io::Result<()> {
        if let Some(mut o) = self.open.take() {
            patch(&mut o, self.channels)?;
            o.w.get_ref().sync_all()?;
        }
        Ok(())
    }

    /// Close the open segment with its final sizes; the names written.
    pub fn finish(mut self) -> io::Result<Vec<String>> {
        self.close_current()?;
        Ok(std::mem::take(&mut self.segments))
    }
}

/// Move whole frames from the capture ring into `writer`: every sample in
/// it, less a trailing partial frame still being written. The ring wraps on
/// a sample boundary, which need not be a frame boundary. Returns the samples
/// moved.
pub fn drain_ring(cons: &mut rtrb::Consumer<f32>, writer: &mut SegmentWriter, channels: usize) -> io::Result<usize> {
    let n = cons.slots() - cons.slots() % channels;
    if n == 0 {
        return Ok(0);
    }
    let chunk = cons.read_chunk(n).map_err(|e| io::Error::other(e.to_string()))?;
    let (a, b) = chunk.as_slices();
    if a.len() % channels == 0 {
        writer.push(a)?;
        writer.push(b)?;
    } else {
        let mut joined = Vec::with_capacity(n);
        joined.extend_from_slice(a);
        joined.extend_from_slice(b);
        writer.push(&joined)?;
    }
    chunk.commit_all();
    Ok(n)
}

/// Throw away what the ring holds: the silence an input delivers while the
/// microphone permission is still being asked for.
pub fn discard_ring(cons: &mut rtrb::Consumer<f32>) {
    let n = cons.slots();
    if let Ok(chunk) = cons.read_chunk(n) {
        chunk.commit_all();
    }
}

/// Notices an input that stops delivering without reporting an error.
pub struct StallWatch {
    last: Instant,
    limit: Duration,
}

impl StallWatch {
    pub fn new(now: Instant, limit: Duration) -> Self {
        Self { last: now, limit }
    }

    /// Record a drain of `samples` at `now`; true when the input has
    /// delivered nothing for longer than the limit.
    pub fn stalled(&mut self, samples: usize, now: Instant) -> bool {
        if samples > 0 {
            self.last = now;
            return false;
        }
        now.duration_since(self.last) > self.limit
    }
}

/// Rewrite the RIFF and data sizes for what has been written so far.
fn patch(o: &mut Open, channels: u16) -> io::Result<()> {
    let data = o.frames * u64::from(channels) * 2;
    let data = u32::try_from(data)
        .ok()
        .filter(|&n| n <= u32::MAX - 36)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "segment too long for a WAV file (4 GiB)"))?;
    o.w.flush()?;
    let f = o.w.get_mut();
    f.seek(SeekFrom::Start(4))?;
    f.write_all(&(36 + data).to_le_bytes())?;
    f.seek(SeekFrom::Start(40))?;
    f.write_all(&data.to_le_bytes())?;
    f.seek(SeekFrom::End(0))?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A clock that starts at `secs` and moves `step` seconds per reading.
    fn at_step(secs: u64, step: u64) -> impl FnMut() -> SystemTime + Send + 'static {
        let mut next = secs;
        move || {
            let t = UNIX_EPOCH + Duration::from_secs(next);
            next += step;
            t
        }
    }

    fn at(secs: u64) -> impl FnMut() -> SystemTime + Send + 'static {
        at_step(secs, 1)
    }

    fn le16(b: &[u8], i: usize) -> u16 {
        u16::from_le_bytes([b[i], b[i + 1]])
    }

    fn le32(b: &[u8], i: usize) -> u32 {
        u32::from_le_bytes([b[i], b[i + 1], b[i + 2], b[i + 3]])
    }

    #[test]
    fn segment_names_are_utc_civil_times() {
        assert_eq!(segment_name(0), "audio_1970-01-01T00-00-00.wav");
        // 2026-10-03T02:55:07Z, checked against `date -u -d @1790996107`.
        assert_eq!(segment_name(1_790_996_107), "audio_2026-10-03T02-55-07.wav");
        // A leap day and the last second of a year.
        assert_eq!(segment_name(951_782_400), "audio_2000-02-29T00-00-00.wav");
        assert_eq!(segment_name(1_767_225_599), "audio_2025-12-31T23-59-59.wav");
    }

    #[test]
    fn frames_roll_into_named_segments_with_final_sizes() {
        let tmp = tempfile::tempdir().unwrap();
        // 4 Hz, 2-second segments: 8 frames each. 20 stereo frames make
        // segments of 8, 8 and 4 frames, each named for the clock as it opens.
        let mut w = SegmentWriter::new(tmp.path(), 2, 4, 2, at_step(1_790_996_107, 2)).unwrap();
        let pcm: Vec<f32> = (0..40).map(|i| if i % 2 == 0 { 0.5 } else { -1.5 }).collect();
        w.push(&pcm[..14]).unwrap();
        w.push(&pcm[14..]).unwrap();
        assert_eq!(w.frames(), 20);
        let names = w.finish().unwrap();
        assert_eq!(
            names,
            ["audio_2026-10-03T02-55-07.wav", "audio_2026-10-03T02-55-09.wav", "audio_2026-10-03T02-55-11.wav"]
        );
        for (name, frames) in names.iter().zip([8u32, 8, 4]) {
            let b = std::fs::read(tmp.path().join(name)).unwrap();
            assert_eq!(b.len() as u32, 44 + frames * 4, "{name}");
            assert_eq!(le32(&b, 4), 36 + frames * 4);
            assert_eq!(le32(&b, 40), frames * 4);
            assert_eq!((le16(&b, 20), le16(&b, 22), le32(&b, 24), le16(&b, 34)), (1, 2, 4, 16));
            // Left 0.5, right clamped from -1.5 to full scale.
            assert_eq!(i16::from_le_bytes([b[44], b[45]]), 16384);
            assert_eq!(i16::from_le_bytes([b[46], b[47]]), -32767);
        }
    }

    #[test]
    fn mono_stays_mono_and_wide_sources_keep_their_first_two_channels() {
        let tmp = tempfile::tempdir().unwrap();
        let mut mono = SegmentWriter::new(tmp.path(), 1, 8, 1, at(10)).unwrap();
        mono.push(&[0.25; 3]).unwrap();
        let name = &mono.finish().unwrap()[0];
        let b = std::fs::read(tmp.path().join(name)).unwrap();
        assert_eq!((le16(&b, 22), le32(&b, 40)), (1, 6));

        let mut wide = SegmentWriter::new(tmp.path(), 4, 8, 1, at(20)).unwrap();
        assert_eq!(wide.format(), (2, 8));
        wide.push(&[0.1, 0.2, 0.9, 0.9, 0.3, 0.4, 0.9, 0.9]).unwrap();
        let name = &wide.finish().unwrap()[0];
        let b = std::fs::read(tmp.path().join(name)).unwrap();
        let got: Vec<i16> = b[44..].chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
        assert_eq!(got, [to_i16(0.1), to_i16(0.2), to_i16(0.3), to_i16(0.4)]);
        // A partial frame is refused rather than shifting every channel after it.
        let mut w = SegmentWriter::new(tmp.path(), 2, 8, 1, at(30)).unwrap();
        assert!(w.push(&[0.0; 3]).is_err());
    }

    #[test]
    fn a_killed_recording_leaves_a_playable_file_up_to_its_last_patch() {
        let tmp = tempfile::tempdir().unwrap();
        let mut w = SegmentWriter::new(tmp.path(), 2, 4, 10, at(40)).unwrap();
        // Two seconds and one frame: patched at 1 s and 2 s, then dropped
        // without finish, as a SIGKILL would leave it.
        w.push(&[0.0; 18]).unwrap();
        let name = w.segments()[0].clone();
        drop(w);
        let b = std::fs::read(tmp.path().join(&name)).unwrap();
        assert_eq!(le32(&b, 40), 8 * 4, "the header covers the patched frames");
        assert!(b.len() >= 44 + 8 * 4);
    }

    #[test]
    fn nothing_is_ever_replaced_and_no_frames_means_no_file() {
        let tmp = tempfile::tempdir().unwrap();
        let existing = tmp.path().join(segment_name(50));
        std::fs::write(&existing, b"keep me").unwrap();
        // A taken name moves to the next free second; the file is untouched.
        let mut w = SegmentWriter::new(tmp.path(), 2, 4, 1, at_step(50, 0)).unwrap();
        w.push(&[0.0; 2]).unwrap();
        assert_eq!(w.finish().unwrap(), [segment_name(51)]);
        assert_eq!(std::fs::read(&existing).unwrap(), b"keep me");
        // With every nearby second taken, it fails rather than overwrite.
        for s in 52..50 + NAME_TRIES {
            std::fs::write(tmp.path().join(segment_name(s)), b"keep me").unwrap();
        }
        let mut w = SegmentWriter::new(tmp.path(), 2, 4, 1, at_step(50, 0)).unwrap();
        let err = w.push(&[0.0; 2]).unwrap_err();
        assert_eq!(err.kind(), io::ErrorKind::AlreadyExists);
        assert_eq!(std::fs::read(&existing).unwrap(), b"keep me");
        // Control: a writer that got no frames creates nothing.
        let empty = tempfile::tempdir().unwrap();
        let w = SegmentWriter::new(empty.path(), 2, 4, 1, at(60)).unwrap();
        assert!(w.finish().unwrap().is_empty());
        assert_eq!(std::fs::read_dir(empty.path()).unwrap().count(), 0);
    }

    #[test]
    fn bad_formats_and_segment_lengths_are_refused() {
        let tmp = tempfile::tempdir().unwrap();
        assert!(SegmentWriter::new(tmp.path(), 0, 48000, 300, at(0)).is_err());
        assert!(SegmentWriter::new(tmp.path(), 2, 0, 300, at(0)).is_err());
        assert!(SegmentWriter::new(tmp.path(), 2, 48000, 0, at(0)).is_err());
        assert!(SegmentWriter::new(tmp.path(), 2, 48000, MAX_SEGMENT_SECONDS + 1, at(0)).is_err());
        assert!(SegmentWriter::new(&tmp.path().join("missing"), 2, 48000, 300, at(0)).is_err());
        assert!(SegmentWriter::new(tmp.path(), 2, 48000, MAX_SEGMENT_SECONDS, at(0)).is_ok());
    }

    #[test]
    fn the_ring_drains_whole_frames_even_when_it_wraps_mid_frame() {
        let tmp = tempfile::tempdir().unwrap();
        let mut w = SegmentWriter::new(tmp.path(), 2, 8, 10, at(70)).unwrap();
        // Capacity 5 samples: after 4 written and drained, the next 4 wrap
        // with one sample before the end and three after it.
        let (mut prod, mut cons) = rtrb::RingBuffer::<f32>::new(5);
        for v in [0.1, 0.2, 0.3, 0.4] {
            prod.push(v).unwrap();
        }
        assert_eq!(drain_ring(&mut cons, &mut w, 2).unwrap(), 4);
        for v in [0.5, 0.6, 0.7] {
            prod.push(v).unwrap();
        }
        // A partial frame waits for its other half.
        assert_eq!(drain_ring(&mut cons, &mut w, 2).unwrap(), 2);
        prod.push(0.8).unwrap();
        assert_eq!(drain_ring(&mut cons, &mut w, 2).unwrap(), 2);
        assert_eq!(drain_ring(&mut cons, &mut w, 2).unwrap(), 0);
        let name = w.finish().unwrap().remove(0);
        let b = std::fs::read(tmp.path().join(name)).unwrap();
        let got: Vec<i16> = b[44..].chunks_exact(2).map(|c| i16::from_le_bytes([c[0], c[1]])).collect();
        let want: Vec<i16> = [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8].iter().map(|s| to_i16(*s)).collect();
        assert_eq!(got, want, "every sample in order, channels never shifted");
    }

    #[test]
    fn an_input_that_goes_quiet_past_the_limit_is_stalled() {
        let t0 = Instant::now();
        let mut watch = StallWatch::new(t0, Duration::from_secs(5));
        assert!(!watch.stalled(0, t0 + Duration::from_secs(4)));
        // Audio arriving resets the clock.
        assert!(!watch.stalled(512, t0 + Duration::from_secs(4)));
        assert!(!watch.stalled(0, t0 + Duration::from_secs(9)));
        assert!(watch.stalled(0, t0 + Duration::from_millis(9_001)));
    }
}
