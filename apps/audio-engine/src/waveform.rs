//! Tri-band waveform peaks: audio file -> `(columns, 3)` u8 [low, mid, high].
//!
//! The same contract as `apps/analysis_waveform/decode.py` (NATIVE-06), which
//! builds it from an ffmpeg filter graph; this builds it from the engine's own
//! symphonia decode, so the app can draw its own waveform with no ffmpeg on
//! the machine. The numbers are meant to be the same ones:
//!
//! * the track is summed to mono before it is split, with ffmpeg's
//!   `channel_layouts=mono` gains: a stereo file is (L + R) / sqrt 2 (its
//!   default rematrix, measured: 1.419x a plain (L + R) / 2 on real tracks),
//!   a mono file passes at unity. A file with more than two channels uses its
//!   front pair under the stereo rule, where ffmpeg would mix them all;
//! * each band edge is `sections` cascaded 2-pole Butterworth biquads (the
//!   RBJ cookbook forms with Q = 1/sqrt 2, which is what ffmpeg's
//!   `lowpass`/`highpass` with `poles=2` compute), so 2 sections is 24 dB/oct;
//! * a column is the peak |sample| over `rate / columns_per_s` samples, read
//!   as int16 and shifted right by 7, so a full-scale sine reads 255.
//!
//! One difference is deliberate: ffmpeg resamples to 44.1 kHz first, this
//! filters at the file's own rate and spaces columns by exact sample counts
//! (`floor(k * rate / columns_per_s)`), so a 48 kHz file still gets 150
//! columns a second and nothing is resampled only to be reduced to peaks.
//!
//! Nothing is held past one decoded packet plus the u8 columns (450 bytes a
//! second of audio), the same streaming rule the Python decoder keeps.

use std::fs::File;
use std::path::Path;

use crate::decode;
use crate::protocol::ProtoError;

/// What a peak column means. Every field travels into the Python cache key.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Profile {
    pub crossover_low_hz: f64,
    pub crossover_high_hz: f64,
    /// Cascaded 2-pole sections per band edge.
    pub filter_sections: usize,
    pub columns_per_s: u32,
}

impl Default for Profile {
    /// The shipped `DecodeProfile` in `apps/analysis_waveform/decode.py`.
    fn default() -> Self {
        Profile { crossover_low_hz: 200.0, crossover_high_hz: 4000.0, filter_sections: 2, columns_per_s: 150 }
    }
}

/// One RBJ biquad in transposed direct form II, f64 state.
#[derive(Clone, Copy, Debug)]
struct Biquad {
    b0: f64,
    b1: f64,
    b2: f64,
    a1: f64,
    a2: f64,
    z1: f64,
    z2: f64,
}

impl Biquad {
    fn new(rate: u32, hz: f64, high: bool) -> Biquad {
        let w0 = 2.0 * std::f64::consts::PI * hz / rate as f64;
        let (sin, cos) = w0.sin_cos();
        let alpha = sin / (2.0 * std::f64::consts::FRAC_1_SQRT_2);
        let a0 = 1.0 + alpha;
        let (b0, b1) = if high { ((1.0 + cos) / 2.0, -(1.0 + cos)) } else { ((1.0 - cos) / 2.0, 1.0 - cos) };
        Biquad { b0: b0 / a0, b1: b1 / a0, b2: b0 / a0, a1: -2.0 * cos / a0, a2: (1.0 - alpha) / a0, z1: 0.0, z2: 0.0 }
    }

    #[inline]
    fn run(&mut self, x: f64) -> f64 {
        let y = self.b0 * x + self.z1;
        self.z1 = self.b1 * x - self.a1 * y + self.z2;
        self.z2 = self.b2 * x - self.a2 * y;
        y
    }
}

fn chain(rate: u32, hz: f64, high: bool, sections: usize) -> Vec<Biquad> {
    vec![Biquad::new(rate, hz, high); sections]
}

#[inline]
fn run_chain(chain: &mut [Biquad], mut x: f64) -> f64 {
    for f in chain {
        x = f.run(x);
    }
    x
}

