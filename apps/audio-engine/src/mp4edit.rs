//! The edit list of an MP4/M4A audio track: how many encoder priming frames
//! to drop from the front and how long the playable audio is.
//!
//! symphonia 0.6's isomp4 demuxer parses `elst` but does not apply it ("edits
//! are not currently supported", `symphonia-format-isomp4` demuxer.rs), so an
//! AAC track decodes with its priming frames in front: 1024 from ffmpeg's
//! encoder, 2112 from Apple's. Everything in that file then plays and analyzes
//! 23 to 48 ms late against the same file decoded by ffmpeg, rekordbox or a
//! browser. Measured Thu 1 Oct 2026 on an ffmpeg-encoded AAC m4a: symphonia
//! output lagged ffmpeg's by exactly 1024 frames, while WAV, AIFF, FLAC, ALAC,
//! MP3 and Vorbis matched it sample for sample in time.
//!
//! Only the first non-empty edit of the first sound track is read. That is the
//! one every AAC encoder writes; a multi-edit audio file is not something a
//! music library contains, and reading one edit is never worse than none.
//!
//! A sound track with no edit list falls back to iTunes gapless metadata: the
//! `iTunSMPB` freeform tag under `moov/udta/meta/ilst`, which states the same
//! priming delay, the end padding and the original sample count as hex. Files
//! from Apple's encoders (iTunes, Music, afconvert) commonly carry only that,
//! so without the fallback they decode 2112 frames (47.9 ms at 44.1 kHz) late.
//! An edit list, when present, wins: it is the container's own statement.

use std::io::{Read, Seek, SeekFrom};

/// The first non-empty edit of the file's first sound track, in that track's
/// sample frames.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Edit {
    /// Frames to drop from the front (`elst` media_time).
    pub skip: u64,
    /// Playable frames after `skip`, when the edit states a length.
    pub keep: Option<u64>,
}

/// Largest box body read into memory. `mvhd`, `mdhd`, `hdlr` and `elst` are
/// tens of bytes; anything bigger is not one of them.
const MAX_SMALL_BOX: u64 = 1 << 16;

/// Largest skip or length an edit may state, in decoded frames: 2^40 is over
/// 250 days at 48 kHz, so anything past it is a corrupt edit or tag.
pub const MAX_EDIT_FRAMES: u64 = 1 << 40;

struct BoxHead {
    kind: [u8; 4],
    /// Offset of the body.
    body: u64,
    /// Offset just past the box.
    end: u64,
}

fn read_head<R: Read + Seek>(r: &mut R, at: u64, limit: u64) -> std::io::Result<Option<BoxHead>> {
    if at + 8 > limit {
        return Ok(None);
    }
    r.seek(SeekFrom::Start(at))?;
    let mut h = [0u8; 8];
    if r.read_exact(&mut h).is_err() {
        return Ok(None);
    }
    let size32 = u32::from_be_bytes([h[0], h[1], h[2], h[3]]) as u64;
    let kind = [h[4], h[5], h[6], h[7]];
    let (body, end) = match size32 {
        0 => (at + 8, limit),
        1 => {
            let mut l = [0u8; 8];
            if r.read_exact(&mut l).is_err() {
                return Ok(None);
            }
            (at + 16, at.saturating_add(u64::from_be_bytes(l)))
        }
        n => (at + 8, at.saturating_add(n)),
    };
    if end < body || end > limit {
        return Ok(None);
    }
    Ok(Some(BoxHead { kind, body, end }))
}

/// The child boxes of the span `[from, to)`.
fn children<R: Read + Seek>(r: &mut R, from: u64, to: u64) -> std::io::Result<Vec<BoxHead>> {
    let mut out = Vec::new();
    let mut at = from;
    while let Some(b) = read_head(r, at, to)? {
        at = b.end;
        out.push(b);
        if out.len() > 4096 {
            break;
        }
    }
    Ok(out)
}

fn body<R: Read + Seek>(r: &mut R, b: &BoxHead) -> std::io::Result<Option<Vec<u8>>> {
    let n = b.end - b.body;
    if n > MAX_SMALL_BOX {
        return Ok(None);
    }
    r.seek(SeekFrom::Start(b.body))?;
    let mut v = vec![0u8; n as usize];
    r.read_exact(&mut v)?;
    Ok(Some(v))
}

fn be32(v: &[u8], at: usize) -> Option<u32> {
    v.get(at..at + 4).map(|s| u32::from_be_bytes([s[0], s[1], s[2], s[3]]))
}

