//! The MP4 edit list of a file's audio track, for `odj-audio decode` only.
//!
//! symphonia 0.6's MP4 reader parses `edts/elst` but does not apply it, and
//! its AAC decoder trims nothing, so an M4A decodes with the encoder's
//! priming (usually 1024 frames, 23.2 ms at 44.1 kHz) in front of the audio.
//! ffmpeg starts the audio at the edit's `media_time` instead, so a stem
//! separated from symphonia's decode would sit that far early against the
//! source as every ffmpeg-based player decodes it. `decode` reads the edit
//! list here and trims the way ffmpeg 6.1 does: `media_time` frames off the
//! front, and every packet that starts at or after the edit's end dropped (a
//! packet that straddles the end is kept whole, as ffmpeg keeps it).
//!
//! Only the shape real encoders write is applied: one edit, `media_time >= 0`,
//! rate 1. Anything else (an empty edit inserting silence, several edits, a
//! rate) is reported as ignored and the file decodes untrimmed, so the caller
//! sees it rather than a guess.
//!
//! The deck's decode (`decode::decode_open_within`) does not use this: its
//! alignment feeds beatgrids and cues, and changing it needs its own
//! measurement.

use std::fs::File;
use std::io::{Read, Seek, SeekFrom};
use std::path::Path;

/// What the edit list asks for, in the audio track's sample frames.
#[derive(Clone, Debug, PartialEq, Eq)]
pub enum Edit {
    /// No MP4 container, or no edit list on the audio track.
    None,
    /// Trim `skip` frames from the front; drop packets that start at or after
    /// `skip + keep` frames (`keep` None: no end).
    Apply { skip: u64, keep: Option<u64> },
    /// An edit list this reader does not apply; the decode is untrimmed.
    Ignored(String),
}

/// The moov box is metadata; past this it is not a real one.
const MAX_MOOV_BYTES: u64 = 64 << 20;

/// Read `path`'s edit list for the audio track `track_id` (as symphonia
/// numbers it, the `tkhd` track id), converted to frames at `sample_rate`.
pub fn read(path: &Path, track_id: u32, sample_rate: u32) -> Edit {
    match moov(path) {
        Ok(Some(moov)) => from_moov(&moov, track_id, sample_rate),
        Ok(None) => Edit::None,
        Err(e) => Edit::Ignored(format!("cannot read the MP4 boxes: {e}")),
    }
}

/// The bytes of the top-level `moov` box, or None when the file is not MP4.
fn moov(path: &Path) -> std::io::Result<Option<Vec<u8>>> {
    let mut f = File::open(path)?;
    let len = f.metadata()?.len();
    let mut head = [0u8; 8];
    if len < 8 {
        return Ok(None);
    }
    f.read_exact(&mut head)?;
    if &head[4..8] != b"ftyp" {
        return Ok(None);
    }
    let mut off = 0u64;
    while off + 8 <= len {
        f.seek(SeekFrom::Start(off))?;
        f.read_exact(&mut head)?;
        let mut size = u64::from(u32::from_be_bytes([head[0], head[1], head[2], head[3]]));
        let mut hdr = 8u64;
        if size == 1 {
            let mut big = [0u8; 8];
            f.read_exact(&mut big)?;
            size = u64::from_be_bytes(big);
            hdr = 16;
        } else if size == 0 {
            size = len - off;
        }
        if size < hdr {
            return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "box smaller than its header"));
        }
        if &head[4..8] == b"moov" {
            let body = size - hdr;
            if body > MAX_MOOV_BYTES {
                return Err(std::io::Error::new(std::io::ErrorKind::InvalidData, "moov box over 64 MiB"));
            }
            let mut buf = vec![0u8; body as usize];
            f.read_exact(&mut buf)?;
            return Ok(Some(buf));
        }
        off += size;
    }
    Ok(None)
}

