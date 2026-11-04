//! Krumhansl-Kessler key scoring over a 12-bin chroma vector.
//!
//! A port of `apps/analysis_key/profiles.py`, kept to its exact rules so a
//! live key and the library's offline key are answers from one instrument:
//!
//! - the same profile constants (Krumhansl & Kessler 1982);
//! - the same 24-candidate index space, `2 * pitch_class + (1 if minor)`;
//! - the same score, the dot product of the unit-normalized chroma with the
//!   unit-normalized profile rotated to each tonic (`+ 1e-9` in both norms),
//!   which is a cosine, not a Pearson correlation;
//! - the same ranking: highest score first, ties to the lower index (Python's
//!   `sorted(..., reverse=True)` is stable).
//!
//! Camelot and Open Key follow `apps/analysis_key/canon.py` (through
//! `apps.equivalence.normalisers`): C major is 8B / 1d, A minor 8A / 1m, and
//! the Open Key number is the Camelot number plus five, mod 12, 1-indexed.

/// One rotation per pitch class in each mode.
pub const N_KEYS: usize = 24;

/// `apps/analysis_key/segments.py` `EMISSION_GAIN`: the scale that turns a
/// difference between two near-equal cosines into a decision. Only the
/// segment emissions use it; no per-key probability is published, because
/// that mapping is one of the uncalibrated TCLIVE-6567e constants.
pub(crate) const EMISSION_GAIN: f64 = 24.0;

const KK_MAJOR: [f64; 12] = [6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88];
const KK_MINOR: [f64; 12] = [6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17];

/// The rekordbox-style spelling `canon.py` writes (flats, but F#).
const SPELLING: [&str; 12] = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"];

/// A key: pitch class 0 (C) to 11 (B) and a mode bit.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub struct Key {
    pub pitch_class: u8,
    pub minor: bool,
}

impl Key {
    pub fn from_index(index: usize) -> Key {
        assert!(index < N_KEYS, "key index must be 0..{}, got {index}", N_KEYS - 1);
        Key {
            pitch_class: (index / 2) as u8,
            minor: index % 2 == 1,
        }
    }

    pub fn index(self) -> usize {
        2 * self.pitch_class as usize + usize::from(self.minor)
    }

    fn camelot_number(self) -> u32 {
        let pc = u32::from(self.pitch_class);
        let n = if self.minor { (7 * pc + 5) % 12 } else { (7 * pc + 8) % 12 };
        if n == 0 {
            12
        } else {
            n
        }
    }

    /// `8B` for C major, `8A` for A minor.
    pub fn camelot(self) -> String {
        format!("{}{}", self.camelot_number(), if self.minor { 'A' } else { 'B' })
    }

    /// `1d` for C major, `1m` for A minor.
    pub fn open_key(self) -> String {
        let n = (self.camelot_number() + 5 - 1) % 12 + 1;
        format!("{}{}", n, if self.minor { 'm' } else { 'd' })
    }

    /// `C`, `F#m`, `Bb`.
    pub fn name(self) -> String {
        let base = SPELLING[self.pitch_class as usize];
        if self.minor {
            format!("{base}m")
        } else {
            base.to_string()
        }
    }
}

fn unit12(v: &[f64; 12]) -> [f64; 12] {
    let norm = v.iter().map(|x| x * x).sum::<f64>().sqrt() + 1e-9;
    let mut out = [0.0; 12];
    for (o, x) in out.iter_mut().zip(v) {
        *o = x / norm;
    }
    out
}

/// `(24, 12)`: row `i` is key `i`'s unit profile, index 0 is C.
/// `np.roll(profile, pc)[j] == profile[(j - pc) mod 12]`.
fn profile_matrix() -> [[f64; 12]; N_KEYS] {
    let major = unit12(&KK_MAJOR);
    let minor = unit12(&KK_MINOR);
    let mut m = [[0.0; 12]; N_KEYS];
    for (index, row) in m.iter_mut().enumerate() {
        let profile = if index % 2 == 0 { &major } else { &minor };
        let pc = index / 2;
        for (j, cell) in row.iter_mut().enumerate() {
            *cell = profile[(j + 12 - pc) % 12];
        }
    }
    m
}

/// The 24 scores of one chroma vector, in key-index order
/// (`profiles.correlation_matrix` on one column).
pub fn scores(chroma: &[f64; 12]) -> [f64; N_KEYS] {
    thread_local! {
        static PROFILES: [[f64; 12]; N_KEYS] = profile_matrix();
    }
    let c = unit12(chroma);
    PROFILES.with(|m| {
        let mut out = [0.0; N_KEYS];
        for (o, row) in out.iter_mut().zip(m.iter()) {
            *o = row.iter().zip(&c).map(|(a, b)| a * b).sum();
        }
        out
    })
}