/// |sample| as ffmpeg's float -> s16 conversion then `>> 7` reads it.
#[inline]
fn peak_byte(x: f64) -> u8 {
    let s = (x * 32768.0).round().clamp(-32768.0, 32767.0).abs().min(32767.0) as u32;
    (s >> 7) as u8
}

/// Streaming reducer: feed mono samples, collect `[low, mid, high]` columns.
pub struct Reducer {
    rate: u32,
    cps: u64,
    low: Vec<Biquad>,
    mid: Vec<Biquad>,
    high: Vec<Biquad>,
    /// Samples consumed so far, and where the current column ends.
    pos: u64,
    column: u64,
    end: u64,
    peak: [u8; 3],
    in_column: bool,
    pub columns: Vec<[u8; 3]>,
}

impl Reducer {
    pub fn new(rate: u32, p: &Profile) -> Result<Reducer, String> {
        if rate == 0 || p.columns_per_s == 0 || p.filter_sections == 0 {
            return Err("rate, columns_per_s and filter_sections must be positive".into());
        }
        let nyquist = rate as f64 / 2.0;
        if !(p.crossover_low_hz > 0.0 && p.crossover_low_hz < p.crossover_high_hz && p.crossover_high_hz < nyquist) {
            return Err(format!(
                "crossovers {} Hz / {} Hz do not fit under the {nyquist} Hz Nyquist of a {rate} Hz file",
                p.crossover_low_hz, p.crossover_high_hz
            ));
        }
        let n = p.filter_sections;
        let mut mid = chain(rate, p.crossover_low_hz, true, n);
        mid.extend(chain(rate, p.crossover_high_hz, false, n));
        let mut r = Reducer {
            rate,
            cps: u64::from(p.columns_per_s),
            low: chain(rate, p.crossover_low_hz, false, n),
            mid,
            high: chain(rate, p.crossover_high_hz, true, n),
            pos: 0,
            column: 0,
            end: 0,
            peak: [0; 3],
            in_column: false,
            columns: Vec::new(),
        };
        r.end = r.column_end(0);
        Ok(r)
    }

    fn column_end(&self, k: u64) -> u64 {
        (k + 1) * u64::from(self.rate) / self.cps
    }

    #[inline]
    pub fn push(&mut self, x: f64) {
        let bands = [run_chain(&mut self.low, x), run_chain(&mut self.mid, x), run_chain(&mut self.high, x)];
        for (p, b) in self.peak.iter_mut().zip(bands) {
            *p = (*p).max(peak_byte(b));
        }
        self.in_column = true;
        self.pos += 1;
        if self.pos == self.end {
            self.columns.push(self.peak);
            self.peak = [0; 3];
            self.in_column = false;
            self.column += 1;
            self.end = self.column_end(self.column);
        }
    }

    /// The columns, a short tail included as one last real column.
    pub fn finish(mut self) -> Vec<[u8; 3]> {
        if self.in_column {
            self.columns.push(self.peak);
        }
        self.columns
    }
}

/// Gain on L + R for the mono sum. `decode_stream` hands mono files over as
/// two identical sides, so unity for mono is a half; 0 channels is silence.
fn downmix_gain(channels: usize) -> f64 {
    if channels <= 1 {
        0.5
    } else {
        std::f64::consts::FRAC_1_SQRT_2
    }
}

/// A track's peak columns and what they were measured from.
pub struct Peaks {
    pub columns: Vec<[u8; 3]>,
    /// The file's own rate, which the bands were filtered at.
    pub sample_rate: u32,
    /// The file's channel count (its widest decoded packet).
    pub channels: usize,
}

