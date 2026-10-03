//! Audio input through cpal (feature `device`): the inputs this machine has,
//! and recording one of them to WAV segments (`record::SegmentWriter`).
//!
//! The device callback only converts to f32 and pushes into a lock-free
//! ring; a full ring drops that whole callback buffer and counts it, rather
//! than blocking the audio thread. The writing happens on the calling
//! thread, which also owns the stream (cpal streams are not `Send` on every
//! platform).

use std::path::Path;
use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, SystemTime};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use serde::Serialize;

use crate::record::SegmentWriter;

/// One input as `odj-audio input-devices` lists it. `index` is its place in
/// this listing, which `record --device-index` takes.
#[derive(Debug, Clone, Serialize)]
pub struct InputInfo {
    pub index: usize,
    pub name: String,
    pub channels: u16,
    pub rate: u32,
}

/// Which input to record: by exact name, or by listing index.
pub enum Select {
    Name(String),
    Index(usize),
}

fn inputs() -> Result<Vec<(cpal::Device, String)>, String> {
    let devices = cpal::default_host()
        .input_devices()
        .map_err(|e| format!("cannot list audio inputs: {e}"))?;
    let mut out = Vec::new();
    for d in devices {
        let name = d
            .description()
            .map(|desc| desc.name().to_string())
            .map_err(|e| format!("cannot name an audio input: {e}"))?;
        out.push((d, name));
    }
    Ok(out)
}

/// Every input with its default format, in the host's order.
pub fn input_devices() -> Result<Vec<InputInfo>, String> {
    let mut out = Vec::new();
    for (index, (d, name)) in inputs()?.into_iter().enumerate() {
        let config = d
            .default_input_config()
            .map_err(|e| format!("cannot read the format of audio input {name:?}: {e}"))?;
        out.push(InputInfo { index, name, channels: config.channels(), rate: config.sample_rate() });
    }
    Ok(out)
}

/// The one input `select` names; a name two inputs share is refused, since
/// recording either could record the wrong one.
pub fn pick<T>(found: Vec<(T, String)>, select: &Select) -> Result<(T, String), String> {
    let names: Vec<String> = found.iter().map(|(_, n)| format!("{n:?}")).collect();
    let connected = if names.is_empty() { "none".to_string() } else { names.join(", ") };
    match select {
        Select::Index(i) => found
            .into_iter()
            .nth(*i)
            .ok_or_else(|| format!("no audio input at index {i} (connected inputs: {connected})")),
        Select::Name(want) => {
            let mut hits: Vec<(T, String)> = found.into_iter().filter(|(_, n)| n == want).collect();
            match hits.len() {
                0 => Err(format!("audio input {want:?} is not connected (connected inputs: {connected})")),
                1 => Ok(hits.remove(0)),
                n => Err(format!("{n} audio inputs are named {want:?}; rename one so REC can tell them apart")),
            }
        }
    }
}

/// What `record` opened, reported once the stream is running.
#[derive(Debug, Clone, Serialize)]
pub struct Started {
    pub device: String,
    pub rate: u32,
    pub source_channels: u16,
    pub channels: u16,
}

/// How a recording ended.
#[derive(Debug, Clone, Serialize)]
pub struct Stopped {
    pub segments: Vec<String>,
    pub frames: u64,
    /// Samples the writer could not keep up with (a full ring); 0 normally.
    pub dropped_samples: u64,
}

fn build<T>(
    device: &cpal::Device,
    config: cpal::StreamConfig,
    mut prod: rtrb::Producer<f32>,
    dropped: Arc<AtomicU64>,
    failed: Arc<AtomicBool>,
) -> Result<cpal::Stream, String>
where
    T: cpal::SizedSample,
    f32: cpal::FromSample<T>,
{
    device
        .build_input_stream(
            config,
            move |data: &[T], _: &cpal::InputCallbackInfo| {
                // Whole buffers only, so the ring always holds whole frames.
                match prod.write_chunk_uninit(data.len()) {
                    Ok(chunk) => {
                        chunk.fill_from_iter(data.iter().map(|s| cpal::Sample::to_sample::<f32>(*s)));
                    }
                    Err(_) => {
                        dropped.fetch_add(data.len() as u64, Ordering::Relaxed);
                    }
                }
            },
            move |err| {
                eprintln!("odj-audio: input stream error: {err}");
                failed.store(true, Ordering::Relaxed);
            },
            None,
        )
        .map_err(|e| format!("cannot open the input stream: {e}"))
}

