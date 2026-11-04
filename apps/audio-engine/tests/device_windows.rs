//! The engine on a real Windows output device (feature `device`: WASAPI
//! shared mode through cpal).
//!
//! Every test here needs a default output device and is `#[ignore]`d, so a
//! build host without a sound card does not report a verdict it could not
//! measure. Run them where there is one:
//!
//!   cargo test --features device --test device_windows -- --ignored --nocapture --test-threads 1
//!
//! One at a time: each opens the default output, and two engines on one
//! endpoint would hear each other on its loopback. On a machine with no
//! output device they fail ("no default output device"); they never pass
//! without having heard or measured the thing they name.
//!
//! `ODJ_AUDIO_TEST_TRACK=<file>` makes the loopback test play that file
//! instead of its own quiet 440 Hz tone, to prove a real track decodes and
//! reaches the endpoint.

#![cfg(all(windows, feature = "device"))]

mod common;

use std::io::{BufRead, BufReader, Lines, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};
use serde_json::{json, Value};

use common::*;

const BIN: &str = env!("CARGO_BIN_EXE_odj-audio");

/// A running `odj-audio serve`, killed on drop so a failed assertion never
/// leaves an engine holding the output device.
struct Served {
    child: Child,
    stdin: ChildStdin,
    lines: Lines<BufReader<ChildStdout>>,
    hello: Value,
}

impl Served {
    fn start(clock: &str, cwd: &Path) -> Served {
        let mut child = Command::new(BIN)
            .args(["serve", "--clock", clock])
            .current_dir(cwd)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take().unwrap();
        let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
        let first = lines.next().unwrap_or_else(|| panic!("odj-audio serve --clock {clock} ended before its hello; its stderr says why")).unwrap();
        let hello: Value = serde_json::from_str(&first).unwrap();
        assert_eq!(hello["type"], "hello", "{hello}");
        Served { child, stdin, lines, hello }
    }

    fn say(&mut self, v: Value) {
        writeln!(self.stdin, "{v}").unwrap();
    }

    /// The next line `want` accepts, read within `within`. States arrive 30
    /// times a second, so a read never blocks for long on a live engine.
    fn until(&mut self, within: Duration, what: &str, mut want: impl FnMut(&Value) -> bool) -> Value {
        let deadline = Instant::now() + within;
        while Instant::now() < deadline {
            let line = self.lines.next().unwrap_or_else(|| panic!("the engine ended while waiting for {what}")).unwrap();
            let v: Value = serde_json::from_str(&line).unwrap();
            if want(&v) {
                return v;
            }
        }
        panic!("no {what} within {within:?}");
    }

    fn ok(&mut self, id: &str) {
        let r = self.until(Duration::from_secs(30), &format!("result of {id}"), |v| v["type"] == "result" && v["id"] == id);
        assert_eq!(r["ok"], true, "{r}");
    }

    fn state(&mut self) -> Value {
        self.until(Duration::from_secs(5), "state", |v| v["type"] == "state")
    }

    fn stop(mut self) {
        self.say(json!({"id": "bye", "cmd": {"type": "engine_shutdown"}}));
        let deadline = Instant::now() + Duration::from_secs(10);
        while self.child.try_wait().unwrap().is_none() {
            assert!(Instant::now() < deadline, "the engine did not exit on engine_shutdown");
            // Keep its stdout drained, or its last lines block it.
            let _ = self.lines.next();
        }
        assert!(self.child.wait().unwrap().success());
    }
}

impl Drop for Served {
    fn drop(&mut self) {
        if let Ok(None) = self.child.try_wait() {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }
}

/// The kernel base priority (0..31) of every thread of `pid`. A thread of a
/// normal-class process sits at 8; 15 is time-critical within that class,
/// and 16..31 is the real-time range MMCSS moves an audio thread into.
fn thread_base_priorities(pid: u32) -> Vec<i32> {
    use windows_sys::Win32::Foundation::{CloseHandle, INVALID_HANDLE_VALUE};
    use windows_sys::Win32::System::Diagnostics::ToolHelp::{
        CreateToolhelp32Snapshot, Thread32First, Thread32Next, TH32CS_SNAPTHREAD, THREADENTRY32,
    };
    let mut found = Vec::new();
    // SAFETY: the snapshot handle is valid until closed below, and the entry
    // is a THREADENTRY32 whose dwSize is set as the API requires.
    unsafe {
        let snap = CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0);
        assert!(snap != INVALID_HANDLE_VALUE, "no thread snapshot: {}", std::io::Error::last_os_error());
        let mut entry: THREADENTRY32 = std::mem::zeroed();
        entry.dwSize = std::mem::size_of::<THREADENTRY32>() as u32;
        let mut more = Thread32First(snap, &mut entry);
        while more != 0 {
            if entry.th32OwnerProcessID == pid {
                found.push(entry.tpBasePri);
            }
            more = Thread32Next(snap, &mut entry);
        }
        CloseHandle(snap);
    }
    found
}