fn be64(v: &[u8], at: usize) -> Option<u64> {
    v.get(at..at + 8).map(|s| u64::from_be_bytes(s.try_into().unwrap()))
}

/// The timescale of an `mvhd` or `mdhd` body (same layout up to it).
fn timescale(v: &[u8]) -> Option<u32> {
    let at = if *v.first()? == 1 { 20 } else { 12 };
    be32(v, at).filter(|&t| t > 0)
}

/// The first non-empty `elst` entry: (segment_duration in movie units,
/// media_time in media units).
fn first_edit(v: &[u8]) -> Option<(u64, u64)> {
    let version = *v.first()?;
    let count = be32(v, 4)? as usize;
    let (step, mut at) = (if version == 1 { 20 } else { 12 }, 8usize);
    for _ in 0..count.min(1024) {
        let (dur, time) = if version == 1 {
            (be64(v, at)?, be64(v, at + 8)? as i64)
        } else {
            (be32(v, at)? as u64, be32(v, at + 4)? as i32 as i64)
        };
        at += step;
        // media_time -1 is an empty edit (presentation silence), not audio.
        if time >= 0 {
            return Some((dur, time as u64));
        }
    }
    None
}

/// An edit as the file states it, in its own timescales: converted to frames
/// with [`RawEdit::at`] once the decoded rate is known.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct RawEdit {
    media_time: u64,
    media_ts: u32,
    duration: u64,
    movie_ts: u32,
    /// The length is exact to the frame (an `iTunSMPB` count), so a
    /// streaming decode cuts the packet that straddles the end. An `elst`
    /// end keeps that packet whole, as ffmpeg does.
    exact_end: bool,
}

impl RawEdit {
    /// Whether the stated length is exact to the frame (see `exact_end`).
    pub fn exact_end(&self) -> bool {
        self.exact_end
    }

    /// The edit in frames at `rate`, or `None` when it trims nothing or
    /// states a position past [`MAX_EDIT_FRAMES`]: a corrupt edit or tag
    /// leaves the file untrimmed rather than wrap into a tiny length.
    pub fn at(&self, rate: u32) -> Option<Edit> {
        let to_frames = |t: u64, ts: u32| u64::try_from((t as u128 * rate as u128 + ts as u128 / 2) / ts as u128).ok().filter(|&f| f <= MAX_EDIT_FRAMES);
        let skip = to_frames(self.media_time, self.media_ts)?;
        let keep = match self.duration {
            0 => None,
            d => Some(to_frames(d, self.movie_ts)?),
        };
        (skip > 0 || keep.is_some()).then_some(Edit { skip, keep })
    }
}

/// Read the edit of the first sound track in `r`, converted to frames at
/// `rate`. `Ok(None)` when the file is not MP4, has no sound track, no edit
/// list, or an edit with nothing to trim.
pub fn read_edit<R: Read + Seek>(r: &mut R, rate: u32) -> std::io::Result<Option<Edit>> {
    Ok(read_raw_edit(r)?.and_then(|e| e.at(rate)))
}

/// Read the edit of the first sound track in `r` as the file states it.
pub fn read_raw_edit<R: Read + Seek>(r: &mut R) -> std::io::Result<Option<RawEdit>> {
    let len = r.seek(SeekFrom::End(0))?;
    let top = children(r, 0, len)?;
    if top.first().map(|b| &b.kind) != Some(b"ftyp") {
        return Ok(None);
    }
    let Some(moov) = top.iter().find(|b| &b.kind == b"moov") else { return Ok(None) };
    let moov_kids = children(r, moov.body, moov.end)?;
    let Some(movie_ts) = moov_kids.iter().find(|b| &b.kind == b"mvhd").map(|b| body(r, b)).transpose()?.flatten().and_then(|v| timescale(&v)) else {
        return Ok(None);
    };
    for trak in moov_kids.iter().filter(|b| &b.kind == b"trak") {
        let kids = children(r, trak.body, trak.end)?;
        let Some(mdia) = kids.iter().find(|b| &b.kind == b"mdia") else { continue };
        let mdia_kids = children(r, mdia.body, mdia.end)?;
        let handler = match mdia_kids.iter().find(|b| &b.kind == b"hdlr") {
            Some(h) => body(r, h)?.and_then(|v| v.get(8..12).map(|s| [s[0], s[1], s[2], s[3]])),
            None => None,
        };
        if handler != Some(*b"soun") {
            continue;
        }
        let Some(media_ts) = mdia_kids.iter().find(|b| &b.kind == b"mdhd").map(|b| body(r, b)).transpose()?.flatten().and_then(|v| timescale(&v)) else {
            return Ok(None);
        };
        let elst = match kids.iter().find(|b| &b.kind == b"edts") {
            Some(edts) => children(r, edts.body, edts.end)?.into_iter().find(|b| &b.kind == b"elst"),
            None => None,
        };
        let Some(elst) = elst else {
            // No edit list: the iTunes gapless tag, counted in the track's own
            // sample frames, so both timescales are the media timescale.
            return Ok(itunsmpb(r, &moov_kids)?.map(|(delay, count)| RawEdit { media_time: delay, media_ts, duration: count, movie_ts: media_ts, exact_end: true }));
        };
        let Some((dur, time)) = body(r, &elst)?.and_then(|v| first_edit(&v)) else { return Ok(None) };
        return Ok(Some(RawEdit { media_time: time, media_ts, duration: dur, movie_ts, exact_end: false }));
    }
    Ok(None)
}