fn drain(cons: &mut rtrb::Consumer<f32>, writer: &mut SegmentWriter, channels: usize) -> Result<(), String> {
    let n = cons.slots() - cons.slots() % channels;
    if n == 0 {
        return Ok(());
    }
    let chunk = cons.read_chunk(n).map_err(|e| e.to_string())?;
    let (a, b) = chunk.as_slices();
    // The ring wraps on a sample boundary that need not be a frame boundary.
    if a.len() % channels == 0 {
        writer.push(a).and_then(|_| writer.push(b)).map_err(|e| e.to_string())?;
    } else {
        let mut joined = Vec::with_capacity(n);
        joined.extend_from_slice(a);
        joined.extend_from_slice(b);
        writer.push(&joined).map_err(|e| e.to_string())?;
    }
    chunk.commit_all();
    Ok(())
}

/// Record `select` into `dir` until `stop` is set, then close the last
/// segment. `on_started` runs once the stream is running. A stream error
/// (the input went away) ends the recording with an error, after closing
/// what was written.
pub fn record(
    select: &Select,
    dir: &Path,
    segment_seconds: u32,
    stop: Arc<AtomicBool>,
    on_started: impl FnOnce(&Started),
) -> Result<Stopped, String> {
    let (device, name) = pick(inputs()?, select)?;
    let supported = device
        .default_input_config()
        .map_err(|e| format!("cannot read the format of audio input {name:?}: {e}"))?;
    let format = supported.sample_format();
    let config: cpal::StreamConfig = supported.into();
    let source_channels = config.channels;
    let rate = config.sample_rate;
    let mut writer = SegmentWriter::new(dir, source_channels, rate, segment_seconds, SystemTime::now)?;
    // Two seconds of audio: the writer drains every 20 ms.
    let (prod, mut cons) = rtrb::RingBuffer::<f32>::new(rate as usize * source_channels as usize * 2);
    let dropped = Arc::new(AtomicU64::new(0));
    let failed = Arc::new(AtomicBool::new(false));
    let (d, f) = (dropped.clone(), failed.clone());
    let stream = match format {
        cpal::SampleFormat::F32 => build::<f32>(&device, config, prod, d, f),
        cpal::SampleFormat::F64 => build::<f64>(&device, config, prod, d, f),
        cpal::SampleFormat::I8 => build::<i8>(&device, config, prod, d, f),
        cpal::SampleFormat::I16 => build::<i16>(&device, config, prod, d, f),
        cpal::SampleFormat::I32 => build::<i32>(&device, config, prod, d, f),
        cpal::SampleFormat::U8 => build::<u8>(&device, config, prod, d, f),
        cpal::SampleFormat::U16 => build::<u16>(&device, config, prod, d, f),
        cpal::SampleFormat::U32 => build::<u32>(&device, config, prod, d, f),
        other => Err(format!("audio input {name:?} delivers {other:?} samples, which this build cannot record")),
    }?;
    stream.play().map_err(|e| format!("cannot start the input stream: {e}"))?;
    let (channels, _) = writer.format();
    on_started(&Started { device: name.clone(), rate, source_channels, channels });
    let ch = source_channels as usize;
    let mut result = Ok(());
    while !stop.load(Ordering::Relaxed) {
        if let Err(e) = drain(&mut cons, &mut writer, ch) {
            result = Err(e);
            break;
        }
        if failed.load(Ordering::Relaxed) {
            result = Err(format!("audio input {name:?} stopped delivering audio (disconnected?)"));
            break;
        }
        std::thread::sleep(Duration::from_millis(20));
    }
    // No more callbacks after this; then write what they left in the ring.
    drop(stream);
    if result.is_ok() {
        result = drain(&mut cons, &mut writer, ch);
    }
    let frames = writer.frames();
    let segments = writer.finish().map_err(|e| e.to_string());
    result?;
    Ok(Stopped { segments: segments?, frames, dropped_samples: dropped.load(Ordering::Relaxed) })
}

#[cfg(test)]
mod tests {
    use super::*;

    fn found() -> Vec<(u8, String)> {
        vec![(0, "MacBook Pro Microphone".into()), (1, "BlackHole 2ch".into()), (2, "Twin".into()), (3, "Twin".into())]
    }

    #[test]
    fn inputs_are_picked_by_exact_name_or_index() {
        assert_eq!(pick(found(), &Select::Name("BlackHole 2ch".into())).unwrap().0, 1);
        assert_eq!(pick(found(), &Select::Index(0)).unwrap().0, 0);
        let gone = pick(found(), &Select::Name("BlackHole".into())).unwrap_err();
        assert!(gone.contains("not connected") && gone.contains("\"BlackHole 2ch\""), "{gone}");
        assert!(pick(found(), &Select::Index(9)).unwrap_err().contains("index 9"));
        let twin = pick(found(), &Select::Name("Twin".into())).unwrap_err();
        assert!(twin.contains("2 audio inputs"), "{twin}");
        assert!(pick(Vec::<(u8, String)>::new(), &Select::Index(0)).unwrap_err().contains("none"));
    }
}
