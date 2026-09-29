//! Device output through cpal (feature `device`). The device callback is the
//! audio thread: it drains the mailbox, renders, and maps stereo onto however
//! many channels the device has (outputs past 2 are silent until the cue bus
//! lands in plan 20-06).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

use crate::serve::AudioSide;

/// Sample rate and channel count of the default output device.
pub fn default_output_format() -> Result<(u32, u16), String> {
    let device = cpal::default_host()
        .default_output_device()
        .ok_or_else(|| "no default output device".to_string())?;
    let config = device.default_output_config().map_err(|e| e.to_string())?;
    if config.sample_format() != cpal::SampleFormat::F32 {
        return Err(format!(
            "the default output device wants {:?} samples; this build drives f32 devices only",
            config.sample_format()
        ));
    }
    Ok((config.sample_rate(), config.channels()))
}

/// Refuse a device opened at another rate than the engine was built for:
/// the default output (or its format) can change between the probe that set
/// the engine's rate and the stream being opened, and an engine rendering at
/// one rate into a stream at another plays at the wrong pitch and speed, with
/// state timed at neither.
pub fn check_opened_rate(opened: u32, engine: u32) -> Result<(), String> {
    if opened == engine {
        return Ok(());
    }
    Err(format!(
        "the default output device changed to {opened} Hz while the engine started at {engine} Hz; start the engine again"
    ))
}

/// Run the audio side inside the default output device's callback until
/// `stop` is set, refusing a device that now runs at another rate than
/// `engine_rate`. The stream is created on this thread because cpal streams
/// are not `Send` on every platform.
pub fn run_device(engine_rate: u32) -> impl FnOnce(AudioSide, Arc<AtomicBool>) + Send + 'static {
    move |mut side: AudioSide, stop: Arc<AtomicBool>| {
        let device = match cpal::default_host().default_output_device() {
            Some(d) => d,
            None => {
                eprintln!("odj-audio: no default output device");
                return;
            }
        };
        let config: cpal::StreamConfig = match device.default_output_config() {
            Ok(c) => c.into(),
            Err(e) => {
                eprintln!("odj-audio: {e}");
                return;
            }
        };
        if let Err(e) = check_opened_rate(config.sample_rate, engine_rate) {
            eprintln!("odj-audio: {e}");
            return;
        }
        let channels = config.channels as usize;
        let sr = config.sample_rate as u64;
        // A stream error (the device went away) ends the audio side, which
        // ends serve with an error instead of acknowledging commands for an
        // engine that no longer makes sound.
        let failed = Arc::new(AtomicBool::new(false));
        let failed_cb = failed.clone();
        let stream = device.build_output_stream(
            config,
            move |data: &mut [f32], info: &cpal::OutputCallbackInfo| {
                let frames = data.len() / channels;
                // This buffer starts playing `latency` after now; state is
                // stamped with when its position is heard, not rendered.
                let ts = info.timestamp();
                let latency = ts.playback.duration_since(ts.callback).as_nanos() as u64;
                let mut done = 0;
                while done < frames {
                    side.set_ahead_ns(latency + done as u64 * 1_000_000_000 / sr);
                    let buf = side.process(frames - done);
                    let got = buf.len() / 2;
                    for f in 0..got {
                        let o = &mut data[(done + f) * channels..(done + f + 1) * channels];
                        o[0] = buf[2 * f];
                        if channels > 1 {
                            o[1] = buf[2 * f + 1];
                        }
                        for x in o.iter_mut().skip(2) {
                            *x = 0.0;
                        }
                    }
                    done += got;
                }
            },
            move |err| {
                eprintln!("odj-audio: stream error: {err}");
                failed_cb.store(true, Ordering::Relaxed);
            },
            None,
        );
        let stream = match stream {
            Ok(s) => s,
            Err(e) => {
                eprintln!("odj-audio: cannot open the output stream: {e}");
                return;
            }
        };
        if let Err(e) = stream.play() {
            eprintln!("odj-audio: cannot start the output stream: {e}");
            return;
        }
        while !stop.load(Ordering::Relaxed) {
            if failed.load(Ordering::Relaxed) {
                return;
            }
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_device_opened_at_another_rate_than_the_engine_is_refused() {
        // Codex on b87a0f49: the rate was probed once for the engine and the
        // device looked up again to open the stream.
        let err = check_opened_rate(44100, 48000).unwrap_err();
        assert!(err.contains("44100 Hz") && err.contains("48000 Hz"), "{err}");
        assert!(check_opened_rate(48000, 44100).is_err());
        // Control: the rate the engine was built for is accepted.
        assert!(check_opened_rate(48000, 48000).is_ok());
    }
}
