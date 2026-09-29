//! Decoding a real compressed file with a damaged packet in it.

mod common;

use std::path::PathBuf;

use odj_audio::decode::{decode_file, decode_file_within};
use symphonia::core::codecs::audio::AudioDecoderOptions;
use symphonia::core::errors::Error as SymError;
use symphonia::core::formats::probe::Hint;
use symphonia::core::formats::{FormatOptions, TrackType};
use symphonia::core::io::MediaSourceStream;
use symphonia::core::meta::MetadataOptions;

/// A tracked AAC clip from the analysis bench (44.1 kHz, about 60 s).
fn clip() -> PathBuf {
    PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../scripts/bench/clips-edge/am-contra-heart-peripheral-cfg-a.m4a")
}

#[test]
fn a_corrupt_packet_keeps_the_timeline_after_it() {
    let clean = decode_file(&clip()).unwrap();
    // Three bytes flipped inside one AAC packet: symphonia reports that
    // packet as a decode error and decodes the rest.
    let mut bytes = std::fs::read(clip()).unwrap();
    for b in &mut bytes[1_565_108..1_565_111] {
        *b ^= 0x5A;
    }
    let dir = common::temp_dir("corrupt");
    let damaged = dir.join("damaged.m4a");
    std::fs::write(&damaged, &bytes).unwrap();
    let got = decode_file(&damaged).unwrap();
    // Same length: the lost packet plays as silence of its own length rather
    // than vanishing (which took 1024 frames out of the middle).
    assert_eq!(got.pcm.len(), clean.pcm.len());
    assert_ne!(got.pcm, clean.pcm, "the damage must reach the decoder, or this tests nothing");
    // Everything well after the damage is where the file puts it, sample for
    // sample: a shifted tail would not match.
    let tail = 44100 * 2;
    assert_eq!(got.pcm[got.pcm.len() - tail..], clean.pcm[clean.pcm.len() - tail..]);
    std::fs::remove_dir_all(dir).ok();
}

/// Byte ranges of the clip's audio packets, from its MP4 sample tables
/// (`stsz` sizes, `stco` chunk offsets, `stsc` packets per chunk).
fn packets(b: &[u8]) -> Vec<(usize, usize)> {
    let u32_at = |i: usize| u32::from_be_bytes(b[i..i + 4].try_into().unwrap()) as usize;
    // The payload range of the first `name` box inside `(from, to)`.
    let child = |(from, to): (usize, usize), name: &[u8]| {
        let mut i = from;
        while i + 8 <= to {
            let size = u32_at(i);
            if &b[i + 4..i + 8] == name {
                return (i + 8, i + size);
            }
            i += size;
        }
        panic!("no {} box", String::from_utf8_lossy(name));
    };
    let stbl = [&b"moov"[..], b"trak", b"mdia", b"minf", b"stbl"].iter().fold((0, b.len()), |r, n| child(r, n));
    let (sz, _) = child(stbl, b"stsz");
    assert_eq!(u32_at(sz + 4), 0, "the clip's packets vary in size");
    let sizes: Vec<usize> = (0..u32_at(sz + 8)).map(|k| u32_at(sz + 12 + 4 * k)).collect();
    let (co, _) = child(stbl, b"stco");
    let chunks: Vec<usize> = (0..u32_at(co + 4)).map(|k| u32_at(co + 8 + 4 * k)).collect();
    let (sc, _) = child(stbl, b"stsc");
    let runs: Vec<(usize, usize)> = (0..u32_at(sc + 4)).map(|k| (u32_at(sc + 8 + 12 * k), u32_at(sc + 12 + 12 * k))).collect();
    let mut out = Vec::new();
    for (c, &start) in chunks.iter().enumerate() {
        let per = runs.iter().rev().find(|r| r.0 <= c + 1).unwrap().1;
        let mut at = start;
        for _ in 0..per {
            let len = sizes[out.len()];
            out.push((at, len));
            at += len;
        }
    }
    assert_eq!(out.len(), sizes.len());
    out
}