/// The (priming delay, original sample count) of the `iTunSMPB` tag under
/// `moov/udta/meta/ilst`, or `None` when the file has no such tag or it does
/// not parse. A count of 0 means the tag states no length.
fn itunsmpb<R: Read + Seek>(r: &mut R, moov_kids: &[BoxHead]) -> std::io::Result<Option<(u64, u64)>> {
    let Some(udta) = moov_kids.iter().find(|b| &b.kind == b"udta") else { return Ok(None) };
    let udta_kids = children(r, udta.body, udta.end)?;
    let Some(meta) = udta_kids.iter().find(|b| &b.kind == b"meta") else { return Ok(None) };
    // `meta` is a FullBox in ISO files (4 bytes of version and flags before
    // its children) and a plain box in QuickTime ones; take whichever holds
    // the `ilst`.
    let mut ilst = None;
    for from in [meta.body + 4, meta.body] {
        ilst = children(r, from, meta.end)?.into_iter().find(|b| &b.kind == b"ilst");
        if ilst.is_some() {
            break;
        }
    }
    let Some(ilst) = ilst else { return Ok(None) };
    for item in children(r, ilst.body, ilst.end)?.iter().filter(|b| &b.kind == b"----") {
        let parts = children(r, item.body, item.end)?;
        // `mean` and `name` are FullBoxes too: the text follows 4 bytes.
        let text = |r: &mut R, kind: &[u8; 4], skip: usize| -> std::io::Result<Option<Vec<u8>>> {
            match parts.iter().find(|b| &b.kind == kind) {
                Some(b) => Ok(body(r, b)?.and_then(|v| v.get(skip..).map(<[u8]>::to_vec))),
                None => Ok(None),
            }
        };
        // A free-form key is its `mean` namespace and its `name` together.
        if text(r, b"name", 4)?.as_deref() != Some(b"iTunSMPB") || text(r, b"mean", 4)?.as_deref() != Some(b"com.apple.iTunes") {
            continue;
        }
        // `data`: 4 bytes of type, 4 of locale, then the ASCII value.
        return Ok(text(r, b"data", 8)?.and_then(|v| parse_itunsmpb(&v)));
    }
    Ok(None)
}

/// Parse an `iTunSMPB` value: space-separated hex words, the second the
/// priming delay, the third the end padding, the fourth the original sample
/// count. A delay of zero with no count trims nothing and reads as `None`.
fn parse_itunsmpb(v: &[u8]) -> Option<(u64, u64)> {
    let s = std::str::from_utf8(v).ok()?;
    let words: Vec<u64> = s.split_whitespace().take(4).map(|w| u64::from_str_radix(w, 16).ok()).collect::<Option<_>>()?;
    let (&delay, &count) = (words.get(1)?, words.get(3)?);
    // An absurd delay (over 10 s at 48 kHz) is a corrupt tag, not priming.
    if delay > 480_000 || (delay == 0 && count == 0) {
        return None;
    }
    Some((delay, count))
}

