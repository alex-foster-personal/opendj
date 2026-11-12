//! Art-Net ArtDmx encoder goldens (issue #11780): wire layout without NIC or node.

use odj_audio::lighting::artnet::{
    encode_artdmx, ArtNetError, ARTDMX_HEADER_LEN, ARTNET_ID, MAX_DMX_SLOTS, OPCODE_ARTDMX,
    PROTOCOL_VERSION,
};
use sha2::{Digest, Sha256};

const GOLDEN_HEX: &str = include_str!("fixtures/lighting/artdmx-universe1.hex");
const GOLDEN_SHA256: &str = "f76255e44ccb8a44ee305c85fdc79e43e1eaa7962f6cf278567d6e564fca0bc2";

fn parse_hex_fixture(text: &str) -> Vec<u8> {
    text.split_whitespace()
        .map(|pair| u8::from_str_radix(pair, 16).expect("fixture hex pair"))
        .collect()
}

fn sha256_hex(data: &[u8]) -> String {
    let digest = Sha256::digest(data);
    digest.iter().map(|b| format!("{b:02x}")).collect()
}

#[test]
fn lighting_artnet_golden_universe1_sequence7_512_channels() {
    let channels: Vec<u8> = (0..512).map(|i| ((i * 7) & 0xFF) as u8).collect();
    let encoded = encode_artdmx(1, 7, 0, &channels).expect("encode full universe");
    let golden = parse_hex_fixture(GOLDEN_HEX);

    assert_eq!(encoded.len(), 530);
    assert_eq!(encoded, golden);
    assert_eq!(sha256_hex(&encoded), GOLDEN_SHA256);

    assert_eq!(&encoded[0..8], ARTNET_ID);
    assert_eq!(
        u16::from_le_bytes(encoded[8..10].try_into().unwrap()),
        OPCODE_ARTDMX
    );
    assert_eq!(
        u16::from_be_bytes(encoded[10..12].try_into().unwrap()),
        PROTOCOL_VERSION
    );
    assert_eq!(encoded[12], 7);
    assert_eq!(encoded[13], 0);
    assert_eq!(encoded[14], 1);
    assert_eq!(encoded[15], 0);
    assert_eq!(u16::from_be_bytes(encoded[16..18].try_into().unwrap()), 512);
    assert_eq!(encoded.len(), ARTDMX_HEADER_LEN + 512);
}

#[test]
fn lighting_artnet_pads_shorter_vectors() {
    let three = encode_artdmx(1, 0, 0, &[0xAA, 0xBB, 0xCC]).expect("pad three");
    assert_eq!(three.len(), 22);
    assert_eq!(u16::from_be_bytes(three[16..18].try_into().unwrap()), 4);
    assert_eq!(&three[18..], &[0xAA, 0xBB, 0xCC, 0x00]);

    let one = encode_artdmx(1, 0, 0, &[0xAA]).expect("pad one");
    assert_eq!(one.len(), ARTDMX_HEADER_LEN + 2);
    assert_eq!(u16::from_be_bytes(one[16..18].try_into().unwrap()), 2);
    assert_eq!(&one[18..], &[0xAA, 0x00]);

    let empty = encode_artdmx(1, 0, 0, &[]).expect("pad empty");
    assert_eq!(u16::from_be_bytes(empty[16..18].try_into().unwrap()), 2);
    assert_eq!(&empty[18..], &[0x00, 0x00]);
}

#[test]
fn lighting_artnet_sequence_255_then_0() {
    let payload = [0u8; 2];
    let first = encode_artdmx(1, 255, 0, &payload).expect("seq 255");
    let second = encode_artdmx(1, 0, 0, &payload).expect("seq 0");
    assert_eq!(first[12], 255);
    assert_eq!(second[12], 0);
}

#[test]
fn lighting_artnet_universe_out_of_range() {
    let payload = [0u8; 2];
    for bad in [32768_u16, 65535] {
        let err = encode_artdmx(bad, 0, 0, &payload).unwrap_err();
        assert_eq!(err, ArtNetError::UniverseOutOfRange { universe: bad });
    }
}

#[test]
fn lighting_artnet_packet_too_large() {
    let big = vec![0u8; MAX_DMX_SLOTS + 1];
    let err = encode_artdmx(1, 0, 0, &big).unwrap_err();
    assert_eq!(
        err,
        ArtNetError::PacketTooLarge {
            slots: MAX_DMX_SLOTS + 1
        }
    );
}
