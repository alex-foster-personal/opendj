//! Tuning offset from spectral peaks, in cents against A440.
//!
//! The same idea as `librosa.estimate_tuning`: find spectral peaks, measure
//! each one's distance to the nearest equal-tempered semitone, and take the
//! mode of a histogram of those distances. Here the histogram is
//! magnitude-weighted, 1-cent wide, circular (+50 and -50 cents are the same
//! offset), and decays with a 30 s time constant so a live estimate follows
//! a pitched-up deck. The peak frequency comes from a parabola through the
//! log magnitudes of a 4096-point Hann spectrum, so a steady tone reads to
//! about a cent.

use realfft::num_complex::Complex;
use realfft::{RealFftPlanner, RealToComplex};
use std::sync::Arc;

use crate::cqt::NOISE_FLOOR_RMS;
use crate::SR;

/// Samples per tuning spectrum.
pub const WINDOW: usize = 4096;
const LOW_HZ: f64 = 80.0;
const HIGH_HZ: f64 = 4000.0;
const PEAKS_PER_FRAME: usize = 8;
const DECAY_S: f64 = 30.0;
/// Below this much accumulated peak magnitude there is no estimate.
const MIN_WEIGHT: f64 = 1.0;

pub struct Tuning {
    fft: Arc<dyn RealToComplex<f32>>,
    window: Vec<f32>,
    input: Vec<f32>,
    spectrum: Vec<Complex<f32>>,
    scratch: Vec<Complex<f32>>,
    mags: Vec<f32>,
    hist: [f64; 100],
    /// The same histogram without decay: the whole stream's offset.
    total: [f64; 100],
    decay: f64,
}

impl Tuning {
    /// `hop_s`: seconds between calls to [`Tuning::push`], for the decay.
    pub fn new(hop_s: f64) -> Tuning {
        let mut planner = RealFftPlanner::<f32>::new();
        let fft = planner.plan_fft_forward(WINDOW);
        let window = (0..WINDOW)
            .map(|n| (0.5 - 0.5 * (2.0 * std::f64::consts::PI * n as f64 / WINDOW as f64).cos()) as f32)
            .collect();
        Tuning {
            input: fft.make_input_vec(),
            spectrum: fft.make_output_vec(),
            scratch: fft.make_scratch_vec(),
            mags: vec![0.0; WINDOW / 2 + 1],
            fft,
            window,
            hist: [0.0; 100],
            total: [0.0; 100],
            decay: (-hop_s / DECAY_S).exp(),
        }
    }

    /// Add the peaks of the last [`WINDOW`] samples. A window below
    /// [`NOISE_FLOOR_RMS`] is silence, as for the chroma: it decays the
    /// recent histogram and adds no peaks to either.
    pub fn push(&mut self, samples: &[f32]) {
        assert_eq!(samples.len(), WINDOW);
        for h in self.hist.iter_mut() {
            *h *= self.decay;
        }
        let energy: f64 = samples.iter().map(|&x| f64::from(x) * f64::from(x)).sum();
        if ((energy / WINDOW as f64).sqrt() as f32) < NOISE_FLOOR_RMS {
            return;
        }
        for ((i, s), w) in self.input.iter_mut().zip(samples).zip(&self.window) {
            *i = s * w;
        }
        self.fft
            .process_with_scratch(&mut self.input, &mut self.spectrum, &mut self.scratch)
            .expect("buffer sizes come from the plan");
        for (m, c) in self.mags.iter_mut().zip(&self.spectrum) {
            *m = c.norm();
        }
        let bin_hz = f64::from(SR) / WINDOW as f64;
        let lo = (LOW_HZ / bin_hz).ceil() as usize;
        let hi = ((HIGH_HZ / bin_hz).floor() as usize).min(self.mags.len() - 2);
        let max = self.mags[lo..=hi].iter().cloned().fold(0f32, f32::max);
        if max <= 1e-6 {
            return;
        }
        let mut peaks: Vec<(f32, usize)> = (lo..=hi)
            .filter(|&k| {
                let m = self.mags[k];
                m > 0.1 * max && m > self.mags[k - 1] && m >= self.mags[k + 1]
            })
            .map(|k| (self.mags[k], k))
            .collect();
        peaks.sort_by(|a, b| b.0.partial_cmp(&a.0).unwrap_or(std::cmp::Ordering::Equal));
        for &(mag, k) in peaks.iter().take(PEAKS_PER_FRAME) {
            let (a, b, c) = (
                f64::from(self.mags[k - 1].max(1e-12)).ln(),
                f64::from(self.mags[k].max(1e-12)).ln(),
                f64::from(self.mags[k + 1].max(1e-12)).ln(),
            );
            let denom = a - 2.0 * b + c;
            let offset = if denom.abs() > 1e-12 { 0.5 * (a - c) / denom } else { 0.0 };
            let hz = (k as f64 + offset.clamp(-0.5, 0.5)) * bin_hz;
            let semis = 12.0 * (hz / 440.0).log2();
            let cents = (semis - semis.round()) * 100.0; // -50..50
                                                         // Linear split between the two nearest 1-cent bins, circularly.
            let pos = (cents + 50.0).rem_euclid(100.0);
            let i0 = pos.floor() as usize % 100;
            let frac = pos - pos.floor();
            let w = f64::from(mag);
            for h in [&mut self.hist, &mut self.total] {
                h[i0] += w * (1.0 - frac);
                h[(i0 + 1) % 100] += w * frac;
            }
        }
    }

