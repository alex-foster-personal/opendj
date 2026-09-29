//! Decoding a real compressed file with a damaged packet in it.

mod common;

use std::path::PathBuf;

use odj_audio::decode::decode_file;

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