#[test]
#[ignore = "needs a real output device; run with --ignored on a machine that has one"]
fn the_device_callback_thread_runs_above_every_normal_thread() {
    // WASAPI hands cpal a plain thread. Left at normal priority, the audio
    // callback competes with every UI and decode thread on the machine, and
    // a busy desktop is heard as dropouts.
    let d = temp_dir("device-priority");
    // Control: the probe reads this process family's threads, and an engine
    // with no device thread has none above normal. Without it, a high
    // priority found below could belong to something every engine has.
    let mut wall = Served::start("wall", &d);
    wall.state();
    let wall_threads = thread_base_priorities(wall.child.id());
    assert!(wall_threads.len() >= 2, "the probe saw {wall_threads:?} for a running engine");
    let wall_top = *wall_threads.iter().max().unwrap();
    assert!(wall_top < 15, "a wall-clock engine already has a thread at {wall_top}: {wall_threads:?}");
    wall.stop();

    let mut dev = Served::start("device", &d);
    // The stream is running once frames have been rendered into it.
    dev.until(Duration::from_secs(10), "a state with frames rendered", |v| v["type"] == "state" && v["frame"].as_u64().is_some_and(|f| f > 0));
    let threads = thread_base_priorities(dev.child.id());
    let top = *threads.iter().max().expect("the probe saw no threads for a running engine");
    eprintln!("device engine thread base priorities: {threads:?}");
    assert!(top >= 15, "no thread of the device engine is above normal priority (highest {top} of {threads:?}); the audio callback is not real-time");
    dev.stop();
}

#[test]
#[ignore = "needs a real output device; run with --ignored on a machine that has one"]
fn the_default_output_delivers_callbacks_and_reports_what_it_is() {
    // A measurement more than a check: what the engine's stream config
    // (the device default, `BufferSize::Default`) gets on this machine.
    let host = cpal::default_host();
    let device = host.default_output_device().expect("no default output device");
    let name = device.description().map(|d| d.name().to_string()).unwrap_or_else(|e| format!("(no description: {e})"));
    let supported = device.default_output_config().expect("the default output has no default config");
    eprintln!("host: {:?}", host.id());
    eprintln!("device: {name}");
    eprintln!(
        "default config: {} Hz, {} channels, {:?}, buffer {:?}",
        supported.sample_rate(),
        supported.channels(),
        supported.sample_format(),
        supported.buffer_size()
    );
    assert_eq!(supported.sample_format(), cpal::SampleFormat::F32, "the engine drives f32 devices only");
    let rate = supported.sample_rate() as u64;
    let config: cpal::StreamConfig = supported.into();
    let channels = config.channels as usize;
    // (frames in the callback, how far ahead of the callback they play).
    let seen: Arc<Mutex<Vec<(usize, u64)>>> = Arc::new(Mutex::new(Vec::with_capacity(4096)));
    let sink = seen.clone();
    let errors: Arc<Mutex<Vec<String>>> = Arc::default();
    let errs = errors.clone();
    let stream = device
        .build_output_stream(
            config,
            move |data: &mut [f32], info: &cpal::OutputCallbackInfo| {
                data.fill(0.0);
                let ts = info.timestamp();
                let ahead = ts.playback.duration_since(ts.callback).as_micros() as u64;
                if let Ok(mut s) = sink.try_lock() {
                    if s.len() < s.capacity() {
                        s.push((data.len() / channels, ahead));
                    }
                }
            },
            move |e| errs.lock().unwrap().push(e.to_string()),
            None,
        )
        .expect("the default output would not open");
    stream.play().unwrap();
    let ran = Duration::from_secs(2);
    std::thread::sleep(ran);
    drop(stream);
    let seen = seen.lock().unwrap().clone();
    assert!(errors.lock().unwrap().is_empty(), "stream errors: {:?}", errors.lock().unwrap());
    assert!(!seen.is_empty(), "the device called back zero times in {ran:?}");
    let mut sizes: Vec<usize> = seen.iter().map(|s| s.0).collect();
    sizes.sort_unstable();
    let mut ahead: Vec<u64> = seen.iter().map(|s| s.1).collect();
    ahead.sort_unstable();
    let frames: usize = sizes.iter().sum();
    eprintln!(
        "callbacks: {} in {ran:?}, frames per callback min {} median {} max {} ({:.2} ms at the median)",
        seen.len(),
        sizes[0],
        sizes[sizes.len() / 2],
        sizes[sizes.len() - 1],
        sizes[sizes.len() / 2] as f64 * 1000.0 / rate as f64
    );
    eprintln!("output latency reported by the stream: min {} us, median {} us, max {} us", ahead[0], ahead[ahead.len() / 2], ahead[ahead.len() - 1]);
    eprintln!("frames delivered: {frames} ({:.1}% of {rate} Hz over {ran:?})", frames as f64 * 100.0 / (rate as f64 * ran.as_secs_f64()));
    // The device pulls at its own rate: within a tenth of it, start-up aside.
    let expected = rate as f64 * ran.as_secs_f64();
    assert!((frames as f64) > expected * 0.8 && (frames as f64) < expected * 1.2, "{frames} frames in {ran:?} at {rate} Hz");
}

