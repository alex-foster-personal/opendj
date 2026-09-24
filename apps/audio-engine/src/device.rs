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

/// Run the audio side inside the default output device's callback until
/// `stop` is set. The stream is created on this thread because cpal streams
/// are not `Send` on every platform.
pub fn run_device() -> impl FnOnce(AudioSide, Arc<AtomicBool>) + Send + 'static {
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
        let channels = config.channels as usize;
        let stream = device.build_output_stream(
            config,
            move |data: &mut [f32], _: &cpal::OutputCallbackInfo| {
                let frames = data.len() / channels;
                let mut done = 0;
                while done < frames {
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
            |err| eprintln!("odj-audio: stream error: {err}"),
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
            std::thread::sleep(Duration::from_millis(20));
        }
    }
}
