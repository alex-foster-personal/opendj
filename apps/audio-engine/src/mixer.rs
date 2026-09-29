//! Mixer knob laws, mirrored from the Web Audio engine.
//!
//! Every constant and curve here is copied from the page so the two engines can
//! be null-tested against each other on the same plan (plan 20-05). Change one
//! side only together with the other:
//!
//! - `apps/webui/frontend/src/lib/player/constants.ts` (EQ, TRIM, FILTER, smoothing)
//! - `apps/webui/frontend/src/lib/rb/audio-engine-guards.ts` (`filterParamsFromKnob`)
//! - `apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts` (`_xfGainFor`)

use crate::engine::{EngineError, ErrorCode};

pub const EQ_FREQ_LOW_HZ: f64 = 250.0;
pub const EQ_FREQ_MID_HZ: f64 = 1200.0;
pub const EQ_FREQ_HIGH_HZ: f64 = 5000.0;
pub const EQ_MID_Q: f64 = 1.0;
/// Knob 0 is a deep DJ-mixer cut, knob 1 a gentle boost.
pub const EQ_MIN_DB: f64 = -26.0;
pub const EQ_MAX_DB: f64 = 6.0;
/// TRIM knob 0..1 maps linearly to 0..2x amplitude (0.5 is unity).
pub const TRIM_MAX_GAIN: f64 = 2.0;

pub const FILTER_DEADZONE_FRAC: f64 = 0.02;
pub const FILTER_LP_CEILING_HZ: f64 = 20000.0;
pub const FILTER_LP_FLOOR_HZ: f64 = 130.0;
pub const FILTER_HP_FLOOR_HZ: f64 = 30.0;
pub const FILTER_HP_CEILING_HZ: f64 = 6300.0;
/// Passed to Web Audio's `BiquadFilterNode.Q`, which for lowpass and highpass
/// is read in dB (see `dsp::Biquad::lowpass`).
pub const FILTER_Q: f64 = 0.707;
/// Smoothing time-constant for every parameter change except EQ (anti-zipper).
pub const PARAM_SMOOTH_S: f64 = 0.01;
/// EQ gain moves in a straight line in dB over this long, not exponentially
/// (`player/eq-apply.ts`, called with `PARAM_SMOOTH_S` as its ramp length).
pub const EQ_RAMP_S: f64 = 0.01;

/// 0 -> EQ_MIN_DB, 0.5 -> 0 dB (flat), 1 -> EQ_MAX_DB. Piecewise linear.
pub fn eq_db_from_knob(value: f64) -> f64 {
    if value <= 0.5 {
        EQ_MIN_DB * (1.0 - value * 2.0)
    } else {
        EQ_MAX_DB * (value * 2.0 - 1.0)
    }
}

pub fn trim_gain_from_knob(value: f64) -> f64 {
    value * TRIM_MAX_GAIN
}

/// Crossfader assignment of one channel.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub enum Assign {
    A,
    B,
    #[default]
    Thru,
}

impl Assign {
    pub fn parse(s: &str) -> Option<Assign> {
        match s {
            "A" => Some(Assign::A),
            "B" => Some(Assign::B),
            "THRU" => Some(Assign::Thru),
            _ => None,
        }
    }

    pub fn as_str(self) -> &'static str {
        match self {
            Assign::A => "A",
            Assign::B => "B",
            Assign::Thru => "THRU",
        }
    }
}

/// Equal-power crossfade gain for one bus assignment at position x (0..1).
pub fn xf_gain(assign: Assign, x: f64) -> f64 {
    match assign {
        Assign::Thru => 1.0,
        Assign::A => (x * std::f64::consts::PI / 2.0).cos(),
        Assign::B => ((1.0 - x) * std::f64::consts::PI / 2.0).cos(),
    }
}

/// Corner frequencies and dry/wet gains for the two parallel filter biquads.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct FilterParams {
    pub lp_hz: f64,
    pub hp_hz: f64,
    pub dry: f64,
    pub lp_wet: f64,
    pub hp_wet: f64,
}

