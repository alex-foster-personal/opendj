//! Constant-Q chroma, shaped like `librosa.feature.chroma_cqt`.
//!
//! librosa's own layout: three constant-Q bins per semitone from C1 up seven
//! octaves (252 bins, `fmin=C1, n_octaves=7, bins_per_octave=36`), each a
//! Hann-windowed complex sinusoid of length `Q * sr / f` with
//! `Q = 1 / (2^(1/36) - 1)`, centered in a 32768-sample frame (1.49 s at
//! 22050 Hz). C1's filter is 34660 samples, so the three lowest bins are cut
//! to the frame (slightly wider than librosa's there); every other bin is
//! librosa's length. The bins are computed the Brown-Puckette way: each
//! filter's spectrum is precomputed once and kept sparse, so a frame costs
//! one real FFT plus a few thousand complex multiplies.
//!
//! Matching librosa's shape, measured against librosa itself (see
//! `tests/live_core/test_live_core_parity.py`):
//!
//! - filters are L1-normalized and a bin's magnitude is then multiplied by
//!   `sqrt(filter length)`, which reproduces librosa's `scale=True` response
//!   (a sine of fixed amplitude reads `1/sqrt(f)`: half as loud two octaves
//!   up);
//! - a pitch class is the plain sum of its bins, the semitone's own bin and
//!   the bins a third of a semitone either side, over all seven octaves
//!   (librosa's `cq_to_chroma`), with no log compression, because
//!   `chroma_cqt` applies none;
//! - each frame is divided by its largest bin (`norm=inf`).
//!
//! The center frequencies follow the tuning estimate: every bin moves by the
//! same number of cents, as librosa's `tuning` argument moves `fmin`.

use realfft::num_complex::Complex;
use realfft::{RealFftPlanner, RealToComplex};
use std::sync::Arc;

use crate::SR;

/// Analysis frame length in samples at [`SR`].
pub const FRAME: usize = 32768;
/// A frame quieter than this RMS (-70 dBFS) is treated as silence: idle
/// interface noise or dither would otherwise normalize up to a full-scale,
/// arbitrary chroma column and hold a key forever.
pub const NOISE_FLOOR_RMS: f32 = 3.162e-4;
/// C1 (MIDI 24) at A440.
const FMIN: f64 = 32.703_195_662_574_83;
const BINS_PER_OCTAVE: usize = 36;
const N_BINS: usize = 7 * BINS_PER_OCTAVE;
/// A sparse kernel entry below this fraction of its own peak is dropped
/// (librosa's `sparsity=0.01` in spirit).
const SPARSITY: f64 = 0.01;

struct Kernel {
    /// First FFT bin the entries cover.
    start: usize,
    /// `conj(K[k]) / FRAME`, from `start`.
    taps: Vec<Complex<f32>>,
    /// librosa's `scale=True`: `sqrt(filter length)` after L1 normalization.
    scale: f32,
}

pub struct ConstantQ {
    fft: Arc<dyn RealToComplex<f32>>,
    input: Vec<f32>,
    spectrum: Vec<Complex<f32>>,
    scratch: Vec<Complex<f32>>,
    kernels: Vec<Kernel>,
    tuning_cents: f64,
    builder: KernelBuilder,
    /// A retune in progress: its tuning and the kernels built so far. The
    /// live kernels stay in use until every bin is built, then swap at once.
    pending: Option<(f64, Vec<Kernel>)>,
}

impl ConstantQ {
    pub fn new(tuning_cents: f64) -> ConstantQ {
        let mut planner = RealFftPlanner::<f32>::new();
        let fft = planner.plan_fft_forward(FRAME);
        let input = fft.make_input_vec();
        let spectrum = fft.make_output_vec();
        let scratch = fft.make_scratch_vec();
        let mut builder = KernelBuilder::new();
        let kernels = (0..N_BINS).map(|b| builder.kernel(b, tuning_cents)).collect();
        ConstantQ {
            fft,
            input,
            spectrum,
            scratch,
            kernels,
            tuning_cents,
            builder,
            pending: None,
        }
    }

    pub fn tuning_cents(&self) -> f64 {
        self.tuning_cents
    }

    /// Rebuild every filter for a new tuning now (252 FFTs of [`FRAME`],
    /// about 0.24 s in release; the live path uses [`Self::begin_retune`]).
    pub fn retune(&mut self, tuning_cents: f64) {
        self.begin_retune(tuning_cents);
        while self.pending.is_some() {
            self.step_retune(N_BINS);
        }
    }

