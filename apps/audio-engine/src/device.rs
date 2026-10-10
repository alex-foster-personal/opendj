//! Device output through cpal (feature `device`). The device callback is the
//! audio thread: it drains the mailbox, renders, and maps stereo onto however
//! many channels the device has (outputs past 2 are silent until the cue bus
//! lands in plan 20-06).

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use cpal::traits::{DeviceTrait, HostTrait, StreamTrait};

use crate::serve::AudioSide;

/// The default output device as probed at start: which device it is, and
/// the rate the engine is built for. The stream opens this device, not
/// whatever is the default by then.
pub struct Probed {
    pub id: cpal::DeviceId,
    pub rate: u32,
    pub channels: u16,
}

/// Probe the default output device.
pub fn default_output() -> Result<Probed, String> {
    let device = cpal::default_host()
        .default_output_device()
        .ok_or_else(|| "no default output device".to_string())?;
    let id = device.id().map_err(|e| format!("cannot identify the default output device: {e}"))?;
    let config = device.default_output_config().map_err(|e| e.to_string())?;
    if config.sample_format() != cpal::SampleFormat::F32 {
        return Err(format!(
            "the default output device wants {:?} samples; this build drives f32 devices only",
            config.sample_format()
        ));
    }
    Ok(Probed { id, rate: config.sample_rate(), channels: config.channels() })
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

/// What an error on the open output stream means for it.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Fault {
    /// The device missed a buffer (an underrun or overrun): a click or a few
    /// milliseconds of silence, after which the stream plays on.
    Glitch,
    /// The stream delivers no more audio, or may not: the audio side ends,
    /// which ends serve with an error.
    Fatal,
}

/// Sort a stream error by cpal's own kind, never by its message. Only an
/// xrun is a glitch. Every other kind stays fatal, the ones cpal has not
/// named yet included: an output that has gone quiet must not keep
/// acknowledging commands. (`capture` sorts the other way round, since a
/// recording can live with a gap; an engine that makes no sound cannot.)
pub fn classify(kind: cpal::ErrorKind) -> Fault {
    match kind {
        cpal::ErrorKind::Xrun => Fault::Glitch,
        _ => Fault::Fatal,
    }
}

/// The open stream's health, shared between cpal's error callback and the
/// thread that holds the stream open.
#[derive(Debug, Default)]
pub struct StreamHealth {
    xruns: AtomicU64,
    failed: AtomicBool,
}

impl StreamHealth {
    /// cpal's error callback. Some backends call it on the audio thread, so
    /// a glitch costs one atomic add here and is written up by `watch`; a
    /// fatal error is written here, as the stream it would interrupt is over.
    pub fn on_error(&self, err: &cpal::Error) {
        match classify(err.kind()) {
            Fault::Glitch => {
                self.xruns.fetch_add(1, Ordering::Relaxed);
            }
            Fault::Fatal => {
                eprintln!("odj-audio: stream error: {err}");
                self.failed.store(true, Ordering::Relaxed);
            }
        }
    }

    /// Xruns since the stream opened.
    pub fn xruns(&self) -> u64 {
        self.xruns.load(Ordering::Relaxed)
    }

    /// Whether a fatal error has ended the stream.
    pub fn failed(&self) -> bool {
        self.failed.load(Ordering::Relaxed)
    }
}

/// At most one xrun line this often: an overloaded machine can xrun many
/// times a second, and each line reaches the supervisor's log.
pub const XRUN_REPORT_EVERY: Duration = Duration::from_secs(1);

/// Which xruns have been written up, and when.
#[derive(Debug, Default)]
pub struct XrunReport {
    reported: u64,
    last: Option<Instant>,
}

impl XrunReport {
    /// The xruns to write up at `now`, as (new, total): the first at once,
    /// later ones gathered into one report per `every`. `flush` reports
    /// whatever is left regardless, for the stream's end.
    pub fn take(&mut self, total: u64, now: Instant, every: Duration, flush: bool) -> Option<(u64, u64)> {
        let due = match self.last {
            _ if flush => true,
            None => true,
            Some(t) => now.duration_since(t) >= every,
        };
        if total == self.reported || !due {
            return None;
        }
        let new = total - self.reported;
        self.reported = total;
        self.last = Some(now);
        Some((new, total))
    }
}

/// Why `watch` returned.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Ended {
    /// `stop` was set.
    Stopped,
    /// A fatal stream error.
    Failed,
}