/// FILTER knob law: lowpass for CCW, highpass for CW, swept exponentially in
/// Hz, with a dry bypass inside the dead zone around the 0.5 detent. Only the
/// selected side's wet gain is nonzero.
pub fn filter_params(value: f64) -> FilterParams {
    let color = (value - 0.5) * 2.0;
    if color.abs() < FILTER_DEADZONE_FRAC {
        return FilterParams {
            lp_hz: FILTER_LP_CEILING_HZ,
            hp_hz: FILTER_HP_FLOOR_HZ,
            dry: 1.0,
            lp_wet: 0.0,
            hp_wet: 0.0,
        };
    }
    let u = (color.abs() - FILTER_DEADZONE_FRAC) / (1.0 - FILTER_DEADZONE_FRAC);
    if color < 0.0 {
        let lp_hz = FILTER_LP_CEILING_HZ * (FILTER_LP_FLOOR_HZ / FILTER_LP_CEILING_HZ).powf(u);
        FilterParams { lp_hz, hp_hz: FILTER_HP_FLOOR_HZ, dry: 0.0, lp_wet: 1.0, hp_wet: 0.0 }
    } else {
        let hp_hz = FILTER_HP_FLOOR_HZ * (FILTER_HP_CEILING_HZ / FILTER_HP_FLOOR_HZ).powf(u);
        FilterParams { lp_hz: FILTER_LP_CEILING_HZ, hp_hz, dry: 0.0, lp_wet: 0.0, hp_wet: 1.0 }
    }
}

/// Every mixer knob is defined over the closed unit range, as on the page
/// (`assertUnitRange`). Out-of-range values are refused, never clamped.
pub fn check_unit(value: f64) -> Result<f64, EngineError> {
    if value.is_finite() && (0.0..=1.0).contains(&value) {
        Ok(value)
    } else {
        Err(EngineError::new(ErrorCode::Invalid, "knob value must be finite and within 0..1"))
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn eq_law_endpoints_and_center() {
        assert_eq!(eq_db_from_knob(0.0), EQ_MIN_DB);
        assert_eq!(eq_db_from_knob(0.5), 0.0);
        assert_eq!(eq_db_from_knob(1.0), EQ_MAX_DB);
        assert!((eq_db_from_knob(0.25) - EQ_MIN_DB / 2.0).abs() < 1e-12);
    }

    #[test]
    fn crossfader_is_equal_power() {
        let h = std::f64::consts::FRAC_1_SQRT_2;
        assert!((xf_gain(Assign::A, 0.0) - 1.0).abs() < 1e-12);
        assert!(xf_gain(Assign::A, 1.0).abs() < 1e-12);
        assert!((xf_gain(Assign::A, 0.5) - h).abs() < 1e-12);
        assert!(xf_gain(Assign::B, 0.0).abs() < 1e-12);
        assert!((xf_gain(Assign::B, 1.0) - 1.0).abs() < 1e-12);
        assert!((xf_gain(Assign::B, 0.5) - h).abs() < 1e-12);
        for x in [0.0, 0.5, 1.0] {
            assert_eq!(xf_gain(Assign::Thru, x), 1.0);
            let pa = xf_gain(Assign::A, x).powi(2);
            let pb = xf_gain(Assign::B, x).powi(2);
            assert!((pa + pb - 1.0).abs() < 1e-12, "power sum at {x}");
        }
    }

    #[test]
    fn filter_dead_zone_is_dry_and_sides_are_exclusive() {
        let c = filter_params(0.5);
        assert_eq!((c.dry, c.lp_wet, c.hp_wet), (1.0, 0.0, 0.0));
        let ccw = filter_params(0.0);
        assert_eq!((ccw.dry, ccw.lp_wet, ccw.hp_wet), (0.0, 1.0, 0.0));
        assert!((ccw.lp_hz - FILTER_LP_FLOOR_HZ).abs() < 1e-9);
        let cw = filter_params(1.0);
        assert_eq!((cw.dry, cw.lp_wet, cw.hp_wet), (0.0, 0.0, 1.0));
        assert!((cw.hp_hz - FILTER_HP_CEILING_HZ).abs() < 1e-9);
    }

    #[test]
    fn unit_check_refuses_rather_than_clamps() {
        assert!(check_unit(0.0).is_ok());
        assert!(check_unit(1.0).is_ok());
        assert!(check_unit(1.0000001).is_err());
        assert!(check_unit(-0.1).is_err());
        assert!(check_unit(f64::NAN).is_err());
    }
}
