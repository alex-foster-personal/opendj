//! Mixer knob laws, mirrored from the Web Audio engine.
//!
//! Every constant and curve here is copied from the page so the two engines can
//! be null-tested against each other on the same plan (plan 20-05). Change one
//! side only together with the other:
//!
//! - `apps/webui/frontend/src/lib/player/constants.ts` (EQ, TRIM, FILTER incl.
//!   `FILTER_GLIDE_DOMAIN` / `FILTER_GLIDE_S`, smoothing)
//! - `apps/webui/frontend/src/lib/rb/audio-engine-guards.ts` (`filterParamsFromKnob`)
//! - `apps/webui/frontend/src/lib/rb/audio-engine.svelte.ts` (`_xfGainFor`)

use crate::dsp::GlideDomain;
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
/// Mirrors `FILTER_Q_DB`: Q in dB, as Web Audio reads lowpass/highpass Q (see
/// `dsp::Coeffs::lowpass`). 0.707 dB is linear Q 1.085, a +1.74 dB peak just
/// inside the corner (+0.71 dB at it), not a flat Butterworth (#9791).
pub const FILTER_Q_DB: f64 = 0.707;
/// Mirrors `FILTER_SLOPE_DB_OCT` (FXFILT-09, issue #10010): 12 dB/oct is one
/// biquad per side, 24 adds a second section at the same corner. FXFILT-02 owns
/// resonance (#9826) on the first section; the second section's Q never carries it.
pub const FILTER_SLOPE_DB_OCT: u32 = 12;
const _: () = assert!(FILTER_SLOPE_DB_OCT == 12 || FILTER_SLOPE_DB_OCT == 24);
/// Mirrors `FILTER_SECOND_SECTION_Q_DB`: linear Q 1/sqrt(2) read in dB, a flat Butterworth.
pub const FILTER_SECOND_SECTION_Q_DB: f64 = -3.010299956639812;
/// Mirrors `FILTER_GLIDE_DOMAIN` in constants.ts: the domain the corner glides
/// in. The choice is calibration round filter-6 (MIXUX-05).
pub const FILTER_GLIDE_DOMAIN: GlideDomain = GlideDomain::Hz;
/// Mirrors `FILTER_GLIDE_S`: time constant of the corner glide, either domain.
pub const FILTER_GLIDE_S: f64 = 0.01;
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

/// Auto gain (AUTOGAIN-02): the per-track offset the page resolved
/// (`/api/v1/auto-gain`), applied at the trim stage under the TRIM knob.
/// Bounds mirror `apps/analysis_loudness/auto_gain.py` OFFSET_MIN/MAX_DB.
pub const AUTO_GAIN_MIN_DB: f64 = -12.0;
pub const AUTO_GAIN_MAX_DB: f64 = 12.0;

pub fn check_auto_gain_db(db: f64) -> Result<f64, EngineError> {
    if db.is_finite() && (AUTO_GAIN_MIN_DB..=AUTO_GAIN_MAX_DB).contains(&db) {
        Ok(db)
    } else {
        Err(EngineError::new(ErrorCode::Invalid, "auto gain must be finite and within -12..12 dB"))
    }
}

/// The trim stage's linear gain: TRIM knob times auto gain.
pub fn trim_stage_gain(knob: f64, auto_gain_db: f64) -> f64 {
    trim_gain_from_knob(knob) * 10f64.powf(auto_gain_db / 20.0)
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
    fn auto_gain_multiplies_under_the_trim_knob() {
        // AUTOGAIN-02: knob at unity, -6 dB of auto gain is 10^(-6/20).
        assert!((trim_stage_gain(0.5, -6.0) - 10f64.powf(-0.3)).abs() < 1e-12);
        assert_eq!(trim_stage_gain(0.5, 0.0), 1.0);
        // The knob still works on top: full knob doubles whatever auto gain left.
        assert!((trim_stage_gain(1.0, -6.0) - 2.0 * 10f64.powf(-0.3)).abs() < 1e-12);
        assert!(check_auto_gain_db(12.5).is_err());
        assert!(check_auto_gain_db(f64::NAN).is_err());
        assert_eq!(check_auto_gain_db(-12.0).unwrap(), -12.0);
    }

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

    /// Shared with the web law: written from `filterParamsFromKnob` by
    /// `apps/webui/frontend/tests/unit/filter-law-fixture.test.mjs` (#9791).
    const FILTER_LAW_FIXTURE: &str = include_str!("../tests/fixtures/filter-law.json");

    #[test]
    fn filter_law_matches_the_shared_fixture() {
        // [if] filter_params is off the web law fixture by more than 1e-9 on any field [then] fail, [else stop].
        let doc: serde_json::Value = serde_json::from_str(FILTER_LAW_FIXTURE).unwrap();
        let tol = doc["tolerance"].as_f64().unwrap();
        assert_eq!(tol, 1e-9);
        let cols: Vec<&str> = doc["columns"].as_array().unwrap().iter().map(|c| c.as_str().unwrap()).collect();
        assert_eq!(cols, ["knob", "lpHz", "hpHz", "dryGain", "lpWetGain", "hpWetGain"]);
        let rows = doc["rows"].as_array().unwrap();
        assert_eq!(rows.len(), 103, "101 grid positions plus both dead-zone inner edges");
        for row in rows {
            let r: Vec<f64> = row.as_array().unwrap().iter().map(|v| v.as_f64().unwrap()).collect();
            let p = filter_params(r[0]);
            let got = [p.lp_hz, p.hp_hz, p.dry, p.lp_wet, p.hp_wet];
            for (c, (got, want)) in got.iter().zip(&r[1..]).enumerate() {
                assert!((got - want).abs() <= tol, "knob {} {}: rust {got}, fixture {want}", r[0], cols[c + 1]);
            }
        }
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