/// Child boxes of `b`: (type, body).
fn children(b: &[u8]) -> Vec<([u8; 4], &[u8])> {
    let mut out = Vec::new();
    let mut off = 0usize;
    while off + 8 <= b.len() {
        let mut size = u32::from_be_bytes([b[off], b[off + 1], b[off + 2], b[off + 3]]) as u64;
        let typ = [b[off + 4], b[off + 5], b[off + 6], b[off + 7]];
        let mut hdr = 8usize;
        if size == 1 {
            if off + 16 > b.len() {
                break;
            }
            size = u64::from_be_bytes(b[off + 8..off + 16].try_into().unwrap());
            hdr = 16;
        } else if size == 0 {
            size = (b.len() - off) as u64;
        }
        let end = match usize::try_from(size).ok().and_then(|s| off.checked_add(s)) {
            Some(e) if size as usize >= hdr && e <= b.len() => e,
            _ => break,
        };
        out.push((typ, &b[off + hdr..end]));
        off = end;
    }
    out
}

fn child<'a>(b: &'a [u8], typ: &[u8; 4]) -> Option<&'a [u8]> {
    children(b).into_iter().find(|(t, _)| t == typ).map(|(_, body)| body)
}

fn be_u32(b: &[u8], at: usize) -> Option<u32> {
    b.get(at..at + 4).map(|s| u32::from_be_bytes(s.try_into().unwrap()))
}

fn be_u64(b: &[u8], at: usize) -> Option<u64> {
    b.get(at..at + 8).map(|s| u64::from_be_bytes(s.try_into().unwrap()))
}

/// The timescale of an `mvhd` or `mdhd` body (same layout up to it).
fn timescale(b: &[u8]) -> Option<u32> {
    match b.first()? {
        0 => be_u32(b, 12),
        1 => be_u32(b, 20),
        _ => None,
    }
}

fn tkhd_track_id(b: &[u8]) -> Option<u32> {
    match b.first()? {
        0 => be_u32(b, 12),
        1 => be_u32(b, 20),
        _ => None,
    }
}

fn is_sound(trak: &[u8]) -> bool {
    child(trak, b"mdia").and_then(|m| child(m, b"hdlr")).and_then(|h| h.get(8..12)) == Some(b"soun".as_slice())
}

/// (segment_duration in movie units, media_time in media units, rate) per entry.
fn elst_entries(b: &[u8]) -> Option<Vec<(u64, i64, u32)>> {
    let version = *b.first()?;
    let n = be_u32(b, 4)? as usize;
    let mut out = Vec::with_capacity(n.min(16));
    let mut at = 8usize;
    for _ in 0..n {
        let (d, m, step) = match version {
            0 => (u64::from(be_u32(b, at)?), i64::from(be_u32(b, at + 4)? as i32), 8),
            1 => (be_u64(b, at)?, be_u64(b, at + 8)? as i64, 16),
            _ => return None,
        };
        let rate = be_u32(b, at + step)?;
        out.push((d, m, rate));
        at += step + 4;
    }
    Some(out)
}

