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

// ----- offline track render (library CLI, issue #12231) --------------------

use serde::Serialize;
use std::env;
use std::fs::File;
use std::io::{BufWriter, Read, Write};
use std::path::{Path, PathBuf};

/// When set in the environment, offline render refuses instead of falling back
/// to varispeed (tests and fail-fast callers).
pub fn stretch_force_unavailable() -> bool {
    matches!(env::var("ODJ_STRETCH_FORCE_UNAVAILABLE").as_deref(), Ok("1") | Ok("true"))
}

#[derive(Debug, Clone, Copy)]
pub struct TrackRenderSpec {
    pub tempo_ratio: f64,
    pub master_tempo: bool,
    pub key_shift_semitones: i32,
    /// Master Tempo / key lock: a tempo change must go through the stretcher.
    pub key_lock: bool,
}

#[derive(Debug, Clone, Serialize)]
pub struct TrackRenderReceipt {
    pub engine: &'static str,
    pub varispeed_fallback: bool,
    pub requested_semitones: f64,
    pub measured_semitones: Option<f64>,
    pub measured_duration_s: f64,
    pub source_duration_s: f64,
    pub output_frames: usize,
    pub sample_rate: u32,
    pub tempo_ratio: f64,
    pub master_tempo: bool,
    pub key_shift_semitones: i32,
}

fn hermite_sample(pcm: &[f32], frames: usize, pos: f64) -> (f64, f64) {
    let i = pos.floor();
    let t = pos - i;
    let i = i as isize;
    let last = frames as isize - 1;
    let mut k = [i - 1, i, i + 1, i + 2];
    for k in k.iter_mut() {
        *k = (*k).clamp(0, last) * 2;
    }
    let mut out = [0.0; 2];
    for (ch, o) in out.iter_mut().enumerate() {
        let at = |j: usize| pcm[k[j] as usize + ch] as f64;
        let (xm1, x0, x1, x2) = (at(0), at(1), at(2), at(3));
        let c1 = 0.5 * (x1 - xm1);
        let c2 = xm1 - 2.5 * x0 + 2.0 * x1 - 0.5 * x2;
        let c3 = 0.5 * (x2 - xm1) + 1.5 * (x0 - x1);
        *o = ((c3 * t + c2) * t + c1) * t + x0;
    }
    (out[0], out[1])
}

fn uses_stretcher(spec: &TrackRenderSpec) -> bool {
    spec.master_tempo && (spec.tempo_ratio - 1.0).abs() > 1e-12
        || spec.key_shift_semitones != 0
}

fn must_use_stretcher(spec: &TrackRenderSpec) -> bool {
    spec.key_lock && (spec.tempo_ratio - 1.0).abs() > 1e-12
        || spec.key_shift_semitones != 0
        || spec.master_tempo && (spec.tempo_ratio - 1.0).abs() > 1e-12
}

fn estimate_fundamental_hz(pcm: &[f32], sample_rate: u32) -> Option<f64> {
    if pcm.len() < sample_rate as usize * 4 {
        return None;
    }
    let start = pcm.len().saturating_sub(sample_rate as usize);
    let tail = &pcm[start..];
    let mut crossings = 0usize;
    let mut prev = tail[0];
    for &s in tail.iter().skip(1).step_by(2) {
        if (prev <= 0.0 && s > 0.0) || (prev >= 0.0 && s < 0.0) {
            crossings += 1;
        }
        prev = s;
    }
    if crossings < 4 {
        return None;
    }
    let seconds = (tail.len() / 2) as f64 / sample_rate as f64;
    Some(crossings as f64 / (2.0 * seconds))
}

fn measured_semitones_from_output(
    source_hz: Option<f64>,
    out: &[f32],
    sample_rate: u32,
) -> Option<f64> {
    let src = source_hz?;
    let out_hz = estimate_fundamental_hz(out, sample_rate)?;
    if src <= 0.0 || out_hz <= 0.0 {
        return None;
    }
    Some(12.0 * (out_hz / src).log2())
}

