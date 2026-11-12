//! Art-Net 4 ArtDmx packet encoder (clean-room from the public specification).
//! No sockets: callers own UDP output and sequencing policy.

use std::fmt;

pub const ARTNET_ID: &[u8; 8] = b"Art-Net\0";
pub const OPCODE_ARTDMX: u16 = 0x5000;
pub const PROTOCOL_VERSION: u16 = 14;
pub const ARTDMX_HEADER_LEN: usize = 18;
pub const MAX_DMX_SLOTS: usize = 512;
pub const MIN_DMX_SLOTS: usize = 2;
/// Inclusive upper bound for the 15-bit Port-Address on the wire.
pub const MAX_UNIVERSE: u16 = 32_767;

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum ArtNetError {
    UniverseOutOfRange { universe: u16 },
    PacketTooLarge { slots: usize },
}

impl fmt::Display for ArtNetError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            ArtNetError::UniverseOutOfRange { universe } => {
                write!(
                    f,
                    "universe (Port-Address) must be 0..={MAX_UNIVERSE}, got {universe}"
                )
            }
            ArtNetError::PacketTooLarge { slots } => {
                write!(
                    f,
                    "ArtDmx carries at most {MAX_DMX_SLOTS} slots, got {slots}"
                )
            }
        }
    }
}

impl std::error::Error for ArtNetError {}

/// Encode one ArtDmx packet. `universe` is the 15-bit Port-Address (Net:SubNet:Universe
/// flattened). `sequence` and `physical` are written verbatim; this module does not wrap
/// sequence after 255.
pub fn encode_artdmx(
    universe: u16,
    sequence: u8,
    physical: u8,
    channels: &[u8],
) -> Result<Vec<u8>, ArtNetError> {
    if universe > MAX_UNIVERSE {
        return Err(ArtNetError::UniverseOutOfRange { universe });
    }

    let mut slots: Vec<u8> = channels.to_vec();
    if slots.len() > MAX_DMX_SLOTS {
        return Err(ArtNetError::PacketTooLarge { slots: slots.len() });
    }
    if slots.len() % 2 != 0 {
        slots.push(0);
    }
    while slots.len() < MIN_DMX_SLOTS {
        slots.push(0);
    }

    let slot_count = slots.len();
    let mut out = Vec::with_capacity(ARTDMX_HEADER_LEN + slot_count);
    out.extend_from_slice(ARTNET_ID);
    out.extend_from_slice(&OPCODE_ARTDMX.to_le_bytes());
    out.extend_from_slice(&PROTOCOL_VERSION.to_be_bytes());
    out.push(sequence);
    out.push(physical);
    out.push((universe & 0xFF) as u8);
    out.push((universe >> 8) as u8);
    out.extend_from_slice(&(slot_count as u16).to_be_bytes());
    out.extend_from_slice(&slots);
    Ok(out)
}