/// Hold the stream open until `stop` is set or a fatal error ends it,
/// handing xruns to `report` as (new, total) off the audio thread. An xrun
/// never ends it. `close` closes the stream, and runs before the last count
/// is read: an xrun its callback counts up to that moment is in the last
/// report, and none can come after it.
pub fn watch(
    health: &StreamHealth,
    stop: &AtomicBool,
    poll: Duration,
    every: Duration,
    close: impl FnOnce(),
    mut report: impl FnMut(u64, u64),
) -> Ended {
    let mut xruns = XrunReport::default();
    let ended = loop {
        if health.failed() {
            break Ended::Failed;
        }
        if stop.load(Ordering::Relaxed) {
            break Ended::Stopped;
        }
        if let Some((new, total)) = xruns.take(health.xruns(), Instant::now(), every, false) {
            report(new, total);
        }
        std::thread::sleep(poll);
    };
    close();
    if let Some((new, total)) = xruns.take(health.xruns(), Instant::now(), every, true) {
        report(new, total);
    }
    ended
}

/// Run the audio side inside the callback of the device probed at start
/// until `stop` is set, refusing it if it is gone or now runs at another
/// rate than the engine's. The stream is created on this thread because
/// cpal streams are not `Send` on every platform, so the device is found
/// again here by its id: not as the default, which may be another device
/// by now (speakers where an interface was probed).
pub fn run_device(probed: Probed) -> impl FnOnce(AudioSide, Arc<AtomicBool>) + Send + 'static {
    let Probed { id, rate: engine_rate, .. } = probed;
    move |mut side: AudioSide, stop: Arc<AtomicBool>| {
        let device = match cpal::default_host().device_by_id(&id) {
            Some(d) => d,
            None => {
                eprintln!("odj-audio: the output device probed at start ({id}) is gone");
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
        // A fatal stream error (the device went away) ends the audio side,
        // which ends serve with an error instead of acknowledging commands
        // for an engine that no longer makes sound. An xrun is counted and
        // the stream plays on (`classify`).
        let health = Arc::new(StreamHealth::default());
        let health_cb = health.clone();
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
            move |err| health_cb.on_error(&err),
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
        watch(
            &health,
            &stop,
            Duration::from_millis(20),
            XRUN_REPORT_EVERY,
            move || drop(stream),
            |new, total| eprintln!("odj-audio: output xrun: {new} since the last report, {total} since the stream opened"),
        );
    }
}

#[cfg(test)]
mod tests {
    use std::sync::mpsc;

    use super::*;

    /// `watch` on its own thread over a fast poll; the receiver yields how
    /// it ended and every (new, total) it reported.
    type Watched = (Ended, Vec<(u64, u64)>);

    fn watching(health: &Arc<StreamHealth>, stop: &Arc<AtomicBool>) -> mpsc::Receiver<Watched> {
        let (tx, rx) = mpsc::channel();
        let (health, stop) = (health.clone(), stop.clone());
        std::thread::spawn(move || {
            let mut reports = Vec::new();
            let ended =
                watch(&health, &stop, Duration::from_millis(1), Duration::ZERO, || {}, |n, t| reports.push((n, t)));
            let _ = tx.send((ended, reports));
        });
        rx
    }

    #[test]
    fn an_xrun_is_counted_and_the_stream_plays_on() {
        // Silver, Fri 9 Oct 2026: four exits on "A buffer underrun or
        // overrun occurred" under load, each costing every deck its audio.
        let (health, stop) = (Arc::new(StreamHealth::default()), Arc::new(AtomicBool::new(false)));
        let rx = watching(&health, &stop);
        health.on_error(&cpal::Error::new(cpal::ErrorKind::Xrun));
        health.on_error(&cpal::ErrorKind::Xrun.into());
        // Sorted by kind, not by text: a backend's own wording, even one
        // that reads like a lost device, is still an xrun.
        health.on_error(&cpal::Error::with_message(cpal::ErrorKind::Xrun, "device not available"));
        assert_eq!(health.xruns(), 3);
        assert!(!health.failed(), "an xrun was taken for a lost device");
        // Nothing but `stop` ends it: many polls later it is still held.
        assert!(
            rx.recv_timeout(Duration::from_millis(200)).is_err(),
            "the stream was given up after an xrun"
        );
        stop.store(true, Ordering::Relaxed);
        let (ended, reports) = rx.recv_timeout(Duration::from_secs(10)).expect("watch did not stop when asked");
        assert_eq!(ended, Ended::Stopped);
        // Every xrun is reported once, and the last report carries the total.
        assert_eq!(reports.iter().map(|r| r.0).sum::<u64>(), 3, "{reports:?}");
        assert_eq!(reports.last().map(|r| r.1), Some(3), "{reports:?}");
    }

    #[test]
    fn a_lost_device_still_ends_the_stream() {
        for kind in [cpal::ErrorKind::DeviceNotAvailable, cpal::ErrorKind::StreamInvalidated] {
            let (health, stop) = (Arc::new(StreamHealth::default()), Arc::new(AtomicBool::new(false)));
            let rx = watching(&health, &stop);
            health.on_error(&cpal::Error::new(kind));
            // `stop` is never set: the error alone ends it.
            let got = rx.recv_timeout(Duration::from_secs(10));
            stop.store(true, Ordering::Relaxed);
            let (ended, reports) = got.unwrap_or_else(|_| panic!("{kind:?} left the stream held open"));
            assert_eq!(ended, Ended::Failed, "{kind:?}");
            assert!(health.failed(), "{kind:?}");
            assert_eq!((health.xruns(), reports.len()), (0, 0), "{kind:?} was counted as an xrun");
        }
    }

    #[test]
    fn a_lost_device_ends_the_stream_whatever_xruns_came_before() {
        let (health, stop) = (Arc::new(StreamHealth::default()), Arc::new(AtomicBool::new(false)));
        health.on_error(&cpal::ErrorKind::Xrun.into());
        health.on_error(&cpal::ErrorKind::DeviceNotAvailable.into());
        health.on_error(&cpal::ErrorKind::Xrun.into());
        let (ended, reports) = watching(&health, &stop).recv_timeout(Duration::from_secs(10)).expect("held open");
        // The xruns are still written up as the stream ends.
        assert_eq!((ended, reports), (Ended::Failed, vec![(2, 2)]));
    }

    #[test]
    fn the_last_count_is_read_after_the_stream_is_closed() {
        // Sol on a0504f33: the last count was read while the stream was
        // still open, so an xrun counted between that read and the close was
        // never reported. Here the callback fires as late as it can, as the
        // stream closes, on both ways out.
        for fatal in [false, true] {
            let (health, stop) = (StreamHealth::default(), AtomicBool::new(!fatal));
            health.on_error(&cpal::ErrorKind::Xrun.into());
            if fatal {
                health.on_error(&cpal::ErrorKind::DeviceNotAvailable.into());
            }
            let mut reports = Vec::new();
            let late = || health.on_error(&cpal::ErrorKind::Xrun.into());
            let ended = watch(&health, &stop, Duration::from_millis(1), Duration::ZERO, late, |n, t| reports.push((n, t)));
            assert_eq!(ended, if fatal { Ended::Failed } else { Ended::Stopped });
            assert_eq!(reports, vec![(2, 2)], "fatal={fatal}: the xrun counted as the stream closed was not reported");
            assert_eq!(health.xruns(), 2);
        }
    }

    #[test]
    fn only_an_xrun_is_a_glitch() {
        use cpal::ErrorKind::*;
        assert_eq!(classify(Xrun), Fault::Glitch);
        // Control for the overshoot: every other kind cpal 0.18 names ends
        // the stream as it did before, the ones that lose the device first.
        for kind in [
            DeviceNotAvailable,
            StreamInvalidated,
            HostUnavailable,
            PermissionDenied,
            DeviceBusy,
            DeviceChanged,
            InvalidInput,
            RealtimeDenied,
            ResourceExhausted,
            UnsupportedConfig,
            UnsupportedOperation,
            BackendError,
            Other,
        ] {
            assert_eq!(classify(kind), Fault::Fatal, "{kind:?}");
        }
    }

    #[test]
    fn xrun_reports_are_gathered_not_written_one_a_line() {
        let (t0, every) = (Instant::now(), Duration::from_secs(1));
        let at = |ms: u64| t0 + Duration::from_millis(ms);
        let mut r = XrunReport::default();
        assert_eq!(r.take(0, at(0), every, false), None, "nothing to report");
        // The first is written at once.
        assert_eq!(r.take(1, at(10), every, false), Some((1, 1)));
        // A burst inside the interval waits, then goes out as one line.
        assert_eq!(r.take(40, at(500), every, false), None);
        assert_eq!(r.take(90, at(1009), every, false), None);
        assert_eq!(r.take(90, at(1010), every, false), Some((89, 90)));
        assert_eq!(r.take(90, at(5000), every, false), None, "reported twice");
        assert_eq!(r.take(92, at(5001), every, false), Some((2, 92)));
        // The stream ending reports what is left, however recent the last.
        assert_eq!(r.take(95, at(5002), every, false), None);
        assert_eq!(r.take(95, at(5002), every, true), Some((3, 95)));
        assert_eq!(r.take(95, at(5003), every, true), None, "nothing left to flush");
    }

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