/// Render an interleaved-stereo PCM buffer offline through stretch or varispeed.
pub fn render_interleaved(
    pcm: &[f32],
    source_frames: usize,
    sample_rate: u32,
    spec: &TrackRenderSpec,
    source_fundamental_hz: Option<f64>,
) -> Result<(Vec<f32>, TrackRenderReceipt), String> {
    if source_frames == 0 || pcm.len() < source_frames * 2 {
        return Err("source has no audio frames".into());
    }
    if !(0.5..=2.0).contains(&spec.tempo_ratio) {
        return Err(format!(
            "tempo_ratio {} is outside 0.5..2.0",
            spec.tempo_ratio
        ));
    }
    if spec.key_shift_semitones.abs() > 12 {
        return Err(format!(
            "key_shift {} is outside -12..12",
            spec.key_shift_semitones
        ));
    }
    let stretching = uses_stretcher(spec);
    if must_use_stretcher(spec) && stretch_force_unavailable() {
        return Err("stretch engine unavailable".into());
    }
    let source_duration_s = source_frames as f64 / sample_rate as f64;
    let requested =
        semitones(spec.tempo_ratio, spec.master_tempo, spec.key_shift_semitones) as f64;
    let step = spec.tempo_ratio;
    let end = source_frames as f64;
    let mut out: Vec<f32> = Vec::new();
    let (engine, varispeed_fallback) = if stretching {
        if stretch_force_unavailable() {
            return Err("stretch engine unavailable".into());
        }
        let mut stretcher = Stretcher::new(sample_rate as f64, 0);
        let semis = semitones(spec.tempo_ratio, spec.master_tempo, spec.key_shift_semitones);
        stretcher.prime(pcm, source_frames, 0.0, step, semis);
        let mut pos = 0.0f64;
        let mut run = 0usize;
        while pos < end {
            let (l, r) = stretcher.next(pcm, source_frames, pos, step, semis);
            out.extend_from_slice(&[l, r]);
            run += 1;
            pos = run as f64 * step;
        }
        ("stretch", false)
    } else if spec.key_lock && (spec.tempo_ratio - 1.0).abs() > 1e-12 {
        return Err("key lock forbids varispeed fallback for this tempo change".into());
    } else {
        let mut pos = 0.0f64;
        while pos < end {
            let (l, r) = hermite_sample(pcm, source_frames, pos);
            out.extend_from_slice(&[l as f32, r as f32]);
            pos += step;
        }
        ("varispeed", false)
    };
    let output_frames = out.len() / 2;
    let measured_duration_s = output_frames as f64 / sample_rate as f64;
    let measured = measured_semitones_from_output(source_fundamental_hz, &out, sample_rate);
    let receipt = TrackRenderReceipt {
        engine,
        varispeed_fallback,
        requested_semitones: requested,
        measured_semitones: measured,
        measured_duration_s,
        source_duration_s,
        output_frames,
        sample_rate,
        tempo_ratio: spec.tempo_ratio,
        master_tempo: spec.master_tempo,
        key_shift_semitones: spec.key_shift_semitones,
    };
    Ok((out, receipt))
}

fn read_f32_wav(path: &Path) -> Result<(u32, Vec<f32>), String> {
    let mut f = File::open(path).map_err(|e| format!("cannot open {}: {e}", path.display()))?;
    let mut head = [0u8; 44];
    f.read_exact(&mut head)
        .map_err(|e| format!("cannot read WAV header from {}: {e}", path.display()))?;
    if &head[0..4] != b"RIFF" || &head[8..12] != b"WAVE" {
        return Err(format!("{} is not a RIFF WAVE file", path.display()));
    }
    let channels = u16::from_le_bytes([head[22], head[23]]);
    let sample_rate = u32::from_le_bytes([head[24], head[25], head[26], head[27]]);
    let bits = u16::from_le_bytes([head[34], head[35]]);
    if bits != 32 {
        return Err(format!(
            "{} is {bits}-bit PCM; track-render needs 32-bit float WAV",
            path.display()
        ));
    }
    let data_len = u32::from_le_bytes([head[40], head[41], head[42], head[43]]) as usize;
    let mut raw = vec![0u8; data_len];
    f.read_exact(&mut raw)
        .map_err(|e| format!("cannot read WAV data from {}: {e}", path.display()))?;
    let mut pcm = Vec::with_capacity(raw.len() / 4);
    for chunk in raw.chunks_exact(4) {
        pcm.push(f32::from_le_bytes([chunk[0], chunk[1], chunk[2], chunk[3]]));
    }
    let pcm = if channels == 1 {
        let mut stereo = Vec::with_capacity(pcm.len() * 2);
        for sample in pcm {
            stereo.push(sample);
            stereo.push(sample);
        }
        stereo
    } else if channels == 2 {
        pcm
    } else {
        return Err(format!(
            "{} has {channels} channels; track-render supports mono or stereo only",
            path.display()
        ));
    };
    Ok((sample_rate, pcm))
}