fn from_moov(moov: &[u8], track_id: u32, sample_rate: u32) -> Edit {
    let traks: Vec<&[u8]> = children(moov).into_iter().filter(|(t, _)| t == b"trak").map(|(_, b)| b).collect();
    let by_id = traks.iter().copied().find(|t| child(t, b"tkhd").and_then(tkhd_track_id) == Some(track_id));
    let Some(trak) = by_id.or_else(|| traks.iter().copied().find(|t| is_sound(t))) else {
        return Edit::None;
    };
    let Some(elst) = child(trak, b"edts").and_then(|e| child(e, b"elst")) else {
        return Edit::None;
    };
    let Some(entries) = elst_entries(elst) else {
        return Edit::Ignored("unreadable elst".into());
    };
    let [(segment, media_time, rate)] = entries[..] else {
        return Edit::Ignored(format!("{} edits (only one is applied)", entries.len()));
    };
    if media_time < 0 {
        return Edit::Ignored("an empty edit (leading silence) is not applied".into());
    }
    if rate != 0x0001_0000 {
        return Edit::Ignored(format!("edit rate {rate:#x} is not 1"));
    }
    let (Some(movie_ts), Some(media_ts)) = (
        child(moov, b"mvhd").and_then(timescale),
        child(trak, b"mdia").and_then(|m| child(m, b"mdhd")).and_then(timescale),
    ) else {
        return Edit::Ignored("no mvhd/mdhd timescale".into());
    };
    if movie_ts == 0 || media_ts == 0 {
        return Edit::Ignored("zero timescale".into());
    }
    let to_frames = |v: u64, ts: u32| (u128::from(v) * u128::from(sample_rate) + u128::from(ts) / 2) / u128::from(ts);
    let skip = to_frames(media_time as u64, media_ts) as u64;
    let keep = (segment > 0).then(|| to_frames(segment, movie_ts) as u64);
    Edit::Apply { skip, keep }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn bx(typ: &[u8; 4], body: &[u8]) -> Vec<u8> {
        let mut v = ((body.len() + 8) as u32).to_be_bytes().to_vec();
        v.extend_from_slice(typ);
        v.extend_from_slice(body);
        v
    }

    fn header_v0(ts: u32) -> Vec<u8> {
        // version/flags, creation, modification, timescale, duration
        [0u32, 0, 0, ts, 0].iter().flat_map(|x| x.to_be_bytes()).collect()
    }

    fn moov_with(elst: &[u8], track_id: u32) -> Vec<u8> {
        let tkhd: Vec<u8> = [0u32, 0, 0, track_id, 0].iter().flat_map(|x| x.to_be_bytes()).collect();
        let hdlr: Vec<u8> = [0u8; 8].iter().copied().chain(*b"soun").chain([0u8; 12]).collect();
        let mdia = [bx(b"mdhd", &header_v0(44100)), bx(b"hdlr", &hdlr)].concat();
        let trak = [bx(b"tkhd", &tkhd), bx(b"edts", &bx(b"elst", elst)), bx(b"mdia", &mdia)].concat();
        [bx(b"mvhd", &header_v0(1000)), bx(b"trak", &trak)].concat()
    }

    fn elst_v0(entries: &[(u32, i32)]) -> Vec<u8> {
        let mut v = vec![0u8; 4];
        v.extend_from_slice(&(entries.len() as u32).to_be_bytes());
        for (d, m) in entries {
            v.extend_from_slice(&d.to_be_bytes());
            v.extend_from_slice(&m.to_be_bytes());
            v.extend_from_slice(&0x0001_0000u32.to_be_bytes());
        }
        v
    }

    #[test]
    fn one_edit_trims_the_priming_and_bounds_the_end() {
        // What ffmpeg's AAC encoder writes: 60 s at 1 kHz movie units, 1024 frames in.
        let moov = moov_with(&elst_v0(&[(60000, 1024)]), 1);
        assert_eq!(from_moov(&moov, 1, 44100), Edit::Apply { skip: 1024, keep: Some(2_646_000) });
        // Version 1 (64-bit fields) reads the same.
        let mut v1 = vec![1u8, 0, 0, 0];
        v1.extend_from_slice(&1u32.to_be_bytes());
        v1.extend_from_slice(&60000u64.to_be_bytes());
        v1.extend_from_slice(&1024i64.to_be_bytes());
        v1.extend_from_slice(&0x0001_0000u32.to_be_bytes());
        assert_eq!(from_moov(&moov_with(&v1, 1), 1, 44100), Edit::Apply { skip: 1024, keep: Some(2_646_000) });
    }

    #[test]
    fn shapes_it_does_not_apply_are_reported_not_guessed() {
        let empty_edit = moov_with(&elst_v0(&[(500, -1), (60000, 0)]), 1);
        assert!(matches!(from_moov(&empty_edit, 1, 44100), Edit::Ignored(_)));
        let leading_empty_only = moov_with(&elst_v0(&[(500, -1)]), 1);
        assert!(matches!(from_moov(&leading_empty_only, 1, 44100), Edit::Ignored(_)));
        // Control: no edit list at all is "none", not "ignored".
        let tkhd: Vec<u8> = [0u32, 0, 0, 1, 0].iter().flat_map(|x| x.to_be_bytes()).collect();
        let bare = [bx(b"mvhd", &header_v0(1000)), bx(b"trak", &bx(b"tkhd", &tkhd))].concat();
        assert_eq!(from_moov(&bare, 1, 44100), Edit::None);
    }
}