/// Apply `edit` to interleaved stereo `pcm`: drop `skip` frames from the
/// front, then keep at most `keep` frames. An edit that would leave nothing is
/// ignored rather than turn a decodable file into silence.
pub fn apply_stereo(pcm: &mut Vec<f32>, edit: Edit) {
    let frames = (pcm.len() / 2) as u64;
    if edit.skip >= frames {
        return;
    }
    pcm.drain(..(edit.skip as usize) * 2);
    if let Some(keep) = edit.keep {
        if keep > 0 && keep < frames - edit.skip {
            pcm.truncate(keep as usize * 2);
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn bx(kind: &[u8; 4], body: &[u8]) -> Vec<u8> {
        let mut v = ((body.len() + 8) as u32).to_be_bytes().to_vec();
        v.extend_from_slice(kind);
        v.extend_from_slice(body);
        v
    }

    fn hd(ts: u32) -> Vec<u8> {
        // version 0, flags, creation, modification, timescale, duration
        let mut v = vec![0u8; 12];
        v.extend_from_slice(&ts.to_be_bytes());
        v.extend_from_slice(&0u32.to_be_bytes());
        v
    }

    fn elst(entries: &[(u32, i32)]) -> Vec<u8> {
        let mut v = vec![0u8; 4];
        v.extend_from_slice(&(entries.len() as u32).to_be_bytes());
        for (d, t) in entries {
            v.extend_from_slice(&d.to_be_bytes());
            v.extend_from_slice(&t.to_be_bytes());
            v.extend_from_slice(&0x0001_0000u32.to_be_bytes());
        }
        v
    }

    fn file(handler: &[u8; 4], edits: Option<&[(u32, i32)]>) -> Vec<u8> {
        tagged(handler, edits, None)
    }

    /// The `moov/udta/meta/ilst` of an iTunes-tagged file holding `smpb`.
    fn udta(smpb: &str) -> Vec<u8> {
        udta_in(b"com.apple.iTunes", smpb)
    }

    fn udta_in(namespace: &[u8], smpb: &str) -> Vec<u8> {
        let mean = bx(b"mean", &[&[0u8; 4][..], namespace].concat());
        let name = bx(b"name", &[&[0u8; 4][..], b"iTunSMPB"].concat());
        let data = bx(b"data", &[&[0, 0, 0, 1, 0, 0, 0, 0][..], smpb.as_bytes()].concat());
        let other = bx(b"----", &[mean.clone(), bx(b"name", b"\0\0\0\0iTunNORM"), bx(b"data", b"\0\0\0\x01\0\0\0\0 1 2")].concat());
        let ilst = bx(b"ilst", &[other, bx(b"----", &[mean, name, data].concat())].concat());
        bx(b"udta", &bx(b"meta", &[&[0u8; 4][..], &bx(b"hdlr", &[0u8; 25]), &ilst].concat()))
    }

    fn tagged(handler: &[u8; 4], edits: Option<&[(u32, i32)]>, extra: Option<Vec<u8>>) -> Vec<u8> {
        let mut hdlr = vec![0u8; 8];
        hdlr.extend_from_slice(handler);
        hdlr.extend_from_slice(&[0u8; 12]);
        let mdia = [bx(b"mdhd", &hd(44100)), bx(b"hdlr", &hdlr)].concat();
        let mut trak = Vec::new();
        if let Some(e) = edits {
            trak.extend(bx(b"edts", &bx(b"elst", &elst(e))));
        }
        trak.extend(bx(b"mdia", &mdia));
        let moov = [bx(b"mvhd", &hd(1000)), bx(b"trak", &trak), extra.unwrap_or_default()].concat();
        [bx(b"ftyp", b"M4A \0\0\0\0"), bx(b"moov", &moov)].concat()
    }

    #[test]
    fn an_aac_priming_edit_is_read_in_frames() {
        // ffmpeg's AAC encoder: media_time 1024, 20 s long in movie units.
        let f = file(b"soun", Some(&[(20_000, 1024)]));
        let e = read_edit(&mut Cursor::new(f), 44100).unwrap().unwrap();
        assert_eq!(e, Edit { skip: 1024, keep: Some(882_000) });
    }

    #[test]
    fn an_empty_edit_is_passed_over_for_the_first_real_one() {
        let f = file(b"soun", Some(&[(500, -1), (20_000, 2112)]));
        assert_eq!(read_edit(&mut Cursor::new(f), 44100).unwrap().unwrap().skip, 2112);
    }

    #[test]
    fn no_edit_no_sound_track_or_no_mp4_means_nothing_to_trim() {
        assert_eq!(read_edit(&mut Cursor::new(file(b"soun", None)), 44100).unwrap(), None);
        assert_eq!(read_edit(&mut Cursor::new(file(b"vide", Some(&[(1, 1024)]))), 44100).unwrap(), None);
        let wav = b"RIFF\x24\0\0\0WAVEfmt ".to_vec();
        assert_eq!(read_edit(&mut Cursor::new(wav), 44100).unwrap(), None);
        // A truncated box header is the end of the list, not a panic.
        let mut cut = file(b"soun", Some(&[(20_000, 1024)]));
        cut.truncate(30);
        assert_eq!(read_edit(&mut Cursor::new(cut), 44100).unwrap(), None);
    }

    /// Apple's encoder: 2112 frames of priming, 0x1CA of padding, 0x4AF0F6
    /// original frames, and no edit list.
    const APPLE: &str = " 00000000 00000840 000001CA 00000000004AF0F6 00000000 00000000 00000000 00000000 00000000 00000000 00000000 00000000";

    #[test]
    fn with_no_edit_list_the_itunes_gapless_tag_states_the_trim() {
        let f = tagged(b"soun", None, Some(udta(APPLE)));
        let e = read_edit(&mut Cursor::new(f), 44100).unwrap().unwrap();
        assert_eq!(e, Edit { skip: 2112, keep: Some(0x4AF0F6) });
        // Decoded at a different rate than the track's, it scales like an edit.
        let f = tagged(b"soun", None, Some(udta(APPLE)));
        assert_eq!(read_edit(&mut Cursor::new(f), 88200).unwrap().unwrap().skip, 4224);
    }

    #[test]
    fn an_itunsmpb_name_outside_apples_namespace_is_not_the_gapless_tag() {
        let f = tagged(b"soun", None, Some(udta_in(b"com.example.vendor", APPLE)));
        assert_eq!(read_edit(&mut Cursor::new(f), 44100).unwrap(), None);
    }

    #[test]
    fn an_edit_list_wins_over_the_itunes_gapless_tag() {
        let f = tagged(b"soun", Some(&[(20_000, 1024)]), Some(udta(APPLE)));
        assert_eq!(read_edit(&mut Cursor::new(f), 44100).unwrap().unwrap(), Edit { skip: 1024, keep: Some(882_000) });
    }

    #[test]
    fn a_quicktime_style_meta_without_version_bytes_is_read_too() {
        let full = udta(APPLE);
        // Drop the 4 version/flags bytes and fix the two enclosing sizes.
        let meta = &full[8..];
        let plain = bx(b"udta", &bx(b"meta", &meta[12..]));
        let f = tagged(b"soun", None, Some(plain));
        assert_eq!(read_edit(&mut Cursor::new(f), 44100).unwrap().unwrap().skip, 2112);
    }

    #[test]
    fn a_missing_or_unreadable_gapless_tag_trims_nothing() {
        let huge = " 00000000 00000840 00000000 FFFFFFFFFFFFFFFF";
        for bad in ["", "garbage", huge, " 00000000 00000000 00000000 0000000000000000", " 00000000 FFFFFFFF 00000000 0000000000000010", " 00000000 00000840"] {
            let f = tagged(b"soun", None, Some(udta(bad)));
            assert_eq!(read_edit(&mut Cursor::new(f), 44100).unwrap(), None, "{bad:?}");
        }
        // A delay with no stated length still trims the front.
        let f = tagged(b"soun", None, Some(udta(" 00000000 00000840 00000000 0000000000000000")));
        assert_eq!(read_edit(&mut Cursor::new(f), 44100).unwrap().unwrap(), Edit { skip: 2112, keep: None });
    }

    #[test]
    fn an_edit_past_the_frame_bound_is_ignored_not_wrapped() {
        let raw = |media_time, duration| RawEdit { media_time, media_ts: 44100, duration, movie_ts: 44100, exact_end: false };
        assert_eq!(raw(1024, u64::MAX).at(44100), None);
        assert_eq!(raw(u64::MAX, 0).at(44100), None);
        assert_eq!(raw(1024, MAX_EDIT_FRAMES + 1).at(44100), None);
        // The bound itself is still an edit.
        assert_eq!(raw(1024, MAX_EDIT_FRAMES).at(44100), Some(Edit { skip: 1024, keep: Some(MAX_EDIT_FRAMES) }));
    }

    #[test]
    fn applying_an_edit_trims_the_front_and_the_tail() {
        let mut pcm: Vec<f32> = (0..20).map(|i| i as f32).collect(); // 10 frames
        apply_stereo(&mut pcm, Edit { skip: 2, keep: Some(5) });
        assert_eq!(pcm, vec![4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0, 11.0, 12.0, 13.0]);
    }

    #[test]
    fn an_edit_that_would_leave_nothing_is_ignored() {
        let mut pcm = vec![0.5f32; 8];
        apply_stereo(&mut pcm, Edit { skip: 4, keep: None });
        assert_eq!(pcm.len(), 8);
        // A keep longer than what decoded leaves the decode as it is.
        apply_stereo(&mut pcm, Edit { skip: 0, keep: Some(99) });
        assert_eq!(pcm.len(), 8);
    }
}