fn write_f32_wav(path: &Path, sample_rate: u32, pcm: &[f32]) -> Result<(), String> {
    let f = File::create(path).map_err(|e| format!("cannot create {}: {e}", path.display()))?;
    let mut w = BufWriter::new(f);
    crate::wav::write_f32(&mut w, sample_rate, pcm).map_err(|e| format!("cannot write WAV: {e}"))?;
    w.flush()
        .map_err(|e| format!("cannot flush {}: {e}", path.display()))
}

fn paths_same_file(a: &Path, b: &Path) -> bool {
    if a == b {
        return true;
    }
    if let (Ok(ma), Ok(mb)) = (a.canonicalize(), b.canonicalize()) {
        return ma == mb;
    }
    a.exists() && b.exists() && same_file::is_same_file(a, b).unwrap_or(false)
}

/// `odj-audio track-render` implementation: read a float WAV, stretch, write OUT.
pub fn track_render_command(mut args: Vec<String>) -> Result<(), String> {
    let take = |args: &mut Vec<String>, flag: &str| -> Result<Option<String>, String> {
        match args.iter().position(|a| a == flag) {
            None => Ok(None),
            Some(i) => {
                if i + 1 >= args.len() {
                    return Err(format!("{flag} needs a value"));
                }
                let v = args.remove(i + 1);
                args.remove(i);
                Ok(Some(v))
            }
        }
    };
    let flag = |args: &mut Vec<String>, name: &str| -> bool {
        match args.iter().position(|a| a == name) {
            None => false,
            Some(i) => {
                args.remove(i);
                true
            }
        }
    };
    let inp = take(&mut args, "--in")?.ok_or_else(|| "--in is required".to_string())?;
    let out = take(&mut args, "--out")?.ok_or_else(|| "--out is required".to_string())?;
    let tempo = take(&mut args, "--tempo-ratio")?
        .map(|s| s.parse::<f64>())
        .transpose()
        .map_err(|_| "--tempo-ratio must be a number".to_string())?
        .unwrap_or(1.0);
    let key_shift = take(&mut args, "--key-shift")?
        .map(|s| s.parse::<i32>())
        .transpose()
        .map_err(|_| "--key-shift must be an integer".to_string())?
        .unwrap_or(0);
    let master_tempo = flag(&mut args, "--master-tempo");
    let key_lock = flag(&mut args, "--key-lock");
    let source_hz = take(&mut args, "--source-hz")?
        .map(|s| s.parse::<f64>())
        .transpose()
        .map_err(|_| "--source-hz must be a number".to_string())?;
    if !args.is_empty() {
        return Err(format!("unexpected argument {}", args[0]));
    }
    let inp = PathBuf::from(&inp);
    let out = PathBuf::from(&out);
    if paths_same_file(&inp, &out) {
        return Err("output path is the same file as the source".into());
    }
    if out.exists() {
        return Err(format!("{} already exists", out.display()));
    }
    let (sample_rate, pcm) = read_f32_wav(&inp)?;
    let frames = pcm.len() / 2;
    let spec = TrackRenderSpec {
        tempo_ratio: tempo,
        master_tempo: master_tempo || key_lock,
        key_shift_semitones: key_shift,
        key_lock,
    };
    let (rendered, receipt) = render_interleaved(&pcm, frames, sample_rate, &spec, source_hz)?;
    let parent = out
        .parent()
        .filter(|p| !p.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent).map_err(|e| format!("cannot create {}: {e}", parent.display()))?;
    let staging = parent.join(format!(
        ".odj-track-render-{}-{}.wav",
        std::process::id(),
        std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .map(|d| d.as_nanos())
            .unwrap_or(0)
    ));
    if staging.exists() {
        return Err(format!(
            "staging file {} already exists; retry",
            staging.display()
        ));
    }
    if paths_same_file(&inp, &staging) {
        return Err("staging path collides with input".into());
    }
    write_f32_wav(&staging, sample_rate, &rendered)?;
    std::fs::rename(&staging, &out).map_err(|e| {
        let _ = std::fs::remove_file(&staging);
        format!("cannot rename {} to {}: {e}", staging.display(), out.display())
    })?;
    let line = serde_json::to_string(&receipt).map_err(|e| e.to_string())?;
    println!("{line}");
    Ok(())
}

