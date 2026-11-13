//! Per-deck waveform band gains: how much of each rekordbox ANLZ band (low,
//! mid, high) a deck's channel strip lets through right now.
//!
//! The page's waveforms are drawn from precomputed 3-band analysis that never
//! sees live audio. To show EQ and filter moves on them (as Serato and
//! rekordbox do), the engine reports, per deck, the linear magnitude of the
//! strip it is actually running (the three EQ biquads in series, then the
//! FILTER knob's dry/lowpass/highpass mix) evaluated at one representative
//! frequency per band. The page multiplies each band's amplitude by its gain.
//!
//! The biquads are designed exactly as `deck::Strip::build` designs them, from
//! the same knob laws in `mixer`, so a band gain is the strip's own response,
//! not a second model of it. Trim, fader and crossfader are level, not tone,
//! and are deliberately left out.

use crate::dsp::Coeffs;
use crate::mixer;

/// Representative frequency of each ANLZ band, low/mid/high, in Hz.
pub const WAVE_BAND_CENTRES_HZ: [f64; 3] = [100.0, 1000.0, 8000.0];

/// A band centre is never evaluated above this fraction of the sample rate:
/// past Nyquist the response folds back and stops meaning "this band".
const MAX_CENTRE_OF_SR: f64 = 0.45;

