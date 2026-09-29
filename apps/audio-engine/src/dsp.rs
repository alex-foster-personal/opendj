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
        // Web Audio clamps the corner to Nyquist; there the whole band is below
        // it, so the shelf is a flat A^2 (Chromium `SetLowShelfParams`).
        if f0 >= sr / 2.0 {
            return Coeffs { b0: a * a, ..Coeffs::IDENTITY };
        }
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
        // A corner at or above Nyquist (e.g. 5 kHz at 8 kHz) leaves nothing to
        // shelve: Web Audio clamps it there and the filter is flat 1. Without
        // this, sin(w0) goes negative and the poles leave the unit circle.
        if f0 >= sr / 2.0 {
            return Coeffs::IDENTITY;
        }
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
        // Same Nyquist clamp: a peak at or above it is flat 1 in Web Audio.
        if f0 >= sr / 2.0 {
            return Coeffs::IDENTITY;
        }
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

    /// All-stop: nothing passes.
    pub const ZERO: Coeffs = Coeffs { b0: 0.0, b1: 0.0, b2: 0.0, a1: 0.0, a2: 0.0 };

    /// Web Audio reads lowpass/highpass Q in dB: alpha = sin(w0) / (2 * 10^(Q/20)).
    ///
    /// The corner is clamped to [0, Nyquist] with both ends handled exactly,
    /// as Chromium does (`Biquad::SetLowpassParams`): at Nyquist a lowpass
    /// passes everything, at 0 nothing. The cookbook formula at w0 = pi puts
    /// a double pole on z = -1, which grows without bound from any leftover
    /// filter state; the exact forms have no poles, so a state carried in
    /// from the previous coefficients drains in two samples.
    pub fn lowpass(sr: f64, f0: f64, q_db: f64) -> Coeffs {
        if f0 >= sr / 2.0 {
            return Coeffs::IDENTITY;
        }
        if f0 <= 0.0 {
            return Coeffs::ZERO;
        }
        let w0 = 2.0 * std::f64::consts::PI * f0 / sr;
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

    /// Highpass mirrors `lowpass`'s ends (`Biquad::SetHighpassParams`): at
    /// Nyquist it passes nothing, at 0 everything.
    pub fn highpass(sr: f64, f0: f64, q_db: f64) -> Coeffs {
        if f0 >= sr / 2.0 {
            return Coeffs::ZERO;
        }
        if f0 <= 0.0 {
            return Coeffs::IDENTITY;
        }
        let w0 = 2.0 * std::f64::consts::PI * f0 / sr;
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

/// A stereo biquad in direct form I, the form Chromium runs whenever a
/// parameter is automated (`platform/audio/biquad.cc`, `Biquad::Process`).
/// With coefficients that change every frame the form matters: the state of a
/// direct form I filter is its past inputs and outputs, which stay valid when
/// the coefficients move, while a transposed form's state is built from the
/// old coefficients. Measured against Chromium on a filter move, transposed
/// direct form II with per-frame coefficients is off by about 20 dB in the
/// first 5 ms; this form lands on the decoder floor.
#[derive(Clone, Copy, Debug)]
pub struct StereoBiquad {
    pub c: Coeffs,
    /// Per channel: x[n-1], x[n-2], y[n-1], y[n-2].
    h: [[f64; 4]; 2],
}

impl StereoBiquad {
    pub fn new(c: Coeffs) -> StereoBiquad {
        StereoBiquad { c, h: [[0.0; 4]; 2] }
    }

    #[inline]
    pub fn process(&mut self, ch: usize, x: f64) -> f64 {
        let c = &self.c;
        let [x1, x2, y1, y2] = self.h[ch];
        let y = c.b0 * x + c.b1 * x1 + c.b2 * x2 - c.a1 * y1 - c.a2 * y2;
        self.h[ch] = [x, x1, y, y1];
        y
    }

    pub fn reset(&mut self) {
        self.h = [[0.0; 4]; 2];
    }
}

/// One-pole smoother matching Web Audio's `setTargetAtTime`:
/// v(t) = target + (v0 - target) * e^(-t / tau), sampled per frame.
///
/// `tick` returns the value for the current frame and then advances, so the
/// frame a change lands on still plays v0 and the glide starts one frame
/// later. That is `setTargetAtTime`'s own shape (e^0 = 1 at its start time),
/// and measured against Chromium it is worth about 45 dB on a filter move.
///
/// This follows the spec, not Chromium 141 in full: a STEREO BiquadFilterNode
/// there advances a `setTargetAtTime` frequency glide twice per render quantum
/// from the second quantum after the event, so the page's filter settles about
/// twice as fast. Mono nodes and gain params step once per frame, as here.
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
        let out = self.value;
        if self.value != self.target {
            self.value += (self.target - self.value) * self.k;
            // Settle exactly, so a smoother at rest costs nothing and the
            // output is identical to one that never moved.
            if (self.target - self.value).abs() < 1e-9 {
                self.value = self.target;
            }
        }
        out
    }

    pub fn settled(&self) -> bool {
        self.value == self.target
    }
}

