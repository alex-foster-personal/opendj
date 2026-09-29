//! Minimal WAV writers: 32-bit float for renders (exact, hashable), 16-bit
//! PCM for small test fixtures.

use std::io::{self, Write};

/// The most stereo f32 frames one WAV file holds: its sizes are 32-bit, so
/// the data chunk tops out just under 4 GiB (about 3.1 hours at 48 kHz).
pub const MAX_F32_FRAMES: u64 = (u32::MAX as u64 - 36) / 8;

fn header(w: &mut impl Write, format: u16, bits: u16, sr: u32, data_bytes: u32) -> io::Result<()> {
    let channels = 2u16;
    let block_align = channels * bits / 8;
    w.write_all(b"RIFF")?;
    w.write_all(&(36 + data_bytes).to_le_bytes())?;
    w.write_all(b"WAVEfmt ")?;
    w.write_all(&16u32.to_le_bytes())?;
    w.write_all(&format.to_le_bytes())?;
    w.write_all(&channels.to_le_bytes())?;
    w.write_all(&sr.to_le_bytes())?;
    w.write_all(&(sr * block_align as u32).to_le_bytes())?;
    w.write_all(&block_align.to_le_bytes())?;
    w.write_all(&bits.to_le_bytes())?;
    w.write_all(b"data")?;
    w.write_all(&data_bytes.to_le_bytes())
}

fn data_bytes(samples: usize, bytes_per_sample: usize) -> io::Result<u32> {
    u32::try_from(samples * bytes_per_sample)
        .ok()
        .filter(|&n| n <= u32::MAX - 36)
        .ok_or_else(|| io::Error::new(io::ErrorKind::InvalidInput, "render too long for a WAV file (4 GiB)"))
}

/// Interleaved stereo f32, IEEE float WAV (format 3).
pub fn write_f32(w: &mut impl Write, sr: u32, pcm: &[f32]) -> io::Result<()> {
    header(w, 3, 32, sr, data_bytes(pcm.len(), 4)?)?;
    for s in pcm {
        w.write_all(&s.to_le_bytes())?;
    }
    // A buffered writer's last write happens here: its error is returned,
    // not lost when the writer is dropped.
    w.flush()
}

/// Interleaved stereo, 16-bit PCM WAV (format 1), clamped.
pub fn write_i16(w: &mut impl Write, sr: u32, pcm: &[f32]) -> io::Result<()> {
    header(w, 1, 16, sr, data_bytes(pcm.len(), 2)?)?;
    for s in pcm {
        let v = (s.clamp(-1.0, 1.0) * 32767.0).round() as i16;
        w.write_all(&v.to_le_bytes())?;
    }
    w.flush()
}

#[cfg(test)]
mod tests {
    use super::*;

    /// A volume that has filled up: every write fails.
    struct Full;

    impl Write for Full {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::Error::new(io::ErrorKind::StorageFull, "no space left"))
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn a_failed_last_write_is_an_error_not_a_success() {
        // Codex on cfd11b0c: a WAV small enough to sit in the buffer reached
        // the file only when the writer was dropped, which ignores errors.
        let pcm = [0.25f32; 64];
        for (name, r) in [
            ("f32", write_f32(&mut io::BufWriter::new(Full), 48000, &pcm)),
            ("i16", write_i16(&mut io::BufWriter::new(Full), 48000, &pcm)),
        ] {
            assert_eq!(r.map_err(|e| e.kind()), Err(io::ErrorKind::StorageFull), "{name}");
        }
        // Control: to a sink with room, the whole file arrives.
        let mut out = io::BufWriter::new(Vec::new());
        write_f32(&mut out, 48000, &pcm).unwrap();
        assert_eq!(out.get_ref().len(), 44 + 64 * 4);
    }
}