/// The amplitude of the `hz` component of `mono` (0 for none, `a` for a sine
/// of amplitude `a`), by correlation over the whole buffer.
fn tone_amplitude(mono: &[f32], rate: u32, hz: f64) -> f64 {
    if mono.is_empty() {
        return 0.0;
    }
    let w = 2.0 * std::f64::consts::PI * hz / rate as f64;
    let (mut re, mut im) = (0.0f64, 0.0f64);
    for (i, &x) in mono.iter().enumerate() {
        re += x as f64 * (w * i as f64).cos();
        im += x as f64 * (w * i as f64).sin();
    }
    2.0 * (re * re + im * im).sqrt() / mono.len() as f64
}

fn rms(mono: &[f32]) -> f64 {
    if mono.is_empty() {
        return 0.0;
    }
    (mono.iter().map(|&x| x as f64 * x as f64).sum::<f64>() / mono.len() as f64).sqrt()
}

#[test]
#[ignore = "needs a real output device, and plays a quiet tone on it; run with --ignored on a machine that has one"]
fn what_a_deck_plays_is_heard_on_the_devices_loopback() {
    // The engine reports its own frame counter and deck position, which say
    // it rendered. Whether the samples reached the endpoint is a question
    // for another instrument: the endpoint's own loopback, which captures
    // what Windows mixes for that device.
    let host = cpal::default_host();
    let device = host.default_output_device().expect("no default output device");
    let supported = device.default_output_config().expect("the default output has no default config");
    let rate = supported.sample_rate();
    let config: cpal::StreamConfig = supported.into();
    let channels = config.channels as usize;
    let heard: Arc<Mutex<Vec<f32>>> = Arc::default();
    let sink = heard.clone();
    // An input stream on an output device is WASAPI loopback capture.
    let loopback = device
        .build_input_stream(
            config,
            move |data: &[f32], _: &cpal::InputCallbackInfo| {
                let mut h = sink.lock().unwrap();
                h.extend(data.chunks(channels).map(|f| f[0]));
            },
            |e| eprintln!("loopback error: {e}"),
            None,
        )
        .expect("the default output's loopback would not open");
    loopback.play().unwrap();
    // What the loopback captures over `for_ms` of the engine's own clock,
    // with the first and last state of that window. The engine's output is
    // read all the while, as a supervisor reads it: left unread, the pipe
    // fills and the engine stops writing state.
    let listen = |e: &mut Served, for_ms: u64, what: &str, mut from: Box<dyn FnMut(&Value) -> bool>| -> (Vec<f32>, Value, Value) {
        let first = e.until(Duration::from_secs(10), what, |v| v["type"] == "state" && from(v));
        heard.lock().unwrap().clear();
        let end_ns = first["sent_ns"].as_u64().unwrap() + for_ms * 1_000_000;
        let last = e.until(Duration::from_secs(10), "the state that ends the listening window", |v| {
            v["type"] == "state" && v["sent_ns"].as_u64().is_some_and(|s| s >= end_ns)
        });
        (std::mem::take(&mut *heard.lock().unwrap()), first, last)
    };

    let d = temp_dir("device-loopback");
    let track = std::env::var_os("ODJ_AUDIO_TEST_TRACK").map(std::path::PathBuf::from);
    let hz = 440.0;
    let path = match &track {
        Some(p) => p.clone(),
        None => {
            // Quiet on purpose: this plays on someone's speakers. 0.05 is
            // 26 dB under full scale.
            let frames = rate as usize * 10;
            let pcm: Vec<f32> = (0..frames)
                .flat_map(|i| {
                    let v = (2.0 * std::f64::consts::PI * hz * i as f64 / rate as f64).sin() as f32 * 0.05;
                    [v, v]
                })
                .collect();
            write_wav(&d, "tone.wav", rate, &pcm)
        }
    };
    let mut e = Served::start("device", &d);
    assert_eq!(e.hello["sample_rate"], rate, "the engine and the probe opened the device at different rates");
    if track.is_some() {
        e.say(json!({"id": "v", "cmd": {"type": "master_volume", "value": 0.3}}));
        e.ok("v");
    }
    e.say(json!({"id": "l", "cmd": {"type": "load", "deck": 1, "path": path}}));
    e.ok("l");
    // Control: the engine is rendering (silence) and the loopback hears none
    // of what it is about to play. Loopback delivers frames only while
    // something renders to the endpoint, which the engine's open stream does.
    let (before, _, _) = listen(&mut e, 700, "a state", Box::new(|_| true));
    let (tone_before, rms_before) = (tone_amplitude(&before, rate, hz), rms(&before));

    e.say(json!({"id": "p", "cmd": {"type": "play", "deck": 1, "playing": true}}));
    e.ok("p");
    // From 300 ms in, so the window holds none of the start.
    let moving = |v: &Value| v["decks"][0]["playing"] == true && v["decks"][0]["position_ms"].as_f64().is_some_and(|p| p > 300.0);
    let (during, first, last) = listen(&mut e, 1500, "a state with deck 1 moving", Box::new(moving));
    let (tone_during, rms_during) = (tone_amplitude(&during, rate, hz), rms(&during));
    e.say(json!({"id": "s", "cmd": {"type": "play", "deck": 1, "playing": false}}));
    e.ok("s");
    let heard_in_ms = last["heard_in_ns"].as_i64().unwrap() as f64 / 1e6;
    let frames = last["frame"].as_u64().unwrap() - first["frame"].as_u64().unwrap();
    let engine_s = (last["sent_ns"].as_u64().unwrap() - first["sent_ns"].as_u64().unwrap()) as f64 / 1e9;
    let moved_ms = last["decks"][0]["position_ms"].as_f64().unwrap() - first["decks"][0]["position_ms"].as_f64().unwrap();
    eprintln!("played: {}", path.display());
    eprintln!("loopback before play: {} frames, rms {rms_before:.6}, {hz} Hz amplitude {tone_before:.6}", before.len());
    eprintln!("loopback during play: {} frames, rms {rms_during:.6}, {hz} Hz amplitude {tone_during:.6}", during.len());
    eprintln!("engine: {frames} frames in {engine_s:.3} s ({:.1} Hz), deck 1 moved {moved_ms:.1} ms, heard_in {heard_in_ms:.2} ms", frames as f64 / engine_s);
    e.stop();
    drop(loopback);

    // The loopback instrument itself delivered: about the device rate.
    assert!(during.len() as f64 > rate as f64 * 1.5 * 0.8, "the loopback captured only {} frames in 1.5 s at {rate} Hz", during.len());
    // The device pulled frames from the engine at its own rate, and the deck
    // moved with them.
    let pulled = frames as f64 / engine_s;
    assert!(pulled > rate as f64 * 0.9 && pulled < rate as f64 * 1.1, "the device pulled {pulled:.0} frames a second from an engine at {rate} Hz");
    assert!((moved_ms - engine_s * 1000.0).abs() < 100.0, "deck 1 moved {moved_ms:.1} ms in {:.1} ms", engine_s * 1000.0);
    if track.is_some() {
        assert!(rms_during > 1e-4, "the endpoint heard rms {rms_during:.6} while the deck played");
        assert!(rms_during > 10.0 * rms_before, "rms {rms_during:.6} while playing, {rms_before:.6} before");
    } else {
        assert!(tone_during > 0.005, "the endpoint heard a {hz} Hz amplitude of {tone_during:.6} while the deck played it at 0.05");
        assert!(tone_during > 20.0 * tone_before, "{hz} Hz amplitude {tone_during:.6} while playing, {tone_before:.6} before");
    }
}