/// Linear gain of each band (low, mid, high) through a strip whose EQ knobs
/// are `eq` (low, mid, high, each 0..1 with 0.5 flat) and whose FILTER knob is
/// `filter` (0..1, 0.5 bypassed), at sample rate `sr`.
pub fn wave_band_gains(sr: f64, eq: [f64; 3], filter: f64) -> [f64; 3] {
    let db = eq.map(mixer::eq_db_from_knob);
    let eq_chain = [
        Coeffs::lowshelf(sr, mixer::EQ_FREQ_LOW_HZ, db[0]),
        Coeffs::peaking(sr, mixer::EQ_FREQ_MID_HZ, mixer::EQ_MID_Q, db[1]),
        Coeffs::highshelf(sr, mixer::EQ_FREQ_HIGH_HZ, db[2]),
    ];
    let fp = mixer::filter_params(filter);
    let lp = Coeffs::lowpass(sr, fp.lp_hz, mixer::FILTER_Q_DB);
    let hp = Coeffs::highpass(sr, fp.hp_hz, mixer::FILTER_Q_DB);
    WAVE_BAND_CENTRES_HZ.map(|centre| {
        let f = centre.min(sr * MAX_CENTRE_OF_SR);
        let eq_mag: f64 = eq_chain.iter().map(|c| c.magnitude_at(sr, f)).product();
        let (lr, li) = lp.response(sr, f);
        let (hr, hi) = hp.response(sr, f);
        let re = fp.dry + fp.lp_wet * lr + fp.hp_wet * hr;
        let im = fp.lp_wet * li + fp.hp_wet * hi;
        eq_mag * re.hypot(im)
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::dsp::StereoBiquad;

    const SR: f64 = 48000.0;
    const FLAT: [f64; 3] = [0.5, 0.5, 0.5];

    /// Independent measurement: run a sine at `f` through the real biquads in
    /// direct form I (the form the strip runs) and compare settled peaks. This
    /// checks `magnitude_at`'s closed form against the filter it describes.
    fn measured_gain(chain: &[Coeffs], f: f64) -> f64 {
        let mut bqs: Vec<StereoBiquad> = chain.iter().map(|c| StereoBiquad::new(*c)).collect();
        let n = SR as usize; // one second
        let mut peak = 0.0f64;
        for i in 0..n {
            let x = (2.0 * std::f64::consts::PI * f * i as f64 / SR).sin();
            let y = bqs.iter_mut().fold(x, |acc, b| b.process(0, acc));
            if i > n / 2 {
                peak = peak.max(y.abs());
            }
        }
        peak
    }

    fn close(a: f64, b: f64, tol: f64) -> bool {
        (a - b).abs() <= tol
    }

    #[test]
    fn a_flat_strip_passes_every_band_at_unity() {
        let g = wave_band_gains(SR, FLAT, 0.5);
        for (i, v) in g.iter().enumerate() {
            assert!(close(*v, 1.0, 1e-9), "band {i} flat gain {v}");
        }
        // Inside the filter's dead zone is still flat.
        let g = wave_band_gains(SR, FLAT, 0.505);
        assert!(g.iter().all(|v| close(*v, 1.0, 1e-9)), "dead zone {g:?}");
    }

    #[test]
    fn a_low_kill_takes_the_low_band_to_almost_nothing_and_leaves_the_highs() {
        let g = wave_band_gains(SR, [0.0, 0.5, 0.5], 0.5);
        let want = measured_gain(
            &[Coeffs::lowshelf(
                SR,
                mixer::EQ_FREQ_LOW_HZ,
                mixer::EQ_MIN_DB,
            )],
            100.0,
        );
        assert!(close(g[0], want, 2e-3), "low {} vs measured {want}", g[0]);
        assert!(g[0] < 0.1, "a kill reads as almost nothing: {}", g[0]);
        assert!(g[1] > 0.85, "mid barely moves: {}", g[1]);
        assert!(close(g[2], 1.0, 0.01), "high untouched: {}", g[2]);
    }

    #[test]
    fn each_band_kill_lands_on_its_own_band() {
        let mid = wave_band_gains(SR, [0.5, 0.0, 0.5], 0.5);
        let want = measured_gain(
            &[Coeffs::peaking(
                SR,
                mixer::EQ_FREQ_MID_HZ,
                mixer::EQ_MID_Q,
                mixer::EQ_MIN_DB,
            )],
            1000.0,
        );
        assert!(
            close(mid[1], want, 2e-3),
            "mid {} vs measured {want}",
            mid[1]
        );
        assert!(mid[1] < 0.1 && mid[0] > 0.8 && mid[2] > 0.8, "{mid:?}");

        let high = wave_band_gains(SR, [0.5, 0.5, 0.0], 0.5);
        let want = measured_gain(
            &[Coeffs::highshelf(
                SR,
                mixer::EQ_FREQ_HIGH_HZ,
                mixer::EQ_MIN_DB,
            )],
            8000.0,
        );
        assert!(
            close(high[2], want, 2e-3),
            "high {} vs measured {want}",
            high[2]
        );
        assert!(high[2] < 0.15 && close(high[0], 1.0, 0.01), "{high:?}");
    }

    #[test]
    fn a_full_boost_is_the_knob_laws_six_db() {
        let g = wave_band_gains(SR, [1.0, 0.5, 0.5], 0.5);
        let six_db = 10f64.powf(mixer::EQ_MAX_DB / 20.0);
        // 100 Hz sits well under the 250 Hz shelf corner, so near the full boost.
        assert!(g[0] > 1.6 && g[0] <= six_db + 1e-9, "low boost {}", g[0]);
    }

    #[test]
    fn the_filter_knob_folds_in_as_a_band_gain() {
        // Full CCW: lowpass at 130 Hz. Highs gone, mids heavily cut, lows kept.
        let lpf = wave_band_gains(SR, FLAT, 0.0);
        let lp = Coeffs::lowpass(SR, mixer::FILTER_LP_FLOOR_HZ, mixer::FILTER_Q_DB);
        assert!(close(lpf[1], measured_gain(&[lp], 1000.0), 2e-3), "{lpf:?}");
        assert!(lpf[2] < 0.01 && lpf[1] < 0.05 && lpf[0] > 0.7, "{lpf:?}");
        // Full CW: highpass at 6.3 kHz. Lows and mids gone, highs kept.
        let hpf = wave_band_gains(SR, FLAT, 1.0);
        assert!(hpf[0] < 0.01 && hpf[1] < 0.05 && hpf[2] > 0.7, "{hpf:?}");
        // The filter and the EQ multiply: a low kill under a highpass is lower still.
        let both = wave_band_gains(SR, [0.0, 0.5, 0.5], 1.0);
        assert!(both[0] < hpf[0], "{both:?} vs {hpf:?}");
    }

    #[test]
    fn gains_move_monotonically_with_the_knob() {
        // Overshoot control: a gain must track the knob in both directions,
        // not only report a kill.
        let mut last = 0.0;
        for k in [0.0, 0.1, 0.25, 0.4, 0.5, 0.6, 0.75, 1.0] {
            let g = wave_band_gains(SR, [k, 0.5, 0.5], 0.5)[0];
            assert!(g > last, "low gain at knob {k} = {g}, not above {last}");
            last = g;
        }
    }

    #[test]
    fn a_band_centre_above_nyquist_is_clamped_and_stays_finite() {
        let g = wave_band_gains(8000.0, [0.5, 0.5, 0.0], 0.0);
        assert!(g.iter().all(|v| v.is_finite() && *v >= 0.0), "{g:?}");
    }
}
