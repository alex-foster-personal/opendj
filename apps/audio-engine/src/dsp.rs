//! Biquads and parameter smoothing, in the exact forms Web Audio uses.
//!
//! Coefficients follow the Web Audio spec's `BiquadFilterNode` section (itself
//! the RBJ Audio EQ Cookbook), including its two quirks that matter for a
//! null test: shelves use slope S = 1, and lowpass/highpass read Q in dB.
//! Everything runs in f64 with no fused or reordered math, so a render is
//! bit-reproducible on any machine that runs the same binary.

/// Normalized biquad coefficients (a0 divided out).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Coeffs {
    pub b0: f64,
    pub b1: f64,
    pub b2: f64,
    pub a1: f64,
    pub a2: f64,
}

impl Coeffs {
    pub const IDENTITY: Coeffs = Coeffs { b0: 1.0, b1: 0.0, b2: 0.0, a1: 0.0, a2: 0.0 };

    fn normalized(b0: f64, b1: f64, b2: f64, a0: f64, a1: f64, a2: f64) -> Coeffs {
        Coeffs { b0: b0 / a0, b1: b1 / a0, b2: b2 / a0, a1: a1 / a0, a2: a2 / a0 }
    }

    pub fn lowshelf(sr: f64, f0: f64, gain_db: f64) -> Coeffs {
        let a = 10f64.powf(gain_db / 40.0);
        let w0 = 2.0 * std::f64::consts::PI * f0 / sr;
        let (sin, cos) = w0.sin_cos();
        // S = 1: alpha = sin(w0)/2 * sqrt((A + 1/A)(1/S - 1) + 2) = sin(w0)/2 * sqrt(2)
        let alpha = sin / 2.0 * 2f64.sqrt();
        let k = 2.0 * a.sqrt() * alpha;
        Coeffs::normalized(
            a * ((a + 1.0) - (a - 1.0) * cos + k),
            2.0 * a * ((a - 1.0) - (a + 1.0) * cos),
            a * ((a + 1.0) - (a - 1.0) * cos - k),
            (a + 1.0) + (a - 1.0) * cos + k,
            -2.0 * ((a - 1.0) + (a + 1.0) * cos),
            (a + 1.0) + (a - 1.0) * cos - k,
        )
    }

    pub fn highshelf(sr: f64, f0: f64, gain_db: f64) -> Coeffs {
        let a = 10f64.powf(gain_db / 40.0);
        let w0 = 2.0 * std::f64::consts::PI * f0 / sr;
        let (sin, cos) = w0.sin_cos();
        let alpha = sin / 2.0 * 2f64.sqrt();
        let k = 2.0 * a.sqrt() * alpha;
        Coeffs::normalized(
            a * ((a + 1.0) + (a - 1.0) * cos + k),
            -2.0 * a * ((a - 1.0) + (a + 1.0) * cos),
            a * ((a + 1.0) + (a - 1.0) * cos - k),
            (a + 1.0) - (a - 1.0) * cos + k,
            2.0 * ((a - 1.0) - (a + 1.0) * cos),
            (a + 1.0) - (a - 1.0) * cos - k,
        )
    }

    pub fn peaking(sr: f64, f0: f64, q: f64, gain_db: f64) -> Coeffs {
        let a = 10f64.powf(gain_db / 40.0);
        let w0 = 2.0 * std::f64::consts::PI * f0 / sr;
        let (sin, cos) = w0.sin_cos();
        let alpha = sin / (2.0 * q);
        Coeffs::normalized(
            1.0 + alpha * a,
            -2.0 * cos,
            1.0 - alpha * a,
            1.0 + alpha / a,
            -2.0 * cos,
            1.0 - alpha / a,
        )
    }

    /// Web Audio reads lowpass/highpass Q in dB: alpha = sin(w0) / (2 * 10^(Q/20)).
    pub fn lowpass(sr: f64, f0: f64, q_db: f64) -> Coeffs {
        let w0 = 2.0 * std::f64::consts::PI * f0.min(sr / 2.0) / sr;
        let (sin, cos) = w0.sin_cos();
        let alpha = sin / (2.0 * 10f64.powf(q_db / 20.0));
        Coeffs::normalized(
            (1.0 - cos) / 2.0,
            1.0 - cos,
            (1.0 - cos) / 2.0,
            1.0 + alpha,
            -2.0 * cos,
            1.0 - alpha,
        )
    }

    pub fn highpass(sr: f64, f0: f64, q_db: f64) -> Coeffs {
        let w0 = 2.0 * std::f64::consts::PI * f0.min(sr / 2.0) / sr;
        let (sin, cos) = w0.sin_cos();
        let alpha = sin / (2.0 * 10f64.powf(q_db / 20.0));
        Coeffs::normalized(
            (1.0 + cos) / 2.0,
            -(1.0 + cos),
            (1.0 + cos) / 2.0,
            1.0 + alpha,
            -2.0 * cos,
            1.0 - alpha,
        )
    }
}

/// A stereo biquad in transposed direct form II.
#[derive(Clone, Copy, Debug)]
pub struct StereoBiquad {
    pub c: Coeffs,
    z1: [f64; 2],
    z2: [f64; 2],
}

impl StereoBiquad {
    pub fn new(c: Coeffs) -> StereoBiquad {
        StereoBiquad { c, z1: [0.0; 2], z2: [0.0; 2] }
    }