    /// Forget the recent offset (sustained silence: the source stopped).
    /// The whole-stream histogram is kept for the summary.
    pub fn clear_recent(&mut self) {
        self.hist = [0.0; 100];
    }

    /// The recent offset in cents (-50..50), or `None` before enough
    /// pitched peaks.
    pub fn cents(&self) -> Option<f64> {
        histogram_cents(&self.hist)
    }

    /// The offset over everything pushed so far.
    pub fn cents_total(&self) -> Option<f64> {
        histogram_cents(&self.total)
    }
}

fn histogram_cents(hist: &[f64; 100]) -> Option<f64> {
    let total: f64 = hist.iter().sum();
    if total < MIN_WEIGHT {
        return None;
    }
    // Smooth over +-3 cents, take the mode, then refine with the
    // circular mean of +-15 cents around it.
    let smooth: Vec<f64> = (0..100)
        .map(|i| (-3i32..=3).map(|d| hist[(i + d).rem_euclid(100) as usize]).sum())
        .collect();
    let mode = (0..100).max_by(|&a, &b| smooth[a].partial_cmp(&smooth[b]).unwrap()).unwrap();
    let (mut sx, mut sy) = (0.0, 0.0);
    for d in -15i32..=15 {
        let i = (mode as i32 + d).rem_euclid(100) as usize;
        let angle = 2.0 * std::f64::consts::PI * (i as f64 - 50.0) / 100.0;
        sx += hist[i] * angle.cos();
        sy += hist[i] * angle.sin();
    }
    let cents = sy.atan2(sx) / (2.0 * std::f64::consts::PI) * 100.0;
    Some(cents)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tone(hz: f64, offset: usize) -> Vec<f32> {
        (offset..offset + WINDOW)
            .map(|n| {
                let t = n as f64 / f64::from(SR);
                (0.4 * (2.0 * std::f64::consts::PI * hz * t).sin() + 0.2 * (2.0 * std::f64::consts::PI * 2.0 * hz * t).sin()) as f32
            })
            .collect()
    }

    #[test]
    fn a_detuned_tone_reads_its_offset_and_an_in_tune_one_reads_zero() {
        for (cents, base) in [(20.0, 440.0), (-30.0, 261.63), (0.0, 196.0), (45.0, 329.63)] {
            let hz = base * 2f64.powf(cents / 1200.0);
            let mut t = Tuning::new(512.0 / f64::from(SR));
            assert!(t.cents().is_none());
            for i in 0..40 {
                t.push(&tone(hz, i * 512));
            }
            // 261.63, 196.0 and 329.63 are themselves within ~0.1 cent of the grid.
            let got = t.cents().unwrap();
            assert!((got - cents).abs() < 3.0, "want {cents}, got {got}");
        }
    }

    #[test]
    fn silence_gives_no_estimate() {
        let mut t = Tuning::new(0.02);
        for _ in 0..10 {
            t.push(&vec![0.0; WINDOW]);
        }
        assert!(t.cents().is_none());
    }
}
