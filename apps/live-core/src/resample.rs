//! Streaming mono resampler to the analysis rate, on rubato's FFT resampler
//! (the converter apps/audio-engine uses for its stereo decode).
//!
//! Output is final as soon as it is appended, and its delay is dropped, so
//! output sample `i` is input time `i / to`: frame times stay true to the
//! input clock.

use rubato::{FftFixedIn, Resampler};

const CHUNK: usize = 1024;

pub struct MonoResampler {
    r: FftFixedIn<f32>,
    from: u32,
    to: u32,
    inp: Vec<Vec<f32>>,
    fill: usize,
    out: Vec<Vec<f32>>,
    skip: usize,
    frames_in: u64,
    frames_out: u64,
}

impl MonoResampler {
    pub fn new(from: u32, to: u32) -> Result<MonoResampler, String> {
        if from == 0 || to == 0 {
            return Err(format!("sample rates must be positive, got {from} -> {to}"));
        }
        let r = FftFixedIn::<f32>::new(from as usize, to as usize, CHUNK, 2, 1).map_err(|e| e.to_string())?;
        let skip = r.output_delay();
        let out_max = r.output_frames_max();
        Ok(MonoResampler {
            r,
            from,
            to,
            inp: vec![vec![0.0; CHUNK]],
            fill: 0,
            out: vec![vec![0.0; out_max]],
            skip,
            frames_in: 0,
            frames_out: 0,
        })
    }

    /// Feed `block`, appending what is ready to `out`.
    pub fn push(&mut self, block: &[f32], out: &mut Vec<f32>) -> Result<(), String> {
        for &s in block {
            self.inp[0][self.fill] = s;
            self.fill += 1;
            if self.fill == CHUNK {
                self.chunk(out, u64::MAX)?;
            }
        }
        self.frames_in += block.len() as u64;
        Ok(())
    }

    /// Flush: zero-pad and run on until exactly `round(in * to / from)`
    /// samples have come out in total.
    pub fn finish(&mut self, out: &mut Vec<f32>) -> Result<(), String> {
        let want = ((self.frames_in as u128 * self.to as u128 + self.from as u128 / 2) / self.from as u128) as u64;
        while self.frames_out < want {
            self.inp[0][self.fill..].fill(0.0);
            self.chunk(out, want)?;
        }
        Ok(())
    }

    fn chunk(&mut self, out: &mut Vec<f32>, limit: u64) -> Result<(), String> {
        let (_, got) = self
            .r
            .process_into_buffer(&self.inp, &mut self.out, None)
            .map_err(|e| e.to_string())?;
        self.fill = 0;
        let from = self.skip.min(got);
        self.skip -= from;
        for &s in &self.out[0][from..got] {
            if self.frames_out >= limit {
                break;
            }
            out.push(s);
            self.frames_out += 1;
        }
        Ok(())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_48k_sine_keeps_its_frequency_length_and_timing() {
        let hz = 440.0;
        let input: Vec<f32> = (0..48000 * 2)
            .map(|n| (2.0 * std::f64::consts::PI * hz * n as f64 / 48000.0).sin() as f32)
            .collect();
        let mut r = MonoResampler::new(48000, 22050).unwrap();
        let mut out = Vec::new();
        for block in input.chunks(777) {
            r.push(block, &mut out).unwrap();
        }
        r.finish(&mut out).unwrap();
        assert_eq!(out.len(), 44100);
        // Sample for sample against the ideal sine, away from the edges.
        let err = (2000..42000)
            .map(|n| (out[n] as f64 - (2.0 * std::f64::consts::PI * hz * n as f64 / 22050.0).sin()).abs())
            .fold(0.0, f64::max);
        assert!(err < 0.01, "max error {err}");
    }
}