/// A linear glide over a fixed number of frames, restarted from wherever the
/// value is whenever the target changes. This is the page's EQ law
/// (`player/eq-apply.ts`: `setValueAtTime(current)` then
/// `linearRampToValueAtTime(target, now + 10 ms)`), and like `Smoothed` it
/// plays the start value on the frame the change lands on.
#[derive(Clone, Copy, Debug)]
pub struct LinearRamp {
    pub value: f64,
    pub target: f64,
    start: f64,
    len: u32,
    done: u32,
}

impl LinearRamp {
    pub fn new(value: f64, sr: f64, ramp_s: f64) -> LinearRamp {
        let len = (ramp_s * sr).round().max(1.0) as u32;
        LinearRamp { value, target: value, start: value, len, done: len }
    }

    pub fn set(&mut self, target: f64) {
        self.target = target;
        self.start = self.value;
        self.done = if target == self.value { self.len } else { 0 };
    }

    #[inline]
    pub fn tick(&mut self) -> f64 {
        let out = self.value;
        if self.done < self.len {
            self.done += 1;
            self.value = if self.done == self.len {
                self.target
            } else {
                self.start + (self.target - self.start) * (self.done as f64 / self.len as f64)
            };
        }
        out
    }

    pub fn settled(&self) -> bool {
        self.done == self.len
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

    fn stable(c: Coeffs) -> bool {
        // Both poles inside the unit circle (the stability triangle).
        c.a2.abs() < 1.0 && c.a1.abs() < 1.0 + c.a2
    }

    #[test]
    fn corners_at_or_above_nyquist_stay_stable_and_flat() {
        // 8 kHz is the lowest rate the CLI accepts; its Nyquist is below the
        // 5 kHz high-shelf corner.
        let sr = 8000.0;
        for g in [-26.0, 6.0] {
            let hi = Coeffs::highshelf(sr, 5000.0, g);
            assert!(stable(hi), "{hi:?}");
            assert_eq!(hi, Coeffs::IDENTITY);
            let pk = Coeffs::peaking(sr, 4000.0, 1.0, g);
            assert_eq!(pk, Coeffs::IDENTITY);
            let lo = Coeffs::lowshelf(sr, 4000.0, g);
            assert!((db(gain_at(lo, sr, 1000.0)) - g).abs() < 1e-9, "{lo:?}");
        }
        // Control: below Nyquist the shelf still shelves, stably.
        let hi = Coeffs::highshelf(sr, 3000.0, 6.0);
        assert!(stable(hi), "{hi:?}");
        assert!(db(gain_at(hi, sr, 3900.0)) > 5.0);
        // And just under it the clamp has not kicked in early.
        for c in [Coeffs::highshelf(sr, 3900.0, 6.0), Coeffs::peaking(sr, 3900.0, 1.0, 6.0)] {
            assert!(stable(c), "{c:?}");
            assert_ne!(c, Coeffs::IDENTITY);
        }
    }

    #[test]
    fn filters_clamped_to_nyquist_have_no_marginal_poles() {
        let sr = 8000.0;
        let q = crate::mixer::FILTER_Q;
        assert_eq!(Coeffs::lowpass(sr, 20000.0, q), Coeffs::IDENTITY);
        assert_eq!(Coeffs::lowpass(sr, 4000.0, q), Coeffs::IDENTITY);
        assert_eq!(Coeffs::highpass(sr, 6300.0, q), Coeffs::ZERO);
        assert_eq!(Coeffs::lowpass(sr, 0.0, q), Coeffs::ZERO);
        assert_eq!(Coeffs::highpass(sr, 0.0, q), Coeffs::IDENTITY);
        // Control: just under Nyquist the cookbook form still applies, stably.
        for c in [Coeffs::lowpass(sr, 3900.0, q), Coeffs::highpass(sr, 3900.0, q)] {
            assert!(stable(c), "{c:?}");
            assert_ne!(c, Coeffs::IDENTITY);
            assert_ne!(c, Coeffs::ZERO);
        }
        // Crossing Nyquist with state in the filter, as a FILTER knob turned
        // clockwise at 8 kHz does: the carried state drains, it never grows.
        for (below, above) in [
            (Coeffs::highpass(sr, 3000.0, q), Coeffs::highpass(sr, 6300.0, q)),
            (Coeffs::lowpass(sr, 3000.0, q), Coeffs::lowpass(sr, 20000.0, q)),
        ] {
            let mut f = StereoBiquad::new(below);
            let mut x = 0.3;
            for _ in 0..4000 {
                x = -x; // Nyquist-rate input charges the state hardest.
                f.process(0, x);
            }
            f.c = above;
            let mut last = 0.0f64;
            for i in 0..100_000 {
                let y = f.process(0, 0.0);
                assert!(y.is_finite(), "non-finite at {i}");
                last = y;
            }
            assert_eq!(last, 0.0, "{above:?}");
        }
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
    fn a_linear_ramp_lands_exactly_on_time() {
        // 10 ms at 48 kHz is 480 frames: halfway at 240, exact at 480.
        let mut r = LinearRamp::new(0.0, 48000.0, 0.01);
        assert!(r.settled());
        r.set(-26.0);
        for _ in 0..240 {
            r.tick();
        }
        assert!((r.value - -13.0).abs() < 1e-12, "{}", r.value);
        assert!(!r.settled());
        for _ in 0..240 {
            r.tick();
        }
        assert_eq!(r.value, -26.0);
        assert!(r.settled());
        // A new target mid-ramp starts from where the value is, not from the
        // old start or the old target.
        r.set(0.0);
        for _ in 0..120 {
            r.tick();
        }
        r.set(6.0);
        let from = r.value;
        assert!((from - -19.5).abs() < 1e-12, "{from}");
        for _ in 0..480 {
            r.tick();
        }
        assert_eq!(r.value, 6.0);
        // Control: the one-pole smoother is still only about 63% there at
        // 10 ms, which is the difference the ramp exists for.
        let mut sm = Smoothed::new(0.0, 48000.0, 0.01);
        sm.set(-26.0);
        for _ in 0..480 {
            sm.tick();
        }
        assert!((sm.value / -26.0 - 0.632).abs() < 0.01, "{}", sm.value);
    }

    #[test]
    fn smoother_follows_set_target_at_time() {
        let sr = 48000.0;
        let mut s = Smoothed::new(0.0, sr, 0.01);
        s.set(1.0);
        // The frame the change lands on still plays the start value, as
        // setTargetAtTime does at its start time; frame k is 1 - e^(-k/(tau sr)).
        assert_eq!(s.tick(), 0.0);
        let mut v = 0.0;
        for _ in 0..480 {
            v = s.tick();
        }
        assert!((v - (1.0 - (-1.0f64).exp())).abs() < 1e-9, "{v}");
        for _ in 0..48000 {
            s.tick();
        }
        assert!(s.settled());
    }

    #[test]
    fn linear_ramp_matches_linear_ramp_to_value_at_time() {
        let sr = 48000.0;
        let mut r = LinearRamp::new(-26.0, sr, 0.01);
        assert!(r.settled());
        r.set(6.0);
        let got: Vec<f64> = (0..482).map(|_| r.tick()).collect();
        assert_eq!(got[0], -26.0);
        assert!((got[240] + 10.0).abs() < 1e-12, "{}", got[240]);
        assert_eq!(got[480], 6.0);
        assert_eq!(got[481], 6.0);
        assert!(r.settled());
        // Retargeting mid-ramp restarts from the current value over a full
        // ramp, as cancelScheduledValues + setValueAtTime(current) does.
        r.set(0.0);
        for _ in 0..240 {
            r.tick();
        }
        r.set(6.0);
        assert_eq!(r.tick(), 3.0);
        for k in 1..480 {
            assert!((r.tick() - (3.0 + 3.0 * k as f64 / 480.0)).abs() < 1e-12);
        }
        assert!(r.settled());
        assert_eq!(r.tick(), 6.0);
        // Control: setting the value it already has does not start a ramp.
        r.set(6.0);
        assert!(r.settled());
    }
}
