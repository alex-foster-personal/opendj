//! BPM from an onset envelope.
//!
//! The envelope is spectral flux: the summed positive change of the log
//! magnitude of a 1024-point Hann spectrum, one value per analysis hop
//! (512 samples at 22050 Hz, 43.07 per second). The tempo is the candidate
//! between [`MIN_BPM`] and [`MAX_BPM`] (0.05 BPM steps) whose beat period
//! best lines up with the envelope's autocorrelation at every multiple of
//! the period out to 8 s (at least four), weighted by librosa's tempo prior
//! (log-normal around 120 BPM, one octave wide), which is what picks 124
//! over its half, 62. Reading many multiples of the period is what makes
//! the answer finer than the 43 Hz frame grid.

use realfft::num_complex::Complex;
use realfft::{RealFftPlanner, RealToComplex};
use std::sync::Arc;

pub const FLUX_WINDOW: usize = 1024;
pub const MIN_BPM: f64 = 60.0;
pub const MAX_BPM: f64 = 200.0;
const PRIOR_BPM: f64 = 120.0;
/// The fewest beat multiples a candidate is scored on.
const MIN_MULTIPLES: usize = 4;
/// The autocorrelation is read out to this many seconds (or half the
/// envelope): the longer the lag, the finer the period it resolves.
const MAX_LAG_S: f64 = 8.0;
/// The least envelope, in seconds, a tempo is estimated from.
pub const MIN_SECONDS: f64 = 4.0;
/// Best autocorrelation score over the zero-lag energy below which there is
/// no beat to report.
const MIN_STRENGTH: f64 = 0.05;

pub struct Flux {
    fft: Arc<dyn RealToComplex<f32>>,
    window: Vec<f32>,
    input: Vec<f32>,
    spectrum: Vec<Complex<f32>>,
    scratch: Vec<Complex<f32>>,
    prev: Vec<f32>,
    primed: bool,
}

impl Flux {
    pub fn new() -> Flux {
        let mut planner = RealFftPlanner::<f32>::new();
        let fft = planner.plan_fft_forward(FLUX_WINDOW);
        let window = (0..FLUX_WINDOW)
            .map(|n| (0.5 - 0.5 * (2.0 * std::f64::consts::PI * n as f64 / FLUX_WINDOW as f64).cos()) as f32)
            .collect();
        Flux {
            input: fft.make_input_vec(),
            spectrum: fft.make_output_vec(),
            scratch: fft.make_scratch_vec(),
            prev: vec![0.0; FLUX_WINDOW / 2 + 1],
            primed: false,
            fft,
            window,
        }
    }

    /// The onset strength of the last [`FLUX_WINDOW`] samples.
    pub fn push(&mut self, samples: &[f32]) -> f32 {
        assert_eq!(samples.len(), FLUX_WINDOW);
        for ((i, s), w) in self.input.iter_mut().zip(samples).zip(&self.window) {
            *i = s * w;
        }
        self.fft
            .process_with_scratch(&mut self.input, &mut self.spectrum, &mut self.scratch)
            .expect("buffer sizes come from the plan");
        let mut flux = 0f32;
        for (p, c) in self.prev.iter_mut().zip(&self.spectrum) {
            let l = (1.0 + 100.0 * c.norm()).ln();
            if self.primed {
                flux += (l - *p).max(0.0);
            }
            *p = l;
        }
        self.primed = true;
        flux
    }
}

impl Default for Flux {
    fn default() -> Self {
        Self::new()
    }
}