    #[inline]
    pub fn process(&mut self, ch: usize, x: f64) -> f64 {
        let c = &self.c;
        let y = c.b0 * x + self.z1[ch];
        self.z1[ch] = c.b1 * x - c.a1 * y + self.z2[ch];
        self.z2[ch] = c.b2 * x - c.a2 * y;
        y
    }

    pub fn reset(&mut self) {
        self.z1 = [0.0; 2];
        self.z2 = [0.0; 2];
    }
}

/// One-pole smoother matching Web Audio's `setTargetAtTime`:
/// v(t) = target + (v0 - target) * e^(-t / tau), sampled per frame.
#[derive(Clone, Copy, Debug)]
pub struct Smoothed {
    pub value: f64,
    pub target: f64,
    k: f64,
}

impl Smoothed {
    pub fn new(value: f64, sr: f64, tau_s: f64) -> Smoothed {
        Smoothed { value, target: value, k: 1.0 - (-1.0 / (tau_s * sr)).exp() }
    }

    pub fn set(&mut self, target: f64) {
        self.target = target;
    }

    /// Jump straight to the target, for state that must not glide (a load).
    pub fn snap(&mut self, value: f64) {
        self.value = value;
        self.target = value;
    }

    #[inline]
    pub fn tick(&mut self) -> f64 {
        if self.value != self.target {
            self.value += (self.target - self.value) * self.k;
            // Settle exactly, so a smoother at rest costs nothing and the
            // output is identical to one that never moved.
            if (self.target - self.value).abs() < 1e-9 {
                self.value = self.target;
            }
        }
        self.value
    }

    pub fn settled(&self) -> bool {
        self.value == self.target
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn gain_at(c: Coeffs, sr: f64, f: f64) -> f64 {
        // |H(e^jw)| evaluated directly from the coefficients.
        let w = 2.0 * std::f64::consts::PI * f / sr;
        let (s1, c1) = w.sin_cos();
        let (s2, c2) = (2.0 * w).sin_cos();
        let nr = c.b0 + c.b1 * c1 + c.b2 * c2;
        let ni = -(c.b1 * s1 + c.b2 * s2);
        let dr = 1.0 + c.a1 * c1 + c.a2 * c2;
        let di = -(c.a1 * s1 + c.a2 * s2);
        ((nr * nr + ni * ni) / (dr * dr + di * di)).sqrt()
    }

    fn db(x: f64) -> f64 {
        20.0 * x.log10()
    }

    #[test]
    fn shelves_reach_their_gain_far_from_the_corner() {
        let sr = 48000.0;
        let lo = Coeffs::lowshelf(sr, 250.0, -26.0);
        assert!((db(gain_at(lo, sr, 10.0)) + 26.0).abs() < 0.2);
        assert!(db(gain_at(lo, sr, 10000.0)).abs() < 0.2);
        let hi = Coeffs::highshelf(sr, 5000.0, 6.0);
        assert!((db(gain_at(hi, sr, 20000.0)) - 6.0).abs() < 0.5);
        assert!(db(gain_at(hi, sr, 50.0)).abs() < 0.1);
    }

    #[test]
    fn flat_eq_is_exactly_unity() {
        let sr = 48000.0;
        for c in [
            Coeffs::lowshelf(sr, 250.0, 0.0),
            Coeffs::peaking(sr, 1200.0, 1.0, 0.0),
            Coeffs::highshelf(sr, 5000.0, 0.0),
        ] {
            for f in [50.0, 250.0, 1200.0, 5000.0, 15000.0] {
                assert!(db(gain_at(c, sr, f)).abs() < 1e-9, "{c:?} at {f}");
            }
        }
    }

    #[test]
    fn peaking_hits_its_gain_at_the_center() {
        let sr = 48000.0;
        let p = Coeffs::peaking(sr, 1200.0, 1.0, -26.0);
        assert!((db(gain_at(p, sr, 1200.0)) + 26.0).abs() < 1e-6);
    }

    #[test]
    fn lowpass_and_highpass_read_q_in_db_like_web_audio() {
        // Web Audio: Q = 0.707 on a lowpass means 0.707 dB of resonance at
        // the corner, not the -3 dB a linear Q of 0.707 would give.
        let sr = 48000.0;
        let lp = Coeffs::lowpass(sr, 1000.0, 0.707);
        assert!((db(gain_at(lp, sr, 1000.0)) - 0.707).abs() < 0.01, "{}", db(gain_at(lp, sr, 1000.0)));
        let hp = Coeffs::highpass(sr, 1000.0, 0.707);
        assert!((db(gain_at(hp, sr, 1000.0)) - 0.707).abs() < 0.01);
    }

    #[test]
    fn smoother_follows_set_target_at_time() {
        let sr = 48000.0;
        let mut s = Smoothed::new(0.0, sr, 0.01);
        s.set(1.0);
        let mut v = 0.0;
        for _ in 0..480 {
            v = s.tick();
        }
        // One time-constant later the value is 1 - 1/e of the way there.
        assert!((v - (1.0 - (-1.0f64).exp())).abs() < 1e-3, "{v}");
        for _ in 0..48000 {
            s.tick();
        }
        assert!(s.settled());
    }
}