/// Key indices ranked best first, ties to the lower index.
pub fn ranked(scores: &[f64; N_KEYS]) -> [usize; N_KEYS] {
    let mut idx: [usize; N_KEYS] = std::array::from_fn(|i| i);
    // A stable sort on descending score keeps the lower index first in a tie.
    idx.sort_by(|&a, &b| scores[b].partial_cmp(&scores[a]).unwrap_or(std::cmp::Ordering::Equal));
    idx
}

/// A whole estimate in `profiles.KeyEstimate`'s terms: the best key, its
/// score floored at 0 (`confidence`), and its lead over the second best
/// floored at 0 (`margin`).
#[derive(Clone, Debug, PartialEq)]
pub struct Estimate {
    pub scores: [f64; N_KEYS],
    pub order: [usize; N_KEYS],
    pub key: Key,
    pub confidence: f64,
    pub margin: f64,
}

/// `None` for a chroma with no energy: a silent vector has no key, and
/// scoring it would publish C major (index 0 wins every tie) as a finding.
pub fn estimate(chroma: &[f64; 12]) -> Option<Estimate> {
    if chroma.iter().all(|&x| x.abs() < 1e-12) || chroma.iter().any(|x| !x.is_finite()) {
        return None;
    }
    let scores = scores(chroma);
    let order = ranked(&scores);
    let best = scores[order[0]];
    let second = scores[order[1]];
    Some(Estimate {
        scores,
        order,
        key: Key::from_index(order[0]),
        confidence: best.max(0.0),
        margin: (best - second).max(0.0),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn camelot_and_open_key_match_the_canon_table() {
        // Major then minor, C..B, from apps.equivalence.normalisers' Camelot wheel.
        let major = ["8B", "3B", "10B", "5B", "12B", "7B", "2B", "9B", "4B", "11B", "6B", "1B"];
        let minor = ["5A", "12A", "7A", "2A", "9A", "4A", "11A", "6A", "1A", "8A", "3A", "10A"];
        for pc in 0..12u8 {
            let maj = Key {
                pitch_class: pc,
                minor: false,
            };
            let min = Key {
                pitch_class: pc,
                minor: true,
            };
            assert_eq!(maj.camelot(), major[pc as usize], "pc {pc} major");
            assert_eq!(min.camelot(), minor[pc as usize], "pc {pc} minor");
            for k in [maj, min] {
                let cam: u32 = k.camelot().trim_end_matches(['A', 'B']).parse().unwrap();
                let ok: u32 = k.open_key().trim_end_matches(['d', 'm']).parse().unwrap();
                assert_eq!(ok, (cam + 5 - 1) % 12 + 1);
            }
        }
        assert_eq!(
            Key {
                pitch_class: 0,
                minor: false
            }
            .open_key(),
            "1d"
        );
        assert_eq!(
            Key {
                pitch_class: 9,
                minor: true
            }
            .open_key(),
            "1m"
        );
        assert_eq!(
            Key {
                pitch_class: 6,
                minor: true
            }
            .name(),
            "F#m"
        );
        assert_eq!(
            Key {
                pitch_class: 10,
                minor: false
            }
            .name(),
            "Bb"
        );
    }

    #[test]
    fn the_index_space_is_two_pc_plus_minor() {
        for i in 0..N_KEYS {
            assert_eq!(Key::from_index(i).index(), i);
        }
        assert_eq!(
            Key::from_index(19),
            Key {
                pitch_class: 9,
                minor: true
            }
        );
    }

    #[test]
    fn a_one_hot_tonic_scores_its_own_profile_weight() {
        // chroma = e_C: the score of key i is its unit profile's value at C.
        let mut c = [0.0; 12];
        c[0] = 1.0;
        let s = scores(&c);
        let major = unit12(&KK_MAJOR);
        let minor = unit12(&KK_MINOR);
        assert!((s[0] - major[0]).abs() < 1e-9);
        assert!((s[1] - minor[0]).abs() < 1e-9);
        // G major (pc 7) sees C as its fourth (index 5).
        assert!((s[14] - major[5]).abs() < 1e-9);
        // A minor (pc 9) sees C as its minor third (index 3).
        assert!((s[19] - minor[3]).abs() < 1e-9);
    }

    #[test]
    fn triads_pick_their_keys_and_silence_picks_none() {
        let triad = |pcs: &[usize]| {
            let mut c = [0.0; 12];
            for &p in pcs {
                c[p] = 1.0;
            }
            c
        };
        assert_eq!(estimate(&triad(&[0, 4, 7])).unwrap().key.camelot(), "8B");
        assert_eq!(estimate(&triad(&[9, 0, 4])).unwrap().key.camelot(), "8A");
        assert!(estimate(&[0.0; 12]).is_none());
    }
}