    /// Start rebuilding the filters for `tuning_cents` a few bins at a time
    /// (see [`Self::step_retune`]); replaces any retune in progress.
    pub fn begin_retune(&mut self, tuning_cents: f64) {
        self.pending = Some((tuning_cents, Vec::with_capacity(N_BINS)));
    }

    pub fn retuning(&self) -> bool {
        self.pending.is_some()
    }

    /// The tuning the retune in progress rebuilds to, if one is running.
    #[cfg(test)]
    pub fn pending_target(&self) -> Option<f64> {
        self.pending.as_ref().map(|(tuning, _)| *tuning)
    }

    /// Build up to `bins` more filters of the retune in progress, and swap
    /// them in once all [`N_BINS`] are built. Returns true on the swap.
    pub fn step_retune(&mut self, bins: usize) -> bool {
        let Some((tuning, built)) = self.pending.as_mut() else { return false };
        let tuning = *tuning;
        for _ in 0..bins {
            if built.len() == N_BINS {
                break;
            }
            let bin = built.len();
            built.push(self.builder.kernel(bin, tuning));
        }
        if built.len() < N_BINS {
            return false;
        }
        let (tuning, built) = self.pending.take().expect("checked above");
        self.kernels = built;
        self.tuning_cents = tuning;
        true
    }

    /// One chroma column from `frame` (exactly [`FRAME`] samples, the bins
    /// centered on its middle sample), divided by its largest bin. A frame
    /// below [`NOISE_FLOOR_RMS`] gives all zeros, not a normalized noise floor.
    pub fn chroma(&mut self, frame: &[f32]) -> [f32; 12] {
        assert_eq!(frame.len(), FRAME, "a constant-Q frame is {FRAME} samples");
        let energy: f64 = frame.iter().map(|&x| f64::from(x) * f64::from(x)).sum();
        if ((energy / FRAME as f64).sqrt() as f32) < NOISE_FLOOR_RMS {
            return [0.0; 12];
        }
        self.input.copy_from_slice(frame);
        self.fft
            .process_with_scratch(&mut self.input, &mut self.spectrum, &mut self.scratch)
            .expect("buffer sizes come from the plan");
        let mut chroma = [0f32; 12];
        for (bin, k) in self.kernels.iter().enumerate() {
            let mut acc = Complex::new(0f32, 0f32);
            for (x, t) in self.spectrum[k.start..k.start + k.taps.len()].iter().zip(&k.taps) {
                acc += x * t;
            }
            // Bin j is j/3 semitones above C1; it counts towards the nearest
            // semitone, so C takes C-1/3, C and C+1/3 (`cq_to_chroma`).
            chroma[((bin + 1) / 3) % 12] += acc.norm() * k.scale;
        }
        let max = chroma.iter().cloned().fold(0f32, f32::max);
        if max > 1e-9 {
            for c in chroma.iter_mut() {
                *c /= max;
            }
        } else {
            chroma = [0.0; 12];
        }
        chroma
    }
}

/// One FFT plan and buffer, reused for every filter it builds.
struct KernelBuilder {
    fft: Arc<dyn rustfft::Fft<f64>>,
    buf: Vec<Complex<f64>>,
}

impl KernelBuilder {
    fn new() -> KernelBuilder {
        let fft = rustfft::FftPlanner::<f64>::new().plan_fft_forward(FRAME);
        KernelBuilder { fft, buf: vec![Complex::new(0f64, 0f64); FRAME] }
    }