/// Decode `path` and reduce it to tri-band peak columns.
pub fn peaks_file(path: &Path, p: &Profile) -> Result<Peaks, ProtoError> {
    let file: File = decode::open(path)?;
    let mut reducer: Option<Reducer> = None;
    let mut widest = 0usize;
    let (sample_rate, _) = decode::decode_stream(file, path, |rate, channels, block| {
        widest = widest.max(channels);
        if reducer.is_none() {
            reducer = Some(Reducer::new(rate, p).map_err(|m| {
                ProtoError::new(crate::engine::ErrorCode::Decode, format!("{m} ({})", path.display()))
            })?);
        }
        let r = reducer.as_mut().expect("set above");
        let gain = downmix_gain(channels);
        for frame in block.chunks_exact(2) {
            r.push((f64::from(frame[0]) + f64::from(frame[1])) * gain);
        }
        Ok(())
    })?;
    let columns = reducer.map(Reducer::finish).unwrap_or_default();
    if columns.is_empty() {
        return Err(ProtoError::new(
            crate::engine::ErrorCode::Decode,
            format!("no audio decoded from {}", path.display()),
        ));
    }
    Ok(Peaks { columns, sample_rate, channels: widest })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(rate: u32, hz: f64, secs: f64, amp: f64) -> Vec<f64> {
        let n = (rate as f64 * secs) as usize;
        (0..n).map(|i| amp * (2.0 * std::f64::consts::PI * hz * i as f64 / rate as f64).sin()).collect()
    }

    fn reduce(rate: u32, xs: &[f64]) -> Vec<[u8; 3]> {
        let mut r = Reducer::new(rate, &Profile::default()).unwrap();
        for &x in xs {
            r.push(x);
        }
        r.finish()
    }

    /// Median of one band over the settled middle of a reduced tone.
    fn mid_band(cols: &[[u8; 3]], band: usize) -> u8 {
        let mut v: Vec<u8> = cols[cols.len() / 4..cols.len() * 3 / 4].iter().map(|c| c[band]).collect();
        v.sort_unstable();
        v[v.len() / 2]
    }

    #[test]
    fn each_tone_lands_in_its_own_band_and_not_the_others() {
        // A distinct tone per band; each must light its band and leave the
        // other two near zero (24 dB/oct keeps the leak under ~0.01).
        for (hz, band) in [(60.0, 0usize), (1000.0, 1), (12000.0, 2)] {
            let cols = reduce(44100, &tone(44100, hz, 2.0, 0.9));
            for b in 0..3 {
                let v = mid_band(&cols, b);
                if b == band {
                    assert!(v > 200, "{hz} Hz should fill band {b}, got {v}");
                } else {
                    assert!(v < 10, "{hz} Hz leaked {v} into band {b}");
                }
            }
        }
    }

    #[test]
    fn columns_are_150_a_second_at_any_rate() {
        for rate in [44100u32, 48000, 96000, 22050] {
            let cols = reduce(rate, &vec![0.0; rate as usize * 3]);
            assert_eq!(cols.len(), 450, "{rate} Hz");
        }
        // A short tail is one more real column, never padded or dropped.
        assert_eq!(reduce(44100, &vec![0.0; 294 * 2 + 5]).len(), 3);
        // Control: an exact multiple adds no empty column.
        assert_eq!(reduce(44100, &vec![0.0; 294 * 2]).len(), 2);
    }

    #[test]
    fn full_scale_reads_255_and_silence_reads_0() {
        assert_eq!(peak_byte(1.0), 255);
        assert_eq!(peak_byte(-1.0), 255);
        assert_eq!(peak_byte(0.0), 0);
        assert_eq!(peak_byte(0.5), 128);
        // Over full scale clips rather than wrapping to a small value.
        assert_eq!(peak_byte(-4.0), 255);
    }

    #[test]
    fn stereo_sums_like_ffmpeg_and_mono_passes_at_unity() {
        // A mono file arrives duplicated: (x + x) * gain must be x.
        assert_eq!((0.25 + 0.25) * downmix_gain(1), 0.25);
        // Stereo: ffmpeg's default rematrix, 1/sqrt 2 per side.
        assert!((downmix_gain(2) - 0.5f64.sqrt()).abs() < 1e-12);
    }

    #[test]
    fn crossovers_above_nyquist_are_refused() {
        // A 8 kHz file has a 4 kHz Nyquist: the high band cannot exist.
        assert!(Reducer::new(8000, &Profile::default()).is_err());
        assert!(Reducer::new(44100, &Profile::default()).is_ok());
    }
}