/// The tempo of `envelope` (one value per frame, `frame_rate` per second),
/// or `None` when it is too short or holds no periodic onsets.
pub fn estimate_bpm(envelope: &[f32], frame_rate: f64) -> Option<f64> {
    if (envelope.len() as f64) < MIN_SECONDS * frame_rate {
        return None;
    }
    // Light smoothing (a 5-tap binomial) rounds the autocorrelation peaks so
    // a fractional lag can be read between frames.
    let n = envelope.len();
    let mut e = vec![0f64; n];
    const TAPS: [f64; 5] = [1.0 / 16.0, 4.0 / 16.0, 6.0 / 16.0, 4.0 / 16.0, 1.0 / 16.0];
    for (i, out) in e.iter_mut().enumerate() {
        let mut acc = 0.0;
        for (j, t) in TAPS.iter().enumerate() {
            let k = (i + j).saturating_sub(2).min(n - 1);
            acc += t * f64::from(envelope[k]);
        }
        *out = acc;
    }
    let mean = e.iter().sum::<f64>() / n as f64;
    e.iter_mut().for_each(|x| *x -= mean);
    let max_period = 60.0 * frame_rate / MIN_BPM;
    let max_lag = ((MIN_MULTIPLES as f64 * max_period)
        .max((MAX_LAG_S * frame_rate).min(n as f64 / 2.0))
        .ceil() as usize
        + 2)
    .min(n - 1);
    // Unbiased autocorrelation, so long lags are not penalized for overlap.
    let acf: Vec<f64> = (0..=max_lag)
        .map(|lag| e[..n - lag].iter().zip(&e[lag..]).map(|(a, b)| a * b).sum::<f64>() / (n - lag) as f64)
        .collect();
    if acf[0] <= 1e-12 {
        return None;
    }
    let at = |lag: f64| -> Option<f64> {
        let i = lag.floor() as usize;
        if i + 1 >= acf.len() {
            return None;
        }
        let f = lag - i as f64;
        Some(acf[i] * (1.0 - f) + acf[i + 1] * f)
    };
    let mut best: Option<(f64, f64, f64)> = None; // (weighted, raw, bpm)
    let steps = ((MAX_BPM - MIN_BPM) / 0.05).round() as usize;
    for s in 0..=steps {
        let bpm = MIN_BPM + s as f64 * 0.05;
        let period = 60.0 * frame_rate / bpm;
        let mut raw = 0.0;
        let mut used = 0;
        let multiples = ((max_lag as f64 - 1.0) / period).floor().max(MIN_MULTIPLES as f64) as usize;
        for m in 1..=multiples {
            if let Some(v) = at(m as f64 * period) {
                raw += v;
                used += 1;
            }
        }
        if used == 0 {
            continue;
        }
        raw /= used as f64;
        let prior = (-0.5 * (bpm / PRIOR_BPM).log2().powi(2)).exp();
        let weighted = raw.max(0.0) * prior;
        if best.is_none_or(|(w, _, _)| weighted > w) {
            best = Some((weighted, raw, bpm));
        }
    }
    let (_, raw, bpm) = best?;
    if raw / acf[0] < MIN_STRENGTH {
        return None;
    }
    Some(bpm)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn pulses(bpm: f64, seconds: f64, rate: f64) -> Vec<f32> {
        let n = (seconds * rate) as usize;
        let mut e = vec![0f32; n];
        let mut t = 0.0;
        while t < seconds {
            let i = (t * rate).round() as usize;
            if i < n {
                e[i] = 1.0;
            }
            t += 60.0 / bpm;
        }
        e
    }

    #[test]
    fn pulse_trains_read_their_tempo() {
        let rate = 22050.0 / 512.0;
        for bpm in [124.0, 128.0, 87.5, 140.0, 150.0, 100.0] {
            let got = estimate_bpm(&pulses(bpm, 30.0, rate), rate).unwrap();
            assert!((got - bpm).abs() < 0.3, "want {bpm}, got {got}");
        }
    }

    #[test]
    fn short_or_flat_envelopes_have_no_tempo() {
        let rate = 43.0;
        assert!(estimate_bpm(&pulses(120.0, 2.0, rate), rate).is_none());
        assert!(estimate_bpm(&vec![0.0; 1000], rate).is_none());
        assert!(estimate_bpm(&vec![1.0; 1000], rate).is_none());
    }
}