    /// Filter `bin` at `tuning_cents`: one FFT of [`FRAME`] (about 1 ms).
    fn kernel(&mut self, bin: usize, tuning_cents: f64) -> Kernel {
        let q = 1.0 / (2f64.powf(1.0 / BINS_PER_OCTAVE as f64) - 1.0);
        let center = FRAME / 2;
        let (fft, buf) = (&self.fft, &mut self.buf);
        let f = FMIN * 2f64.powf(bin as f64 / BINS_PER_OCTAVE as f64 + tuning_cents / 1200.0);
        let nominal = q * f64::from(SR) / f;
        let len = (nominal.ceil() as usize).min(FRAME);
        // Periodic Hann (scipy's fftbins=True, as librosa builds it).
        let window: Vec<f64> = (0..len)
            .map(|n| 0.5 - 0.5 * (2.0 * std::f64::consts::PI * n as f64 / len as f64).cos())
            .collect();
        let l1: f64 = window.iter().sum();
        buf.iter_mut().for_each(|c| *c = Complex::new(0.0, 0.0));
        let start = center - len / 2;
        let w = 2.0 * std::f64::consts::PI * f / f64::from(SR);
        for (n, wn) in window.iter().enumerate() {
            let t = (start + n) as f64 - center as f64;
            buf[start + n] = Complex::from_polar(wn / l1, w * t);
        }
        fft.process(buf);
        // A real frame's spectrum is kept for 0..=FRAME/2 only; the analytic
        // filter's energy sits at +f, so its negative-frequency half is
        // negligible and dropped.
        let half = &buf[..=FRAME / 2];
        let peak = half.iter().map(|c| c.norm()).fold(0.0, f64::max);
        let keep = |c: &Complex<f64>| c.norm() >= SPARSITY * peak;
        let first = half.iter().position(keep).unwrap_or(0);
        let last = half.iter().rposition(keep).unwrap_or(0);
        let taps = half[first..=last]
            .iter()
            .map(|c| {
                let t = c.conj() / FRAME as f64;
                Complex::new(t.re as f32, t.im as f32)
            })
            .collect();
        // librosa's scale uses the filter's own length, which a cut filter keeps.
        Kernel {
            start: first,
            taps,
            scale: nominal.sqrt() as f32,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn sine(hz: f64, amp: f32) -> Vec<f32> {
        (0..FRAME)
            .map(|n| amp * (2.0 * std::f64::consts::PI * hz * n as f64 / f64::from(SR)).sin() as f32)
            .collect()
    }

    #[test]
    fn a_sine_lands_in_its_pitch_class() {
        let mut cq = ConstantQ::new(0.0);
        for (midi, pc) in [(57.0, 9usize), (60.0, 0), (66.0, 6), (40.0, 4), (88.0, 4)] {
            let hz = 440.0 * 2f64.powf((midi - 69.0) / 12.0);
            let c = cq.chroma(&sine(hz, 0.5));
            let best = (0..12).max_by(|&a, &b| c[a].partial_cmp(&c[b]).unwrap()).unwrap();
            assert_eq!(best, pc, "midi {midi}: {c:?}");
            assert!((c[pc] - 1.0).abs() < 1e-6);
            // Neighbours a semitone away hold little of it.
            assert!(c[(pc + 1) % 12] < 0.2 && c[(pc + 11) % 12] < 0.2, "midi {midi}: {c:?}");
        }
    }

    #[test]
    fn two_octaves_up_reads_half_as_loud() {
        // librosa's scale=True response: magnitude goes as 1/sqrt(f).
        let mut cq = ConstantQ::new(0.0);
        let mut mix = sine(220.0, 0.5);
        for (m, s) in mix.iter_mut().zip(sine(880.0 * 2f64.powf(4.0 / 12.0), 0.5)) {
            *m += s; // A3 plus C#6 (two octaves and a third up)
        }
        let c = cq.chroma(&mix);
        let ratio = c[1] / c[9];
        let want = (220.0f64 / (880.0 * 2f64.powf(4.0 / 12.0))).sqrt() as f32;
        assert!((ratio - want).abs() < 0.05, "ratio {ratio}, want {want}");
    }

    #[test]
    fn silence_is_all_zero_and_retuning_follows_a_detuned_tone() {
        let mut cq = ConstantQ::new(0.0);
        assert_eq!(cq.chroma(&vec![0.0; FRAME]), [0.0; 12]);
        // A tone 45 cents sharp of A4: in tune it splits towards A#, retuned it is clean.
        let hz = 440.0 * 2f64.powf(45.0 / 1200.0);
        let before = cq.chroma(&sine(hz, 0.5));
        cq.retune(45.0);
        let after = cq.chroma(&sine(hz, 0.5));
        assert!(after[10] < before[10] * 0.5, "before {before:?} after {after:?}");
    }

    #[test]
    fn a_stepped_retune_keeps_the_old_filters_until_every_bin_is_built() {
        let hz = 440.0 * 2f64.powf(45.0 / 1200.0); // A, 45 cents sharp
        let tone = sine(hz, 0.5);
        let mut whole = ConstantQ::new(0.0);
        whole.retune(45.0);
        let want = whole.chroma(&tone);
        let mut cq = ConstantQ::new(0.0);
        let before = cq.chroma(&tone);
        cq.begin_retune(45.0);
        let mut steps = 0;
        while !cq.step_retune(12) {
            steps += 1;
            // Mid-rebuild the old filters still answer, unchanged.
            assert_eq!(cq.tuning_cents(), 0.0);
            assert_eq!(cq.pending.as_ref().unwrap().1.len(), 12 * steps);
            assert_eq!(cq.chroma(&tone), before);
        }
        assert_eq!(steps + 1, N_BINS / 12);
        assert!(!cq.retuning());
        assert_eq!(cq.tuning_cents(), 45.0);
        // The swapped-in filters are exactly the synchronous rebuild's.
        assert_eq!(cq.chroma(&tone), want);
    }
}