#[cfg(test)]
mod offline_tests {
    use super::*;

    fn tone_pcm(sr: u32, hz: f64, secs: f64, amp: f32) -> Vec<f32> {
        let frames = (sr as f64 * secs) as usize;
        let mut pcm = Vec::with_capacity(frames * 2);
        for i in 0..frames {
            let t = i as f64 / sr as f64;
            let v = f64::from(amp) * (2.0 * std::f64::consts::PI * hz * t).sin();
            pcm.push(v as f32);
            pcm.push(v as f32);
        }
        pcm
    }

    #[test]
    fn key_locked_tempo_change_uses_stretch_not_varispeed() {
        let sr = 48000u32;
        let pcm = tone_pcm(sr, 440.0, 2.0, 0.5);
        let frames = pcm.len() / 2;
        let spec = TrackRenderSpec {
            tempo_ratio: 128.0 / 120.0,
            master_tempo: true,
            key_shift_semitones: 0,
            key_lock: true,
        };
        let (_, receipt) =
            render_interleaved(&pcm, frames, sr, &spec, Some(440.0)).expect("render");
        assert_eq!(receipt.engine, "stretch");
        assert!(!receipt.varispeed_fallback);
        let beat_s = 60.0 / 128.0;
        assert!(
            (receipt.measured_duration_s - (2.0 * 120.0 / 128.0)).abs() <= beat_s,
            "duration {} vs expected",
            receipt.measured_duration_s
        );
    }

    #[test]
    fn semitone_shift_receipt_includes_shifts() {
        let sr = 48000u32;
        let pcm = tone_pcm(sr, 440.0, 2.5, 0.5);
        let frames = pcm.len() / 2;
        let spec = TrackRenderSpec {
            tempo_ratio: 1.0,
            master_tempo: true,
            key_shift_semitones: 2,
            key_lock: false,
        };
        let (_, receipt) =
            render_interleaved(&pcm, frames, sr, &spec, Some(440.0)).expect("render");
        assert!((receipt.requested_semitones - 2.0).abs() < 0.05);
        let measured = receipt.measured_semitones.expect("measured shift");
        assert!((measured - 2.0).abs() < 0.4);
    }

    #[test]
    fn unavailable_stretch_fails_instead_of_varispeed() {
        let sr = 48000u32;
        let pcm = tone_pcm(sr, 440.0, 0.5, 0.5);
        let frames = pcm.len() / 2;
        let spec = TrackRenderSpec {
            tempo_ratio: 1.05,
            master_tempo: true,
            key_shift_semitones: 0,
            key_lock: true,
        };
        std::env::set_var("ODJ_STRETCH_FORCE_UNAVAILABLE", "1");
        let err = render_interleaved(&pcm, frames, sr, &spec, Some(440.0)).unwrap_err();
        std::env::remove_var("ODJ_STRETCH_FORCE_UNAVAILABLE");
        assert!(err.contains("unavailable"));
    }
}