/// (decoded, decode errors, other errors) for each packet of `path`, read
/// straight through symphonia: proof of which branch the damage reaches.
fn outcomes(path: &std::path::Path) -> (usize, usize, usize) {
    let mss = MediaSourceStream::new(Box::new(std::fs::File::open(path).unwrap()), Default::default());
    let mut f = symphonia::default::get_probe()
        .probe(&Hint::new(), mss, FormatOptions::default(), MetadataOptions::default())
        .unwrap();
    let t = f.default_track(TrackType::Audio).unwrap();
    let id = t.id;
    let params = t.codec_params.as_ref().unwrap().audio().unwrap();
    let mut d = symphonia::default::get_codecs().make_audio_decoder(params, &AudioDecoderOptions::default()).unwrap();
    let mut n = (0, 0, 0);
    while let Ok(Some(p)) = f.next_packet() {
        if p.track_id == id {
            match d.decode(&p) {
                Ok(_) => n.0 += 1,
                Err(SymError::DecodeError(_)) => n.1 += 1,
                Err(_) => n.2 += 1,
            }
        }
    }
    n
}

#[test]
fn a_file_whose_every_packet_is_corrupt_does_not_load_as_silence() {
    let clean = decode_file(&clip()).unwrap();
    let bytes = std::fs::read(clip()).unwrap();
    let pk = packets(&bytes);
    // Three bytes flipped 16 bytes into a packet make symphonia report that
    // packet as a decode error, the branch that plays it as silence.
    let mut damaged = bytes.clone();
    for &(at, _) in &pk {
        for x in &mut damaged[at + 16..at + 19] {
            *x ^= 0x5A;
        }
    }
    let dir = common::temp_dir("all-corrupt");
    let all = dir.join("all.m4a");
    std::fs::write(&all, &damaged).unwrap();
    // The damage reaches the decoder and nothing else: every packet is a
    // decode error, so the refusal below is this check and not another one.
    assert_eq!(outcomes(&all), (0, pk.len(), 0));
    let Err(e) = decode_file(&all) else { panic!("an all-corrupt file loaded") };
    assert!(e.message.contains("no packet"), "{}", e.message);
    // Control: one corrupt packet among good ones still loads, full length,
    // that packet as silence (the damage from the test above).
    let mut one = bytes.clone();
    for x in &mut one[1_565_108..1_565_111] {
        *x ^= 0x5A;
    }
    let one_path = dir.join("one.m4a");
    std::fs::write(&one_path, &one).unwrap();
    assert_eq!(outcomes(&one_path), (pk.len() - 1, 1, 0));
    assert_eq!(decode_file(&one_path).unwrap().pcm.len(), clean.pcm.len());
    // So does a file whose LAST packet is the corrupt one: what counts is
    // that some packet decoded, not that the final one did.
    let mut last = bytes.clone();
    let (at, _) = pk[pk.len() - 1];
    for x in &mut last[at + 16..at + 19] {
        *x ^= 0x5A;
    }
    let last_path = dir.join("last.m4a");
    std::fs::write(&last_path, &last).unwrap();
    assert_eq!(outcomes(&last_path), (pk.len() - 1, 1, 0));
    // Its silence is the length the container gives the packet, which for
    // this clip's short final packet is 16 frames under what the decoder
    // makes of it; everything before it is untouched.
    let got = decode_file(&last_path).unwrap();
    assert!(got.pcm.len().abs_diff(clean.pcm.len()) <= 1024 * 2, "{} vs {}", got.pcm.len(), clean.pcm.len());
    let before = clean.pcm.len() - 2 * 2048 * 2;
    assert_eq!(got.pcm[..before], clean.pcm[..before]);
    std::fs::remove_dir_all(dir).ok();
}

#[test]
fn a_decode_holds_no_more_than_the_room_it_was_given() {
    // Codex's case, on the decode side: the samples grew by doubling, so a
    // track that fits its room could hold nearly twice it in spare capacity.
    let d = common::temp_dir("decode-capacity");
    let path = common::write_wav(&d, "a.wav", 48000, &common::sine(48000, 440.0, 2.5));
    let got = decode_file_within(&path, 130_000).unwrap();
    assert_eq!(got.pcm.len(), 240_000);
    assert!(got.pcm.capacity() <= 260_000, "holds {} samples", got.pcm.capacity());
    // Control: with no budget the same decode is free to grow past it, so
    // the bound above is the room's doing.
    let free = decode_file_within(&path, u64::MAX).unwrap();
    assert_eq!(free.pcm, got.pcm);
    assert!(free.pcm.capacity() > 260_000, "holds {} samples", free.pcm.capacity());
}
