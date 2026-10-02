//! Master Tempo (key lock) and key shift through Signalsmith Stretch (plan 20-04).
//!
//! The stretcher is the upstream C++ library (MIT) through the
//! `signalsmith-stretch` crate; nothing here reimplements it. What this module
//! adds is the way the page's AudioWorklet drives it
//! (`signalsmith-stretch` 1.3.2, `SignalsmithStretch.mjs`, `process`), so the
//! two engines stretch the same way:
//!
//! - Output is made in fixed quanta of 128 frames, the worklet's render
//!   quantum. A fixed quantum keeps renders independent of the caller's block
//!   size (D6).
//! - Every quantum re-seeks: the stretcher is handed the last `input + output
//!   latency` frames of the track ending `output latency x rate + input
//!   latency` past the playhead, then asked for one quantum with no new
//!   input. The output is time-aligned with the playhead, so position, loops
//!   and jumps need no latency bookkeeping ("constantly seeking, so we don't
//!   have to worry about the input buffers needing to be a rate-dependent
//!   size", upstream's comment).
//! - Pitch: `12 * log2(tempo)` when Master Tempo is off, 0 when it is on, plus
//!   the key shift (`player/key/camelot.ts` `composeStretchSemitones`), with
//!   the worklet's default 8 kHz tonality limit.
//!
//! Unlike the page, a deck at Master Tempo off and no key shift does not go
//! through the stretcher at all: plain varispeed has no latency, costs about
//! a fortieth as much, and is what a DJ expects from a pitch fader.

use signalsmith_stretch::Stretch;

/// Frames per stretcher call, the Web Audio render quantum.
pub const QUANTUM: usize = 128;

/// The worklet's default `tonalityHz`.
const TONALITY_HZ: f32 = 8000.0;

pub struct Stretcher {
    st: Stretch,
    sr: f64,
    in_lat: usize,
    out_lat: usize,
    /// The STFT hop of `presetDefault` (30 ms), which the crate does not expose.
    interval: usize,
    /// Extra priming frames that set this stretcher's block phase.
    phase: usize,
    /// Interleaved stereo, `in_lat + out_lat` frames: the input handed to
    /// `seek` each quantum.
    seek_buf: Vec<f32>,
    /// Interleaved stereo, one quantum of output.
    out: Vec<f32>,
    /// Frames of `out` already played; `QUANTUM` when it is used up.
    read: usize,
    semitones: f32,
}

impl Stretcher {
    /// Allocates the stretcher and its buffers; call off the audio thread.
    /// `slot` (a deck's mixer slot) offsets its block phase by a quarter
    /// interval per slot: the stretcher does its FFT work once per interval
    /// (30 ms), and four decks started on the same frame would otherwise all
    /// do it in the same callback.
    pub fn new(sample_rate: f64, slot: usize) -> Stretcher {
        let st = Stretch::preset_default(2, sample_rate as u32);
        let (in_lat, out_lat) = (st.input_latency(), st.output_latency());
        let mut s = Stretcher {
            st,
            sr: sample_rate,
            in_lat,
            out_lat,
            interval: (sample_rate * 0.03) as usize,
            phase: 0,
            seek_buf: vec![0.0; (in_lat + out_lat) * 2],
            out: vec![0.0; QUANTUM * 2],
            read: QUANTUM,
            semitones: f32::NAN,
        };
        s.phase = (slot % 4) * s.interval / 4;
        s.set_semitones(0.0);
        s
    }

    /// Input plus output latency in frames: how much track the stretcher
    /// looks at around the playhead.
    pub fn window(&self) -> usize {
        self.in_lat + self.out_lat
    }

    fn set_semitones(&mut self, semitones: f32) {
        if semitones != self.semitones {
            self.st.set_transpose_factor_semitones(semitones, Some(TONALITY_HZ / self.sr as f32));
            self.semitones = semitones;
        }
    }

    /// Drop what is buffered; the next frame starts a fresh quantum.
    pub fn discard(&mut self) {
        self.read = QUANTUM;
    }

    /// Clear the stretcher's history and fill it as if it had been playing
    /// up to `pos`, so output starts at full level instead of fading in over
    /// the output latency. Runs `out_lat / QUANTUM` quanta at once, about
    /// 1.5 ms of CPU; called only when a deck starts stretching.
    pub fn prime(&mut self, pcm: &[f32], frames: usize, pos: f64, rate: f64, semitones: f32) {
        self.st.reset();
        // A whole number of STFT intervals, so the stretcher's block phase
        // after priming is the one a freshly reset worklet has at its first
        // quantum (the page's output then lines up with deck 1's), plus the
        // slot's stagger.
        let len = self.out_lat.div_ceil(self.interval) * self.interval + self.phase;
        let mut done = 0;
        while done < len {
            let n = QUANTUM.min(len - done);
            self.run(pcm, frames, pos - (len - done) as f64 * rate, rate, semitones, n);
            done += n;
        }
        self.read = QUANTUM;
    }

    /// Next output frame for a playhead at `pos` (source frames) moving
    /// `rate` source frames per output frame.
    #[inline]
    pub fn next(&mut self, pcm: &[f32], frames: usize, pos: f64, rate: f64, semitones: f32) -> (f32, f32) {
        if self.read == QUANTUM {
            self.quantum(pcm, frames, pos, rate, semitones);
        }
        let i = self.read * 2;
        self.read += 1;
        (self.out[i], self.out[i + 1])
    }

    fn quantum(&mut self, pcm: &[f32], frames: usize, pos: f64, rate: f64, semitones: f32) {
        self.run(pcm, frames, pos, rate, semitones, QUANTUM);
        self.read = 0;
    }

    /// Seek to `pos` and render `n <= QUANTUM` frames into `out`.
    fn run(&mut self, pcm: &[f32], frames: usize, pos: f64, rate: f64, semitones: f32, n: usize) {
        self.set_semitones(semitones);
        let len = self.window();
        let end = (pos + self.out_lat as f64 * rate + self.in_lat as f64).round() as i64;
        let start = end - len as i64;
        for (i, f) in self.seek_buf.chunks_exact_mut(2).enumerate() {
            let k = start + i as i64;
            if k >= 0 && (k as usize) < frames {
                let k = k as usize * 2;
                f[0] = pcm[k];
                f[1] = pcm[k + 1];
            } else {
                f[0] = 0.0;
                f[1] = 0.0;
            }
        }
        self.st.seek(&self.seek_buf, rate);
        self.st.process([] as [f32; 0], &mut self.out[..n * 2]);
    }
}

/// Transposition the stretcher applies: `composeStretchSemitones` in
/// `player/key/camelot.ts`.
pub fn semitones(tempo: f64, master_tempo: bool, key_shift: i32) -> f32 {
    let mt = if master_tempo { 0.0 } else { 12.0 * tempo.log2() };
    (mt + key_shift as f64) as f32
}
