//! Native headphone cue output for the installed macOS app (CUEOUT-22, IOPIN-05).
//!
//! WHY THIS EXISTS. The Chrome preview plays the cue bus on a second device by
//! pinning a second `AudioContext` with `AudioContext.setSinkId` (CUEOUT-09).
//! The shipped app renders in WKWebView, which has no `setSinkId` at all, so
//! in the installed app every sound the page makes goes to the macOS default
//! output and the headphone cue could never reach a separate device.
//!
//! WHAT IT DOES. The page keeps building the whole monitor graph in Web Audio
//! exactly as it does in Chrome. Only the LAST hop changes: instead of a
//! receiver worklet on a pinned cue context, the existing cue-bridge sender
//! worklet's PCM is relayed by a dedicated web Worker over a loopback
//! WebSocket to this module, which pushes it into a lock-free ring and plays it
//! from a CoreAudio AUHAL output unit bound to the chosen headphone device.
//! The room mix never leaves the webview, so master latency is unchanged.
//!
//! The same socket carries the small control plane the page needs in place of
//! `navigator.mediaDevices`: list output devices, open/close the cue device,
//! and pin MASTER (by making it the macOS default output, which is where the
//! webview plays, and putting it back if macOS moves the default while the pin
//! holds -- the CUEOUT-09 P0 that the room line is never taken over by a
//! headphone plug or a Bluetooth reconnect).
//!
//! THE TWO CLOCKS. The page's AudioContext and the headphone device run on
//! independent clocks, as they do in Chrome's two-context bridge. The ring
//! holds a target fill and corrects drift by dropping or repeating ONE frame
//! per render callback, decided on a smoothed fill so the burstiness of the
//! socket relay does not read as drift. Sample-rate differences are not this
//! module's job: the AUHAL's own converter takes the page's rate on its input
//! scope and renders at the device's rate.
//!
//! The ring, the protocol and the policy are platform-neutral so Linux CI tests
//! them; the CoreAudio half is a macOS seam, and every other platform answers
//! with an honest "unsupported" rather than a fake device list.

use std::collections::hash_map::RandomState;
use std::hash::BuildHasher;
use std::io::ErrorKind;
use std::net::{TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, AtomicUsize, Ordering};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

/// Path the page connects to. Anything else is refused at the handshake.
pub const CUE_SINK_PATH: &str = "/cue";

/// Frames the ring aims to hold. ~42.7 ms at 48 kHz: the same target the
/// in-page bridge uses (`CUE_BRIDGE_TARGET_FRAMES`), which already absorbs two
/// unsynchronized render cadences; the socket relay arrives in small bursts
/// that the smoothed fill below averages out rather than a bigger buffer.
pub const TARGET_FRAMES: usize = 2048;
/// Drift band around the target, on the SMOOTHED fill. Wider than the in-page
/// bridge's one render quantum because the relay delivers 256-frame bursts.
pub const DRIFT_BAND_FRAMES: usize = 384;
/// Ring size: room for the target, the band and a burst of jitter on top.
pub const RING_CAPACITY_FRAMES: usize = TARGET_FRAMES * 4;
/// Smoothing for the fill estimate, per render callback.
const FILL_EMA_ALPHA: f64 = 0.02;

/// How long a blocked read waits before the connection loop does its periodic
/// work (stats, device watch, master pin).
const READ_POLL: Duration = Duration::from_millis(100);
const HOUSEKEEPING_EVERY: Duration = Duration::from_millis(1000);

/// One stereo frame stored as two f32 bit patterns. AtomicU32 keeps the ring in
/// safe Rust: single producer, single consumer, and the index stores carry the
/// Release/Acquire ordering the data stores need.
pub struct CueRing {
    samples: Box<[AtomicU32]>,
    capacity: usize,
    write: AtomicUsize,
    read: AtomicUsize,
    /// Consumer-owned: true until the ring first reaches the target fill, and
    /// again after every underrun.
    priming: AtomicBool,
    /// Consumer-owned smoothed fill, as f64 bits.
    fill_ema: AtomicU64,
    pub underruns: AtomicU64,
    pub overflows: AtomicU64,
    pub dropped: AtomicU64,
    pub duplicated: AtomicU64,
    pub frames_in: AtomicU64,
    pub frames_out: AtomicU64,
}

/// What one pull did, for tests and stats.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PullOutcome {
    pub priming: bool,
    pub underrun: bool,
    pub dropped: bool,
    pub duplicated: bool,
}

impl CueRing {
    pub fn new(capacity_frames: usize) -> Self {
        assert!(
            capacity_frames > TARGET_FRAMES + DRIFT_BAND_FRAMES,
            "cue ring capacity must exceed target plus drift band"
        );
        let samples = (0..capacity_frames * 2).map(|_| AtomicU32::new(0)).collect::<Vec<_>>();
        Self {
            samples: samples.into_boxed_slice(),
            capacity: capacity_frames,
            write: AtomicUsize::new(0),
            read: AtomicUsize::new(0),
            priming: AtomicBool::new(true),
            fill_ema: AtomicU64::new(0f64.to_bits()),
            underruns: AtomicU64::new(0),
            overflows: AtomicU64::new(0),
            dropped: AtomicU64::new(0),
            duplicated: AtomicU64::new(0),
            frames_in: AtomicU64::new(0),
            frames_out: AtomicU64::new(0),
        }
    }

    fn fill_between(&self, write: usize, read: usize) -> usize {
        if write >= read {
            write - read
        } else {
            self.capacity - read + write
        }
    }

    pub fn fill(&self) -> usize {
        self.fill_between(self.write.load(Ordering::Acquire), self.read.load(Ordering::Acquire))
    }

    pub fn is_priming(&self) -> bool {
        self.priming.load(Ordering::Relaxed)
    }

    /// Producer side: append interleaved stereo frames. Frames that do not fit
    /// are counted and discarded; one slot stays empty so full != empty.
    pub fn push_interleaved(&self, interleaved: &[f32]) {
        let mut write = self.write.load(Ordering::Relaxed);
        let read = self.read.load(Ordering::Acquire);
        let mut free = self.capacity - 1 - self.fill_between(write, read);
        let frames = interleaved.len() / 2;
        let mut accepted = 0u64;
        for frame in interleaved.chunks_exact(2) {
            if free == 0 {
                self.overflows.fetch_add(1, Ordering::Relaxed);
                continue;
            }
            self.samples[write * 2].store(frame[0].to_bits(), Ordering::Relaxed);
            self.samples[write * 2 + 1].store(frame[1].to_bits(), Ordering::Relaxed);
            write = (write + 1) % self.capacity;
            free -= 1;
            accepted += 1;
        }
        self.write.store(write, Ordering::Release);
        self.frames_in.fetch_add(accepted, Ordering::Relaxed);
        debug_assert!(accepted as usize <= frames);
    }

    /// Consumer side: fill `out` (interleaved stereo, `out.len() / 2` frames).
    /// Never blocks and never allocates: it runs on the CoreAudio render thread.
    pub fn pull_interleaved(&self, out: &mut [f32]) -> PullOutcome {
        let frames = out.len() / 2;
        let write = self.write.load(Ordering::Acquire);
        let mut read = self.read.load(Ordering::Relaxed);
        let mut fill = self.fill_between(write, read);

        if self.priming.load(Ordering::Relaxed) {
            out.fill(0.0);
            if fill >= TARGET_FRAMES {
                self.priming.store(false, Ordering::Relaxed);
                self.fill_ema.store((fill as f64).to_bits(), Ordering::Relaxed);
            }
            return PullOutcome { priming: true, underrun: false, dropped: false, duplicated: false };
        }

        let mut ema = f64::from_bits(self.fill_ema.load(Ordering::Relaxed));
        ema += (fill as f64 - ema) * FILL_EMA_ALPHA;
        let hi = (TARGET_FRAMES + DRIFT_BAND_FRAMES) as f64;
        let lo = (TARGET_FRAMES - DRIFT_BAND_FRAMES) as f64;
        let mut dropped = false;
        let mut duplicated = false;
        let mut out_frame = 0usize;
        if ema > hi && fill > 1 {
            read = (read + 1) % self.capacity;
            fill -= 1;
            ema -= 1.0;
            dropped = true;
        } else if ema < lo && fill > 0 && frames > 0 {
            // Peek, do not consume: the loop reads this frame again into slot 1.
            out[0] = f32::from_bits(self.samples[read * 2].load(Ordering::Relaxed));
            out[1] = f32::from_bits(self.samples[read * 2 + 1].load(Ordering::Relaxed));
            ema += 1.0;
            duplicated = true;
            out_frame = 1;
        }

        let mut underrun = false;
        while out_frame < frames {
            if fill == 0 {
                out[out_frame * 2..].fill(0.0);
                underrun = true;
                break;
            }
            out[out_frame * 2] = f32::from_bits(self.samples[read * 2].load(Ordering::Relaxed));
            out[out_frame * 2 + 1] = f32::from_bits(self.samples[read * 2 + 1].load(Ordering::Relaxed));
            read = (read + 1) % self.capacity;
            fill -= 1;
            out_frame += 1;
        }
        self.read.store(read, Ordering::Release);
        self.fill_ema.store(ema.to_bits(), Ordering::Relaxed);
        self.frames_out.fetch_add(out_frame as u64, Ordering::Relaxed);
        if dropped {
            self.dropped.fetch_add(1, Ordering::Relaxed);
        }
        if duplicated {
            self.duplicated.fetch_add(1, Ordering::Relaxed);
        }
        if underrun {
            self.underruns.fetch_add(1, Ordering::Relaxed);
            self.priming.store(true, Ordering::Relaxed);
        }
        PullOutcome { priming: false, underrun, dropped, duplicated }
    }

    /// Consumer-side reset for a fresh device: forget what was buffered for
    /// the previous one. Only called while no render callback is running.
    pub fn reset(&self) {
        let write = self.write.load(Ordering::Acquire);
        self.read.store(write, Ordering::Release);
        self.priming.store(true, Ordering::Relaxed);
        self.fill_ema.store(0f64.to_bits(), Ordering::Relaxed);
    }

    pub fn stats_json(&self) -> Value {
        json!({
            "fill": self.fill(),
            "priming": self.is_priming(),
            "underruns": self.underruns.load(Ordering::Relaxed),
            "overflows": self.overflows.load(Ordering::Relaxed),
            "dropped": self.dropped.load(Ordering::Relaxed),
            "duplicated": self.duplicated.load(Ordering::Relaxed),
            "frames_in": self.frames_in.load(Ordering::Relaxed),
            "frames_out": self.frames_out.load(Ordering::Relaxed),
        })
    }
}

/// What a built-in output physically is. A MacBook's speakers and its
/// headphone jack share one codec: jack sense mutes the speakers while
/// anything is plugged in, so while the jack is occupied the two are ONE usable
/// output, the jack. CoreAudio says which through the output data source
/// (`'ispk'` internal speaker, `'hdpn'` headphones); an Intel Mac's single
/// "Built-in Output" switches its data source, an Apple silicon Mac lists a
/// separate "External Headphones" device only while the jack is occupied.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum BuiltinRole {
    NotBuiltin,
    Speaker,
    Headphones,
}

impl BuiltinRole {
    fn as_json(self) -> Value {
        match self {
            BuiltinRole::NotBuiltin => Value::Null,
            BuiltinRole::Speaker => json!("speaker"),
            BuiltinRole::Headphones => json!("headphones"),
        }
    }
}

/// Classify one device. The data source is the authority; the well-known
/// Apple silicon UIDs only stand in when the data source cannot be read.
pub fn builtin_role(transport: &str, data_source: Option<u32>, uid: &str) -> BuiltinRole {
    if transport != "builtin" {
        return BuiltinRole::NotBuiltin;
    }
    match data_source {
        Some(code) if code == crate::output_health::fourcc(b"ispk") => BuiltinRole::Speaker,
        Some(code) if code == crate::output_health::fourcc(b"hdpn") => BuiltinRole::Headphones,
        Some(_) => BuiltinRole::NotBuiltin,
        None if uid == "BuiltInSpeakerDevice" => BuiltinRole::Speaker,
        None if uid == "BuiltInHeadphoneOutputDevice" => BuiltinRole::Headphones,
        None => BuiltinRole::NotBuiltin,
    }
}

/// One output-capable device as the page sees it.
#[derive(Debug, Clone, PartialEq)]
pub struct OutputDevice {
    pub uid: String,
    pub name: String,
    pub channels: u32,
    pub transport: &'static str,
    pub is_default: bool,
    pub builtin_role: BuiltinRole,
    /// The output data source four-char code (`ispk`, `hdpn`), when readable.
    /// Diagnostic: it says whether the role came from CoreAudio or the UID.
    pub data_source: Option<String>,
    /// `kAudioDevicePropertyJackIsConnected` where the device answers it.
    pub jack_connected: Option<bool>,
}

/// The built-in headphone device occupying the jack, when there is one.
fn occupied_jack(devices: &[OutputDevice]) -> Option<&OutputDevice> {
    devices
        .iter()
        .find(|d| d.builtin_role == BuiltinRole::Headphones && d.jack_connected != Some(false))
}

/// True for the built-in speakers while the headphone jack is occupied: the
/// HAL still runs and "delivers", but the amplifier is muted, so nobody hears it.
pub fn muted_by_jack(device: &OutputDevice, devices: &[OutputDevice]) -> bool {
    device.builtin_role == BuiltinRole::Speaker && occupied_jack(devices).is_some()
}

/// The uid of the output that actually sounds for `uid`: the occupied jack for
/// the muted built-in speakers, otherwise the device itself. Two uids with the
/// same physical output are ONE output for MASTER/CUE routing.
pub fn physical_output_uid<'a>(uid: &'a str, devices: &'a [OutputDevice]) -> &'a str {
    match devices.iter().find(|d| d.uid == uid) {
        Some(device) if muted_by_jack(device, devices) => occupied_jack(devices).map_or(uid, |jack| jack.uid.as_str()),
        Some(_) | None => uid,
    }
}

pub fn devices_json(devices: &[OutputDevice]) -> Value {
    Value::Array(
        devices
            .iter()
            .map(|d| {
                json!({
                    "uid": d.uid,
                    "name": d.name,
                    "channels": d.channels,
                    "transport": d.transport,
                    "is_default": d.is_default,
                    "builtin_role": d.builtin_role.as_json(),
                    "data_source": d.data_source,
                    "jack_connected": d.jack_connected,
                    "muted_by_jack": muted_by_jack(d, devices),
                    "physical_uid": physical_output_uid(&d.uid, devices),
                })
            })
            .collect(),
    )
}

/// A change the device watch reports: the listing differs in membership or in
/// which device is the default. Name-only churn is not a change.
pub fn device_signature(devices: &[OutputDevice]) -> String {
    let mut parts: Vec<String> = devices
        .iter()
        .map(|d| format!("{}{}", if d.is_default { "*" } else { "" }, d.uid))
        .collect();
    parts.sort();
    parts.join("\n")
}

/// After one re-assert, a second move of the default inside this window is a
/// fight (the operator or macOS itself insists), so the pin is released and the
/// room follows macOS instead of being reverted every second.
pub const REASSERT_FIGHT_WINDOW: Duration = Duration::from_secs(10);

/// Why MASTER cannot be (or stay) pinned. Each one is surfaced to the page,
/// the shell log and `GET /api/v1/audio/output-health`; none is silent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum MasterFault {
    /// The pin is the built-in speakers while headphones occupy the jack.
    MutedByJack { uid: String, jack_uid: String, cue_on_jack: bool },
    /// MASTER and the open CUE device are one physical output.
    SharesCue { uid: String, cue_uid: String },
    /// The default moved off the pin again right after a re-assert.
    OverriddenBySystem { uid: String, default_uid: Option<String> },
}

impl MasterFault {
    pub fn kind(&self) -> &'static str {
        match self {
            MasterFault::MutedByJack { .. } => "muted_by_jack",
            MasterFault::SharesCue { .. } => "shares_cue",
            MasterFault::OverriddenBySystem { .. } => "overridden_by_system",
        }
    }

    pub fn uid(&self) -> &str {
        match self {
            MasterFault::MutedByJack { uid, .. }
            | MasterFault::SharesCue { uid, .. }
            | MasterFault::OverriddenBySystem { uid, .. } => uid,
        }
    }

    pub fn message(&self) -> String {
        match self {
            MasterFault::MutedByJack { cue_on_jack: true, .. } => "MASTER and CUE can't share the MacBook's built-in output: \
                 the speakers are muted while headphones are in the jack. Use split cue (master L / cue R) \
                 or choose another MASTER device."
                .into(),
            MasterFault::MutedByJack { cue_on_jack: false, .. } => "MacBook speakers are muted while headphones are \
                 plugged into the headphone jack, so MASTER there would be silent. Choose the headphones or another output."
                .into(),
            MasterFault::SharesCue { cue_uid, .. } => format!(
                "MASTER and CUE are the same output ({cue_uid}); use split cue (master L / cue R) on it, not two outputs."
            ),
            MasterFault::OverriddenBySystem { uid, default_uid } => format!(
                "master pin overridden by system: the default output moved to {} again right after MASTER was put \
                 back on {uid}; MASTER now follows the system output.",
                default_uid.as_deref().unwrap_or("another device")
            ),
        }
    }

    pub fn to_json(&self, stage: &str) -> Value {
        json!({ "kind": self.kind(), "uid": self.uid(), "stage": stage, "message": self.message() })
    }
}

/// Refuse a MASTER pin that cannot sound, before anything changes. `None`
/// for a device that is absent: setting the default reports that itself.
pub fn master_pin_refusal(uid: &str, cue_uid: Option<&str>, devices: &[OutputDevice]) -> Option<MasterFault> {
    let device = devices.iter().find(|d| d.uid == uid)?;
    if muted_by_jack(device, devices) {
        let jack_uid = physical_output_uid(uid, devices).to_string();
        let cue_on_jack = cue_uid.is_some_and(|cue| physical_output_uid(cue, devices) == jack_uid);
        return Some(MasterFault::MutedByJack { uid: uid.into(), jack_uid, cue_on_jack });
    }
    match cue_uid {
        Some(cue) if physical_output_uid(cue, devices) == physical_output_uid(uid, devices) => {
            Some(MasterFault::SharesCue { uid: uid.into(), cue_uid: cue.into() })
        }
        Some(_) | None => None,
    }
}

/// What the pin watcher does after a listing.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PinDecision {
    Hold,
    /// Set the default output back to this pinned device.
    Reassert(String),
    /// Drop the pin and let the room follow macOS, reporting why.
    Release(MasterFault),
}

/// The pin watcher's policy. The re-assert exists for one case only: while a
/// cue device is open, a headphone plug or a Bluetooth reconnect taking the
/// default would put the room in the DJ's ears (CUEOUT-09 P0). So it fires only
/// for a pin that can sound, on a device genuinely distinct from the cue, and
/// at most once per `REASSERT_FIGHT_WINDOW`; a pin that cannot sound or keeps
/// losing the default is released with a reason, never reverted forever.
/// Outside two-device cue the operator's own change in macOS Sound settings
/// stands, and a vanished pin has nothing to put back.
pub fn master_pin_decision(
    pinned: Option<&str>,
    cue_uid: Option<&str>,
    devices: &[OutputDevice],
    since_last_reassert: Option<Duration>,
) -> PinDecision {
    let Some(pinned) = pinned else { return PinDecision::Hold };
    if !devices.iter().any(|d| d.uid == pinned) {
        return PinDecision::Hold;
    }
    if let Some(fault) = master_pin_refusal(pinned, cue_uid, devices) {
        return PinDecision::Release(fault);
    }
    let default_uid = devices.iter().find(|d| d.is_default).map(|d| d.uid.clone());
    if default_uid.as_deref() == Some(pinned) || cue_uid.is_none() {
        return PinDecision::Hold;
    }
    match since_last_reassert {
        Some(elapsed) if elapsed < REASSERT_FIGHT_WINDOW => {
            PinDecision::Release(MasterFault::OverriddenBySystem { uid: pinned.into(), default_uid })
        }
        Some(_) | None => PinDecision::Reassert(pinned.into()),
    }
}

/// The current MASTER fault, for `GET /api/v1/audio/output-health` and the
/// CLI. Process-wide because the health server has no handle on a connection.
static MASTER_FAULT: Mutex<Option<Value>> = Mutex::new(None);

fn set_master_fault(fault: Option<Value>) {
    *MASTER_FAULT.lock().expect("master fault mutex") = fault;
}

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn master_fault_json() -> Value {
    MASTER_FAULT.lock().expect("master fault mutex").clone().unwrap_or(Value::Null)
}

/// The shell's output listing, for the health probe.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn list_output_devices() -> Result<Vec<OutputDevice>, String> {
    platform::list_outputs()
}

/// A parsed control message from the page.
#[derive(Debug, Clone, PartialEq)]
pub enum Command {
    Hello,
    List,
    Open { uid: String, sample_rate: f64 },
    Close,
    SetMaster { uid: String },
    ClearMaster,
}

/// Parse one text frame. Fails loudly on anything malformed: an unknown type or
/// a missing field is a page bug, never something to guess around.
pub fn parse_command(text: &str) -> Result<(Option<u64>, Command), String> {
    let value: Value = serde_json::from_str(text).map_err(|e| format!("not JSON: {e}"))?;
    let obj = value.as_object().ok_or("command must be a JSON object")?;
    let id = obj.get("id").and_then(Value::as_u64);
    let ty = obj.get("type").and_then(Value::as_str).ok_or("command has no type")?;
    let uid = || -> Result<String, String> {
        let uid = obj
            .get("uid")
            .and_then(Value::as_str)
            .ok_or_else(|| format!("{ty} needs a uid"))?;
        if uid.trim().is_empty() {
            return Err(format!("{ty} uid is empty"));
        }
        Ok(uid.to_string())
    };
    let command = match ty {
        "hello" => Command::Hello,
        "list" => Command::List,
        "open" => {
            let sample_rate = obj
                .get("sample_rate")
                .and_then(Value::as_f64)
                .ok_or("open needs a sample_rate")?;
            if !(8_000.0..=384_000.0).contains(&sample_rate) {
                return Err(format!("open sample_rate {sample_rate} is out of range"));
            }
            Command::Open { uid: uid()?, sample_rate }
        }
        "close" => Command::Close,
        "set_master" => Command::SetMaster { uid: uid()? },
        "clear_master" => Command::ClearMaster,
        other => return Err(format!("unknown command type {other:?}")),
    };
    Ok((id, command))
}

/// Decode a binary frame of little-endian f32 interleaved stereo samples.
pub fn decode_pcm(bytes: &[u8], scratch: &mut Vec<f32>) -> Result<(), String> {
    if bytes.len() % 8 != 0 {
        return Err(format!("PCM frame of {} bytes is not whole stereo f32 frames", bytes.len()));
    }
    scratch.clear();
    scratch.extend(bytes.chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])));
    Ok(())
}

/// The handshake guard: right path, right token, loopback page origin.
pub fn handshake_allowed(path_and_query: &str, origin: Option<&str>, token: &str) -> Result<(), String> {
    let (path, query) = path_and_query.split_once('?').unwrap_or((path_and_query, ""));
    if path != CUE_SINK_PATH {
        return Err(format!("unknown path {path}"));
    }
    let presented = query
        .split('&')
        .find_map(|pair| pair.strip_prefix("token="))
        .ok_or("missing token")?;
    if presented != token {
        return Err("bad token".into());
    }
    let origin = origin.ok_or("missing Origin")?;
    let loopback = ["http://127.0.0.1", "http://localhost", "tauri://localhost", "https://tauri.localhost"]
        .iter()
        .any(|prefix| {
            origin == *prefix
                || origin
                    .strip_prefix(prefix)
                    .is_some_and(|rest| rest.starts_with(':') && rest[1..].chars().all(|c| c.is_ascii_digit()))
        });
    if !loopback {
        return Err(format!("origin {origin} is not the app's loopback page"));
    }
    Ok(())
}

/// 128 bits from two independently seeded std hashers. Enough to keep other
/// local processes off the socket; the port alone is guessable.
fn new_token() -> String {
    let seed = (std::process::id(), Instant::now(), std::time::SystemTime::now());
    let a = RandomState::new().hash_one(format!("{seed:?}a"));
    let b = RandomState::new().hash_one(format!("{seed:?}b"));
    format!("{a:016x}{b:016x}")
}

struct Shared {
    token: String,
    ring: Arc<CueRing>,
    output: Mutex<Option<platform::CueOutput>>,
    master_pin: Mutex<Option<String>>,
    /// When the watcher last put the default back on the pin; cleared by a new pin.
    last_reassert: Mutex<Option<Instant>>,
    /// Bumped per accepted connection; an older connection loop sees the
    /// change and exits, so exactly one page feeds the ring.
    generation: AtomicU64,
}

/// The running server. Dropping it does not stop the accept thread; the shell
/// keeps it for the life of the process, like the shell health server.
pub struct CueSinkServer {
    port: u16,
    token: String,
}

impl CueSinkServer {
    pub fn start(log: fn(&str)) -> Result<Self, String> {
        let listener = TcpListener::bind("127.0.0.1:0")
            .map_err(|err| format!("binding cue sink listener failed: {err}"))?;
        let port = listener
            .local_addr()
            .map_err(|err| format!("reading cue sink port failed: {err}"))?
            .port();
        let shared = Arc::new(Shared {
            token: new_token(),
            ring: Arc::new(CueRing::new(RING_CAPACITY_FRAMES)),
            output: Mutex::new(None),
            master_pin: Mutex::new(None),
            last_reassert: Mutex::new(None),
            generation: AtomicU64::new(0),
        });
        let token = shared.token.clone();
        thread::Builder::new()
            .name("cue-sink-accept".into())
            .spawn(move || {
                for stream in listener.incoming() {
                    match stream {
                        Ok(stream) => {
                            let shared = Arc::clone(&shared);
                            let generation = shared.generation.fetch_add(1, Ordering::SeqCst) + 1;
                            let spawned = thread::Builder::new().name("cue-sink-conn".into()).spawn(move || {
                                if let Err(err) = serve_connection(stream, &shared, generation, log) {
                                    log(&format!("cue sink connection ended: {err}"));
                                }
                            });
                            if let Err(err) = spawned {
                                log(&format!("cue sink connection thread failed: {err}"));
                            }
                        }
                        Err(err) => log(&format!("cue sink accept failed: {err}")),
                    }
                }
            })
            .map_err(|err| format!("spawning cue sink accept thread failed: {err}"))?;
        Ok(Self { port, token })
    }

    /// The init-script assignment that tells the page where the sink is.
    pub fn init_script(&self) -> Result<String, serde_json::Error> {
        let config = json!({
            "url": format!("ws://127.0.0.1:{}{}", self.port, CUE_SINK_PATH),
            "token": self.token,
            "protocol": 1,
        });
        Ok(format!("globalThis.OPENDJ_CUE_SINK = {};", serde_json::to_string(&config)?))
    }
}

type Socket = tungstenite::WebSocket<TcpStream>;

fn send(socket: &mut Socket, value: Value) -> Result<(), String> {
    socket
        .send(tungstenite::Message::text(value.to_string()))
        .map_err(|err| format!("send failed: {err}"))
}

fn serve_connection(stream: TcpStream, shared: &Arc<Shared>, generation: u64, log: fn(&str)) -> Result<(), String> {
    stream.set_nodelay(true).map_err(|e| format!("nodelay: {e}"))?;
    let token = shared.token.clone();
    let callback = move |request: &tungstenite::handshake::server::Request,
                         response: tungstenite::handshake::server::Response| {
        let origin = request.headers().get("origin").and_then(|v| v.to_str().ok());
        let target = request.uri().path_and_query().map(|pq| pq.as_str()).unwrap_or("");
        match handshake_allowed(target, origin, &token) {
            Ok(()) => Ok(response),
            Err(reason) => {
                let mut refused = tungstenite::handshake::server::ErrorResponse::new(Some(reason));
                *refused.status_mut() = tungstenite::http::StatusCode::FORBIDDEN;
                Err(refused)
            }
        }
    };
    let mut socket = tungstenite::accept_hdr(stream, callback).map_err(|err| format!("handshake refused: {err}"))?;
    socket
        .get_ref()
        .set_read_timeout(Some(READ_POLL))
        .map_err(|e| format!("read timeout: {e}"))?;
    log("cue sink: page connected");

    let mut scratch: Vec<f32> = Vec::with_capacity(4096);
    let mut last_housekeeping = Instant::now();
    let mut last_signature: Option<String> = None;
    let result = loop {
        if shared.generation.load(Ordering::SeqCst) != generation {
            break Ok(());
        }
        match socket.read() {
            Ok(tungstenite::Message::Binary(bytes)) => {
                if let Err(err) = decode_pcm(&bytes, &mut scratch) {
                    break Err(err);
                }
                shared.ring.push_interleaved(&scratch);
            }
            Ok(tungstenite::Message::Text(text)) => {
                let reply = match parse_command(text.as_str()) {
                    Ok((id, command)) => handle_command(shared, command, log).map(|mut v| {
                        if let (Some(id), Some(obj)) = (id, v.as_object_mut()) {
                            obj.insert("id".into(), json!(id));
                        }
                        v
                    }),
                    Err(err) => Err(err),
                };
                let value = match reply {
                    Ok(v) => v,
                    Err(message) => {
                        log(&format!("cue sink command failed: {message}"));
                        let id = serde_json::from_str::<Value>(text.as_str())
                            .ok()
                            .and_then(|v| v.get("id").cloned())
                            .unwrap_or(Value::Null);
                        json!({ "type": "error", "id": id, "message": message })
                    }
                };
                if let Err(err) = send(&mut socket, value) {
                    break Err(err);
                }
            }
            Ok(tungstenite::Message::Close(_)) => break Ok(()),
            Ok(_) => {}
            Err(tungstenite::Error::Io(err))
                if matches!(err.kind(), ErrorKind::WouldBlock | ErrorKind::TimedOut) => {}
            Err(tungstenite::Error::ConnectionClosed | tungstenite::Error::AlreadyClosed) => break Ok(()),
            Err(err) => break Err(format!("read failed: {err}")),
        }
        if last_housekeeping.elapsed() >= HOUSEKEEPING_EVERY {
            last_housekeeping = Instant::now();
            for event in housekeeping(shared, &mut last_signature, log) {
                if let Err(err) = send(&mut socket, event) {
                    return finish(shared, generation, Err(err), log);
                }
            }
        }
    };
    finish(shared, generation, result, log)
}

/// The page is gone (closed, reloaded or replaced): nothing feeds the ring any
/// more, so stop the device and drop the master pin. A replacing connection
/// owns both from here, so a stale loop leaves them alone.
fn finish(shared: &Arc<Shared>, generation: u64, result: Result<(), String>, log: fn(&str)) -> Result<(), String> {
    if shared.generation.load(Ordering::SeqCst) == generation {
        if shared.output.lock().expect("cue output mutex").take().is_some() {
            log("cue sink: page disconnected; cue device closed");
        }
        *shared.master_pin.lock().expect("master pin mutex") = None;
        *shared.last_reassert.lock().expect("last reassert mutex") = None;
        set_master_fault(None);
    }
    result
}

fn handle_command(shared: &Arc<Shared>, command: Command, log: fn(&str)) -> Result<Value, String> {
    match command {
        Command::Hello => Ok(json!({ "type": "hello", "protocol": 1, "supported": platform::SUPPORTED })),
        Command::List => {
            let devices = platform::list_outputs()?;
            Ok(json!({ "type": "devices", "devices": devices_json(&devices) }))
        }
        Command::Open { uid, sample_rate } => {
            let mut slot = shared.output.lock().expect("cue output mutex");
            // Stop the old device before the ring is reset: the reset is only
            // safe with no render callback reading it.
            drop(slot.take());
            shared.ring.reset();
            let output = platform::CueOutput::open(&uid, sample_rate, Arc::clone(&shared.ring))?;
            let info = output.info_json();
            log(&format!("cue sink: opened {uid} at {sample_rate} Hz"));
            *slot = Some(output);
            Ok(json!({ "type": "opened", "uid": uid, "device": info, "target_frames": TARGET_FRAMES }))
        }
        Command::Close => {
            let closed = shared.output.lock().expect("cue output mutex").take().is_some();
            Ok(json!({ "type": "closed", "was_open": closed }))
        }
        Command::SetMaster { uid } => {
            let devices = platform::list_outputs()?;
            let cue_uid = shared.output.lock().expect("cue output mutex").as_ref().map(|o| o.uid().to_string());
            if let Some(fault) = master_pin_refusal(&uid, cue_uid.as_deref(), &devices) {
                log(&format!("cue sink: refused to pin master to {uid}: {}", fault.message()));
                set_master_fault(Some(fault.to_json("refused")));
                return Err(fault.message());
            }
            platform::set_default_output(&uid)?;
            *shared.master_pin.lock().expect("master pin mutex") = Some(uid.clone());
            *shared.last_reassert.lock().expect("last reassert mutex") = None;
            set_master_fault(None);
            log(&format!("cue sink: master pinned to {uid} (macOS default output)"));
            Ok(json!({ "type": "master_set", "uid": uid }))
        }
        Command::ClearMaster => {
            *shared.master_pin.lock().expect("master pin mutex") = None;
            *shared.last_reassert.lock().expect("last reassert mutex") = None;
            set_master_fault(None);
            Ok(json!({ "type": "master_cleared" }))
        }
    }
}

/// Once a second: stats, device membership changes, a vanished cue device, and
/// the MASTER pin. Returns the events to push to the page.
fn housekeeping(shared: &Arc<Shared>, last_signature: &mut Option<String>, log: fn(&str)) -> Vec<Value> {
    let mut events = Vec::new();
    let devices = match platform::list_outputs() {
        Ok(devices) => devices,
        Err(_) => return events,
    };
    {
        let mut slot = shared.output.lock().expect("cue output mutex");
        if let Some(output) = slot.as_ref() {
            let uid = output.uid().to_string();
            if !devices.iter().any(|d| d.uid == uid) || !output.alive() {
                // Never fall back to another device: a cue that lands in the
                // room is the failure this whole route exists to prevent.
                drop(slot.take());
                log(&format!("cue sink: cue device {uid} vanished; cue output stopped"));
                events.push(json!({ "type": "device_lost", "uid": uid }));
            } else {
                events.push(json!({ "type": "stats", "uid": uid, "ring": shared.ring.stats_json() }));
            }
        }
    }
    let pinned = shared.master_pin.lock().expect("master pin mutex").clone();
    let cue_uid = shared.output.lock().expect("cue output mutex").as_ref().map(|o| o.uid().to_string());
    let since_last_reassert = shared.last_reassert.lock().expect("last reassert mutex").map(|at| at.elapsed());
    match master_pin_decision(pinned.as_deref(), cue_uid.as_deref(), &devices, since_last_reassert) {
        PinDecision::Hold => {}
        PinDecision::Reassert(target) => {
            let from = devices.iter().find(|d| d.is_default).map(|d| d.uid.clone());
            match platform::set_default_output(&target) {
                Ok(()) => {
                    *shared.last_reassert.lock().expect("last reassert mutex") = Some(Instant::now());
                    log(&format!("cue sink: macOS moved the default output to {from:?}; MASTER put back on {target}"));
                    events.push(json!({ "type": "master_reasserted", "uid": target, "from_uid": from }));
                }
                Err(err) => log(&format!("cue sink: putting MASTER back on {target} failed: {err}")),
            }
        }
        PinDecision::Release(fault) => {
            *shared.master_pin.lock().expect("master pin mutex") = None;
            *shared.last_reassert.lock().expect("last reassert mutex") = None;
            let message = fault.message();
            log(&format!("cue sink: master pin on {} released ({}): {message}", fault.uid(), fault.kind()));
            set_master_fault(Some(fault.to_json("released")));
            let default_uid = devices.iter().find(|d| d.is_default).map(|d| d.uid.clone());
            events.push(json!({
                "type": "master_pin_released",
                "uid": fault.uid(),
                "reason": fault.kind(),
                "message": message,
                "default_uid": default_uid,
            }));
        }
    }
    let signature = device_signature(&devices);
    if last_signature.as_deref() != Some(signature.as_str()) {
        if last_signature.is_some() {
            events.push(json!({ "type": "devices_changed", "devices": devices_json(&devices) }));
        }
        *last_signature = Some(signature);
    }
    events
}

#[cfg(target_os = "macos")]
mod platform {
    use super::{builtin_role, CueRing, OutputDevice};
    use crate::output_health::platform::{
        get_cf_string, get_default_output_device, get_device_list, output_channel_count, prop_addr,
        set_default_output_device, set_unit_property, AURenderCallbackStruct, AudioBuffer, AudioBufferList,
        AudioComponentDescription, AudioComponentFindNext, AudioComponentInstanceDispose,
        AudioComponentInstanceNew, AudioDeviceID, AudioObjectGetPropertyData, AudioOutputUnitStart,
        AudioOutputUnitStop, AudioStreamBasicDescription, AudioTimeStamp, AudioUnit, AudioUnitInitialize,
        AudioUnitUninitialize, OSStatus, K_AUDIO_DEVICE_PROPERTY_DEVICE_UID, K_AUDIO_FORMAT_FLAG_IS_PACKED,
        K_AUDIO_FORMAT_LINEAR_PCM, K_AUDIO_OBJECT_PROPERTY_NAME, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
        K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT, K_AUDIO_OUTPUT_UNIT_PROPERTY_CURRENT_DEVICE,
        K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO, K_AUDIO_UNIT_MANUFACTURER_APPLE, K_AUDIO_UNIT_PROPERTY_SET_RENDER_CALLBACK,
        K_AUDIO_UNIT_PROPERTY_STREAM_FORMAT, K_AUDIO_UNIT_SUBTYPE_HAL_OUTPUT, K_AUDIO_UNIT_TYPE_OUTPUT, NO_ERR,
    };
    use crate::output_health::{auhal_plan, fourcc};
    use serde_json::{json, Value};
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::Arc;

    pub const SUPPORTED: bool = true;

    const K_AUDIO_FORMAT_FLAG_IS_FLOAT: u32 = 0x1;
    const K_AUDIO_DEVICE_PROPERTY_DEVICE_IS_ALIVE: u32 = fourcc(b"livn");
    const K_AUDIO_DEVICE_PROPERTY_TRANSPORT_TYPE: u32 = fourcc(b"tran");
    const K_AUDIO_DEVICE_PROPERTY_NOMINAL_SAMPLE_RATE: u32 = fourcc(b"nsrt");
    const K_AUDIO_DEVICE_PROPERTY_LATENCY: u32 = fourcc(b"ltnc");
    const K_AUDIO_DEVICE_PROPERTY_SAFETY_OFFSET: u32 = fourcc(b"saft");
    const K_AUDIO_DEVICE_PROPERTY_BUFFER_FRAME_SIZE: u32 = fourcc(b"fsiz");
    const K_AUDIO_DEVICE_PROPERTY_DATA_SOURCE: u32 = fourcc(b"ssrc");
    const K_AUDIO_DEVICE_PROPERTY_JACK_IS_CONNECTED: u32 = fourcc(b"jack");

    fn transport_name(code: u32) -> &'static str {
        match code {
            c if c == fourcc(b"bltn") => "builtin",
            c if c == fourcc(b"blue") || c == fourcc(b"blea") => "bluetooth",
            c if c == fourcc(b"usb ") => "usb",
            c if c == fourcc(b"hdmi") || c == fourcc(b"dprt") => "display",
            c if c == fourcc(b"airp") => "airplay",
            c if c == fourcc(b"grup") || c == fourcc(b"agrp") => "aggregate",
            c if c == fourcc(b"virt") => "virtual",
            c if c == fourcc(b"thun") => "thunderbolt",
            _ => "other",
        }
    }

    /// A fixed-size property read on `scope`. `T` must be plain data.
    fn get_scoped<T: Copy + Default>(device: AudioDeviceID, selector: u32, scope: u32) -> Result<T, String> {
        let address = prop_addr(selector, scope);
        let mut value = T::default();
        let mut size = std::mem::size_of::<T>() as u32;
        // SAFETY: `value` is a live T and `size` says exactly that many bytes.
        let status = unsafe {
            AudioObjectGetPropertyData(device, &address, 0, ptr::null(), &mut size, &mut value as *mut T as *mut c_void)
        };
        if status != NO_ERR {
            return Err(format!("property {selector:#x} read failed: {status}"));
        }
        Ok(value)
    }

    pub fn list_outputs() -> Result<Vec<OutputDevice>, String> {
        let default = get_default_output_device().ok();
        let mut out = Vec::new();
        for id in get_device_list()? {
            let channels = output_channel_count(id).unwrap_or(0);
            if channels == 0 {
                continue;
            }
            let Ok(uid) = get_cf_string(id, K_AUDIO_DEVICE_PROPERTY_DEVICE_UID) else { continue };
            let name = get_cf_string(id, K_AUDIO_OBJECT_PROPERTY_NAME).unwrap_or_else(|_| uid.clone());
            let transport =
                get_scoped::<u32>(id, K_AUDIO_DEVICE_PROPERTY_TRANSPORT_TYPE, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL)
                    .map(transport_name)
                    .unwrap_or("other");
            // Both reads fail on most devices (no data source, no jack):
            // that is "unknown", and the classifier falls back to the UID.
            let data_source =
                get_scoped::<u32>(id, K_AUDIO_DEVICE_PROPERTY_DATA_SOURCE, K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT).ok();
            let jack_connected =
                get_scoped::<u32>(id, K_AUDIO_DEVICE_PROPERTY_JACK_IS_CONNECTED, K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT)
                    .ok()
                    .map(|connected| connected != 0);
            let builtin_role = builtin_role(transport, data_source, &uid);
            out.push(OutputDevice {
                uid,
                name,
                channels,
                transport,
                is_default: Some(id) == default,
                builtin_role,
                data_source: data_source.map(|code| String::from_utf8_lossy(&code.to_be_bytes()).into_owned()),
                jack_connected,
            });
        }
        Ok(out)
    }

    fn find_device(uid: &str) -> Result<AudioDeviceID, String> {
        for id in get_device_list()? {
            if output_channel_count(id).unwrap_or(0) == 0 {
                continue;
            }
            if get_cf_string(id, K_AUDIO_DEVICE_PROPERTY_DEVICE_UID).ok().as_deref() == Some(uid) {
                return Ok(id);
            }
        }
        Err(format!("no output device with uid {uid}"))
    }

    pub fn set_default_output(uid: &str) -> Result<(), String> {
        set_default_output_device(find_device(uid)?)
    }

    /// The ring the render callback reads, reached through the AU's ref con.
    struct RenderState {
        ring: Arc<CueRing>,
    }

    extern "C" fn render(
        in_ref_con: *mut c_void,
        _io_action_flags: *mut u32,
        _in_time_stamp: *const AudioTimeStamp,
        _in_bus_number: u32,
        in_number_frames: u32,
        io_data: *mut AudioBufferList,
    ) -> OSStatus {
        if in_ref_con.is_null() || io_data.is_null() {
            return NO_ERR;
        }
        // SAFETY: the ref con is the boxed RenderState that `CueOutput` frees
        // only after the unit is stopped and disposed; `io_data` is this
        // cycle's list. The client format is packed interleaved f32 stereo, so
        // buffer 0 holds `frames * 2` samples, bounded by its byte size.
        let state = unsafe { &*(in_ref_con as *const RenderState) };
        let list = unsafe { &mut *io_data };
        if list.m_number_buffers == 0 {
            return NO_ERR;
        }
        let first = ptr::addr_of_mut!(list.m_buffers) as *mut AudioBuffer;
        let buffer = unsafe { &mut *first };
        if buffer.m_data.is_null() {
            return NO_ERR;
        }
        let capacity = buffer.m_data_byte_size as usize / std::mem::size_of::<f32>();
        let len = (in_number_frames as usize * 2).min(capacity) & !1;
        let samples = unsafe { std::slice::from_raw_parts_mut(buffer.m_data as *mut f32, len) };
        state.ring.pull_interleaved(samples);
        NO_ERR
    }

    pub struct CueOutput {
        unit: AudioUnit,
        state: *mut RenderState,
        device: AudioDeviceID,
        uid: String,
        info: Value,
    }

    // SAFETY: the AudioUnit handle and the state pointer are only touched under
    // the server's output mutex; CoreAudio's render thread reaches the state
    // through the ref con, which outlives it (see Drop).
    unsafe impl Send for CueOutput {}

    impl CueOutput {
        pub fn open(uid: &str, sample_rate: f64, ring: Arc<CueRing>) -> Result<Self, String> {
            let device = find_device(uid)?;
            let desc = AudioComponentDescription {
                component_type: K_AUDIO_UNIT_TYPE_OUTPUT,
                component_sub_type: K_AUDIO_UNIT_SUBTYPE_HAL_OUTPUT,
                component_manufacturer: K_AUDIO_UNIT_MANUFACTURER_APPLE,
                component_flags: 0,
                component_flags_mask: 0,
            };
            // SAFETY: `desc` is valid; a null result is handled.
            let component = unsafe { AudioComponentFindNext(ptr::null_mut(), &desc) };
            if component.is_null() {
                return Err("HAL output AudioUnit component not found".into());
            }
            let mut unit: AudioUnit = ptr::null_mut();
            // SAFETY: `component` is non-null and `unit` is a live out slot.
            let status = unsafe { AudioComponentInstanceNew(component, &mut unit) };
            if status != NO_ERR || unit.is_null() {
                return Err(format!("AudioComponentInstanceNew failed: {status}"));
            }
            let state = Box::into_raw(Box::new(RenderState { ring }));
            // SAFETY: `unit` is live until disposed below or by Drop; `state`
            // outlives every render callback (freed only after dispose).
            let started = unsafe {
                (|| {
                    let enable = 1u32;
                    let disable = 0u32;
                    set_unit_property(unit, K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO, auhal_plan::ENABLE_OUTPUT_IO, &enable, "enable HAL output IO")?;
                    set_unit_property(unit, K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO, auhal_plan::DISABLE_INPUT_IO, &disable, "disable HAL input IO")?;
                    set_unit_property(unit, K_AUDIO_OUTPUT_UNIT_PROPERTY_CURRENT_DEVICE, auhal_plan::CURRENT_DEVICE, &device, "set HAL current device")?;
                    let fmt = AudioStreamBasicDescription {
                        m_sample_rate: sample_rate,
                        m_format_id: K_AUDIO_FORMAT_LINEAR_PCM,
                        m_format_flags: K_AUDIO_FORMAT_FLAG_IS_FLOAT | K_AUDIO_FORMAT_FLAG_IS_PACKED,
                        m_bytes_per_packet: 8,
                        m_frames_per_packet: 1,
                        m_bytes_per_frame: 8,
                        m_channels_per_frame: 2,
                        m_bits_per_channel: 32,
                        m_reserved: 0,
                    };
                    set_unit_property(unit, K_AUDIO_UNIT_PROPERTY_STREAM_FORMAT, auhal_plan::CLIENT_STREAM_FORMAT, &fmt, "set HAL client stream format")?;
                    let callback = AURenderCallbackStruct { input_proc: render, input_proc_ref_con: state as *mut c_void };
                    set_unit_property(unit, K_AUDIO_UNIT_PROPERTY_SET_RENDER_CALLBACK, auhal_plan::RENDER_CALLBACK, &callback, "set HAL render callback")?;
                    let status = AudioUnitInitialize(unit);
                    if status != NO_ERR {
                        return Err(format!("AudioUnitInitialize failed: {status}"));
                    }
                    let status = AudioOutputUnitStart(unit);
                    if status != NO_ERR {
                        let _ = AudioUnitUninitialize(unit);
                        return Err(format!("AudioOutputUnitStart failed: {status}"));
                    }
                    Ok(())
                })()
            };
            if let Err(err) = started {
                // SAFETY: the unit never started (or was uninitialized), so no
                // callback can still hold `state`.
                unsafe {
                    let _ = AudioComponentInstanceDispose(unit);
                    drop(Box::from_raw(state));
                }
                return Err(err);
            }
            let scope = K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT;
            let latency = get_scoped::<u32>(device, K_AUDIO_DEVICE_PROPERTY_LATENCY, scope).ok();
            let safety = get_scoped::<u32>(device, K_AUDIO_DEVICE_PROPERTY_SAFETY_OFFSET, scope).ok();
            let buffer = get_scoped::<u32>(device, K_AUDIO_DEVICE_PROPERTY_BUFFER_FRAME_SIZE, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL).ok();
            let rate = get_scoped::<f64>(device, K_AUDIO_DEVICE_PROPERTY_NOMINAL_SAMPLE_RATE, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL).ok();
            let name = get_cf_string(device, K_AUDIO_OBJECT_PROPERTY_NAME).ok();
            let device_latency_ms = match (latency, safety, buffer, rate) {
                (Some(l), Some(s), Some(b), Some(r)) if r > 0.0 => Some(f64::from(l + s + b) / r * 1000.0),
                _ => None,
            };
            let info = json!({
                "name": name,
                "client_sample_rate": sample_rate,
                "device_sample_rate": rate,
                "latency_frames": latency,
                "safety_offset_frames": safety,
                "buffer_frames": buffer,
                "device_latency_ms": device_latency_ms,
            });
            Ok(Self { unit, state, device, uid: uid.to_string(), info })
        }

        pub fn uid(&self) -> &str {
            &self.uid
        }

        pub fn info_json(&self) -> Value {
            self.info.clone()
        }

        pub fn alive(&self) -> bool {
            get_scoped::<u32>(self.device, K_AUDIO_DEVICE_PROPERTY_DEVICE_IS_ALIVE, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL)
                .map(|alive| alive != 0)
                .unwrap_or(false)
        }
    }

    impl Drop for CueOutput {
        fn drop(&mut self) {
            // SAFETY: stop, uninitialize and dispose the live unit exactly once;
            // after AudioOutputUnitStop returns no render callback is running,
            // so the state can be freed.
            unsafe {
                let _ = AudioOutputUnitStop(self.unit);
                let _ = AudioUnitUninitialize(self.unit);
                let _ = AudioComponentInstanceDispose(self.unit);
                drop(Box::from_raw(self.state));
            }
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::{CueRing, OutputDevice};
    use serde_json::Value;
    use std::sync::Arc;

    pub const SUPPORTED: bool = false;
    const UNSUPPORTED: &str = "native headphone cue output is only built for macOS so far";

    pub fn list_outputs() -> Result<Vec<OutputDevice>, String> {
        Err(UNSUPPORTED.into())
    }

    pub fn set_default_output(_uid: &str) -> Result<(), String> {
        Err(UNSUPPORTED.into())
    }

    /// Never constructed: `open` always fails here. Kept so the server code
    /// above compiles the same on every platform.
    #[allow(dead_code)]
    pub struct CueOutput {
        uid: String,
    }

    #[allow(dead_code)]
    impl CueOutput {
        pub fn open(_uid: &str, _sample_rate: f64, _ring: Arc<CueRing>) -> Result<Self, String> {
            Err(UNSUPPORTED.into())
        }
        pub fn uid(&self) -> &str {
            &self.uid
        }
        pub fn info_json(&self) -> Value {
            Value::Null
        }
        pub fn alive(&self) -> bool {
            false
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn stereo(frames: usize, start: f32) -> Vec<f32> {
        (0..frames).flat_map(|i| [start + i as f32, -(start + i as f32)]).collect()
    }

    fn primed_ring() -> CueRing {
        let ring = CueRing::new(RING_CAPACITY_FRAMES);
        ring.push_interleaved(&stereo(TARGET_FRAMES, 1.0));
        let mut out = vec![0.0; 256];
        assert!(ring.pull_interleaved(&mut out).priming, "first pull primes");
        assert!(!ring.is_priming(), "target fill ends priming");
        ring
    }

    #[test]
    fn priming_outputs_silence_until_the_target_fill() {
        let ring = CueRing::new(RING_CAPACITY_FRAMES);
        ring.push_interleaved(&stereo(TARGET_FRAMES - 1, 1.0));
        let mut out = vec![9.0; 256];
        let outcome = ring.pull_interleaved(&mut out);
        assert!(outcome.priming);
        assert!(out.iter().all(|&s| s == 0.0));
        assert!(ring.is_priming(), "one frame short of target keeps priming");
        assert_eq!(ring.fill(), TARGET_FRAMES - 1, "priming consumes nothing");
    }

    #[test]
    fn frames_come_out_in_order_with_channels_intact() {
        let ring = primed_ring();
        let mut out = vec![0.0; 8];
        let outcome = ring.pull_interleaved(&mut out);
        assert_eq!(outcome, PullOutcome { priming: false, underrun: false, dropped: false, duplicated: false });
        assert_eq!(out, vec![1.0, -1.0, 2.0, -2.0, 3.0, -3.0, 4.0, -4.0]);
    }

    #[test]
    fn underrun_pads_silence_counts_and_reprimes() {
        let ring = primed_ring();
        let mut out = vec![7.0; (TARGET_FRAMES + 10) * 2];
        let outcome = ring.pull_interleaved(&mut out);
        assert!(outcome.underrun);
        assert!(out[TARGET_FRAMES * 2..].iter().all(|&s| s == 0.0));
        assert_eq!(ring.underruns.load(Ordering::Relaxed), 1);
        assert!(ring.is_priming(), "an underrun re-primes rather than stutter");
    }

    #[test]
    fn overflow_is_counted_and_never_overwrites_unread_audio() {
        let ring = CueRing::new(RING_CAPACITY_FRAMES);
        ring.push_interleaved(&stereo(RING_CAPACITY_FRAMES + 5, 1.0));
        assert_eq!(ring.fill(), RING_CAPACITY_FRAMES - 1, "one slot stays empty");
        assert_eq!(ring.overflows.load(Ordering::Relaxed), 6);
    }

    #[test]
    fn a_consistently_full_ring_drops_one_frame_per_callback_and_no_more() {
        let ring = primed_ring();
        // Keep the producer ahead: fill sits well above the band every call.
        let mut drops = 0;
        let mut out = vec![0.0; 512];
        for _ in 0..2000 {
            ring.push_interleaved(&stereo(256, 0.0));
            let outcome = ring.pull_interleaved(&mut out);
            assert!(!outcome.duplicated, "a full ring never duplicates");
            if outcome.dropped {
                drops += 1;
            }
            if ring.fill() < TARGET_FRAMES + DRIFT_BAND_FRAMES + 512 {
                ring.push_interleaved(&stereo(512, 0.0));
            }
        }
        assert!(drops > 0, "sustained over-fill must be corrected");
        assert!(drops <= 2000, "at most one frame per callback");
    }

    #[test]
    fn a_draining_ring_duplicates_by_peeking_not_consuming() {
        let ring = primed_ring();
        // Pull steadily without refilling below the target: the smoothed fill
        // falls under the band and the policy starts repeating a frame.
        let mut out = vec![0.0; 64];
        let mut saw_duplicate = false;
        for _ in 0..40 {
            let before = ring.fill();
            let outcome = ring.pull_interleaved(&mut out);
            if outcome.duplicated {
                saw_duplicate = true;
                assert_eq!(out[0], out[2], "slot 0 repeats the frame slot 1 reads");
                assert_eq!(before - ring.fill(), 31, "a duplicate consumes one frame fewer");
                break;
            }
        }
        assert!(saw_duplicate, "sustained under-fill must be corrected");
    }

    #[test]
    fn burst_jitter_around_the_target_does_not_trigger_corrections() {
        let ring = primed_ring();
        let mut out = vec![0.0; 256];
        // Drain to target - 512 first, then 1024-frame bursts every eighth
        // 128-frame pull: the instantaneous fill swings 1536..2560, past both
        // band edges every cycle, while the average sits on the target. Only
        // the smoothed fill keeps the policy from "correcting" that swing.
        let mut drain = vec![0.0; 1024];
        ring.pull_interleaved(&mut drain);
        assert_eq!(ring.fill(), TARGET_FRAMES - 512);
        for i in 0..4000 {
            if i % 8 == 0 {
                ring.push_interleaved(&stereo(1024, 0.0));
            }
            let outcome = ring.pull_interleaved(&mut out);
            assert!(!outcome.dropped && !outcome.duplicated, "call {i} corrected on jitter alone");
        }
    }

    #[test]
    fn reset_forgets_buffered_audio_and_reprimes() {
        let ring = primed_ring();
        ring.reset();
        assert_eq!(ring.fill(), 0);
        assert!(ring.is_priming());
    }

    #[test]
    fn commands_parse_and_reject_loudly() {
        assert_eq!(parse_command(r#"{"type":"list","id":3}"#).unwrap(), (Some(3), Command::List));
        assert_eq!(
            parse_command(r#"{"type":"open","uid":"BuiltInSpeakerDevice","sample_rate":48000}"#).unwrap(),
            (None, Command::Open { uid: "BuiltInSpeakerDevice".into(), sample_rate: 48_000.0 })
        );
        assert!(parse_command(r#"{"type":"open","uid":"x"}"#).is_err(), "missing rate");
        assert!(parse_command(r#"{"type":"open","uid":"x","sample_rate":1}"#).is_err(), "absurd rate");
        assert!(parse_command(r#"{"type":"set_master","uid":"  "}"#).is_err(), "blank uid");
        assert!(parse_command(r#"{"type":"rm_rf"}"#).is_err(), "unknown type");
        assert!(parse_command("not json").is_err());
    }

    #[test]
    fn pcm_decodes_little_endian_stereo_and_refuses_partial_frames() {
        let mut scratch = Vec::new();
        let bytes: Vec<u8> = [0.5f32, -0.25].iter().flat_map(|s| s.to_le_bytes()).collect();
        decode_pcm(&bytes, &mut scratch).unwrap();
        assert_eq!(scratch, vec![0.5, -0.25]);
        assert!(decode_pcm(&bytes[..4], &mut scratch).is_err(), "half a frame");
    }

    #[test]
    fn handshake_needs_path_token_and_a_loopback_origin() {
        let ok = handshake_allowed("/cue?token=abc", Some("http://127.0.0.1:51234"), "abc");
        assert!(ok.is_ok(), "{ok:?}");
        assert!(handshake_allowed("/cue?token=abc", Some("tauri://localhost"), "abc").is_ok());
        assert!(handshake_allowed("/cue?token=abd", Some("http://127.0.0.1:1"), "abc").is_err(), "wrong token");
        assert!(handshake_allowed("/cue", Some("http://127.0.0.1:1"), "abc").is_err(), "no token");
        assert!(handshake_allowed("/other?token=abc", Some("http://127.0.0.1:1"), "abc").is_err(), "path");
        assert!(handshake_allowed("/cue?token=abc", None, "abc").is_err(), "no origin");
        assert!(handshake_allowed("/cue?token=abc", Some("https://evil.example"), "abc").is_err());
        assert!(
            handshake_allowed("/cue?token=abc", Some("http://127.0.0.1.evil.example"), "abc").is_err(),
            "a prefix match is not a loopback origin"
        );
    }

    fn device(uid: &str, is_default: bool) -> OutputDevice {
        OutputDevice {
            uid: uid.into(),
            name: uid.into(),
            channels: 2,
            transport: "other",
            is_default,
            builtin_role: BuiltinRole::NotBuiltin,
            data_source: None,
            jack_connected: None,
        }
    }

    const SPK: &str = "BuiltInSpeakerDevice";
    const JACK: &str = "BuiltInHeadphoneOutputDevice";

    fn builtin(uid: &str, role: BuiltinRole, is_default: bool) -> OutputDevice {
        OutputDevice { transport: "builtin", builtin_role: role, ..device(uid, is_default) }
    }

    /// silver, Tue 6 Oct 2026: wired headphones in the MacBook's own jack, an
    /// HDMI display and a virtual device. `default` names the macOS default.
    fn silver(default: &str) -> Vec<OutputDevice> {
        vec![
            builtin(SPK, BuiltinRole::Speaker, default == SPK),
            builtin(JACK, BuiltinRole::Headphones, default == JACK),
            device("lg", default == "lg"),
            device("blackhole", default == "blackhole"),
        ]
    }

    #[test]
    fn builtin_role_reads_the_data_source_first_and_the_uid_only_as_a_fallback() {
        let ispk = Some(crate::output_health::fourcc(b"ispk"));
        let hdpn = Some(crate::output_health::fourcc(b"hdpn"));
        assert_eq!(builtin_role("builtin", ispk, "whatever"), BuiltinRole::Speaker);
        assert_eq!(builtin_role("builtin", hdpn, "BuiltInSpeakerDevice"), BuiltinRole::Headphones, "data source wins");
        assert_eq!(builtin_role("builtin", None, SPK), BuiltinRole::Speaker);
        assert_eq!(builtin_role("builtin", None, JACK), BuiltinRole::Headphones);
        assert_eq!(builtin_role("usb", hdpn, JACK), BuiltinRole::NotBuiltin, "only built-in hardware shares the jack");
        assert_eq!(builtin_role("builtin", Some(crate::output_health::fourcc(b"line")), SPK), BuiltinRole::NotBuiltin);
    }

    #[test]
    fn the_speakers_are_muted_and_merge_into_the_jack_only_while_the_jack_is_occupied() {
        let occupied = silver(SPK);
        assert!(muted_by_jack(&occupied[0], &occupied));
        assert_eq!(physical_output_uid(SPK, &occupied), JACK);
        assert_eq!(physical_output_uid(JACK, &occupied), JACK);
        assert_eq!(physical_output_uid("lg", &occupied), "lg");
        // Controls: no headphone device, or one that says its jack is empty.
        let empty = vec![builtin(SPK, BuiltinRole::Speaker, true), device("lg", false)];
        assert!(!muted_by_jack(&empty[0], &empty));
        assert_eq!(physical_output_uid(SPK, &empty), SPK);
        let mut unplugged = silver(SPK);
        unplugged[1].jack_connected = Some(false);
        assert!(!muted_by_jack(&unplugged[0], &unplugged), "a jack that reports empty mutes nothing");
        // An Intel Mac lists one built-in device; nothing is muted by it.
        let intel = vec![builtin("BuiltInOutput", BuiltinRole::Headphones, true)];
        assert!(!muted_by_jack(&intel[0], &intel));
    }

    #[test]
    fn devices_json_carries_the_mute_and_the_physical_output() {
        let listed = devices_json(&silver(JACK));
        assert_eq!(listed[0]["muted_by_jack"], json!(true));
        assert_eq!(listed[0]["physical_uid"], json!(JACK));
        assert_eq!(listed[0]["builtin_role"], json!("speaker"));
        assert_eq!(listed[1]["muted_by_jack"], json!(false));
        assert_eq!(listed[2]["builtin_role"], Value::Null);
    }

    #[test]
    fn set_master_refuses_the_jack_muted_speakers_and_the_cue_device_itself() {
        let devices = silver(JACK);
        let shared_pair = master_pin_refusal(SPK, Some(JACK), &devices).expect("built-in pair refused");
        assert_eq!(shared_pair.kind(), "muted_by_jack");
        assert!(shared_pair.message().contains("can't share the MacBook's built-in output"), "{}", shared_pair.message());
        let no_cue = master_pin_refusal(SPK, None, &devices).expect("muted speakers refused without a cue too");
        assert!(no_cue.message().contains("muted while headphones"), "{}", no_cue.message());
        assert_eq!(master_pin_refusal(JACK, Some(JACK), &devices).map(|f| f.kind()), Some("shares_cue"));
        // Controls: genuinely different devices, the jack itself with no cue,
        // and the speakers once the jack is empty are all pinnable.
        assert_eq!(master_pin_refusal("lg", Some(JACK), &devices), None);
        assert_eq!(master_pin_refusal(JACK, None, &devices), None);
        assert_eq!(master_pin_refusal(JACK, Some("lg"), &devices), None);
        let empty = vec![builtin(SPK, BuiltinRole::Speaker, true), device("usb-phones", false)];
        assert_eq!(master_pin_refusal(SPK, Some("usb-phones"), &empty), None);
    }

    #[test]
    fn the_jack_muted_pin_is_released_never_reasserted() {
        // The build-10 loop: MASTER pinned on the speakers, cue on the jack,
        // macOS puts the default on the jack. Old policy: put back, forever.
        let decision = master_pin_decision(Some(SPK), Some(JACK), &silver(JACK), None);
        match decision {
            PinDecision::Release(MasterFault::MutedByJack { cue_on_jack: true, .. }) => {}
            other => panic!("expected a muted_by_jack release, got {other:?}"),
        }
        // Even while the speakers are still the default, a muted pin is a fault.
        assert!(matches!(master_pin_decision(Some(SPK), None, &silver(SPK), None), PinDecision::Release(_)));
    }

    #[test]
    fn control_genuinely_distinct_devices_keep_the_cueout_09_reassert() {
        // MASTER on the HDMI display, cue on the jack: plugging in moved the
        // default to the jack. Putting the room back is the P0 protection.
        assert_eq!(master_pin_decision(Some("lg"), Some(JACK), &silver(JACK), None), PinDecision::Reassert("lg".into()));
        let speakers_and_usb = vec![builtin(SPK, BuiltinRole::Speaker, false), device("usb-phones", true)];
        assert_eq!(
            master_pin_decision(Some(SPK), Some("usb-phones"), &speakers_and_usb, None),
            PinDecision::Reassert(SPK.into()),
            "speakers are not muted by USB headphones"
        );
        // A re-assert long ago does not count against a fresh move.
        assert_eq!(
            master_pin_decision(Some("lg"), Some(JACK), &silver(JACK), Some(REASSERT_FIGHT_WINDOW * 3)),
            PinDecision::Reassert("lg".into())
        );
    }

    #[test]
    fn a_second_move_inside_the_window_releases_the_pin_instead_of_fighting() {
        let decision = master_pin_decision(Some("lg"), Some(JACK), &silver(JACK), Some(Duration::from_secs(1)));
        assert_eq!(
            decision,
            PinDecision::Release(MasterFault::OverriddenBySystem { uid: "lg".into(), default_uid: Some(JACK.into()) })
        );
        if let PinDecision::Release(fault) = decision {
            assert!(fault.message().starts_with("master pin overridden by system"), "{}", fault.message());
        }
    }

    #[test]
    fn the_watcher_holds_when_there_is_nothing_to_protect() {
        assert_eq!(master_pin_decision(Some("lg"), Some(JACK), &silver("lg"), None), PinDecision::Hold, "default is the pin");
        assert_eq!(master_pin_decision(None, Some(JACK), &silver(JACK), None), PinDecision::Hold, "no pin: macOS decides");
        assert_eq!(
            master_pin_decision(Some("gone"), Some(JACK), &silver(JACK), None),
            PinDecision::Hold,
            "vanished pin: nothing to restore"
        );
        assert_eq!(
            master_pin_decision(Some("lg"), None, &silver(JACK), None),
            PinDecision::Hold,
            "no cue open: the operator's own Sound-settings change stands"
        );
    }

    /// Simulates the 1 s housekeeping loop against an adversary that moves the
    /// default off the pin every tick, and counts re-asserts. Bounded, not a loop.
    #[test]
    fn an_insistent_default_change_costs_at_most_one_reassert() {
        let mut pinned = Some("lg".to_string());
        let mut last_reassert: Option<Duration> = None;
        let mut reasserts = 0;
        let mut released = false;
        for tick in 0..60u64 {
            let now = Duration::from_secs(tick);
            let since = last_reassert.map(|at| now - at);
            match master_pin_decision(pinned.as_deref(), Some(JACK), &silver(JACK), since) {
                PinDecision::Reassert(_) => {
                    reasserts += 1;
                    last_reassert = Some(now);
                }
                PinDecision::Release(_) => {
                    pinned = None;
                    released = true;
                }
                PinDecision::Hold => {}
            }
        }
        assert_eq!(reasserts, 1, "one re-assert, then follow");
        assert!(released, "the fight ends in a reported release");
    }

    /// Hardware check, run by hand on a Mac (`cargo test -- --ignored --nocapture`):
    /// a MacBook's own speakers must classify as `Speaker` through the real
    /// data-source read. Positive control for the classifier on hardware; with
    /// wired headphones in the jack it must also print the speakers as muted.
    #[test]
    #[ignore = "needs real CoreAudio hardware"]
    #[cfg(target_os = "macos")]
    fn live_listing_classifies_the_builtin_speakers() {
        let devices = list_output_devices().expect("CoreAudio output listing");
        println!("{}", serde_json::to_string_pretty(&devices_json(&devices)).unwrap());
        assert!(
            devices.iter().any(|d| d.builtin_role == BuiltinRole::Speaker),
            "no built-in speaker classified: the data-source read or the UID fallback is broken"
        );
    }

    #[test]
    fn device_signature_tracks_membership_and_default_not_names() {
        let a = vec![device("a", true), device("b", false)];
        let mut renamed = a.clone();
        renamed[1].name = "B renamed".into();
        assert_eq!(device_signature(&a), device_signature(&renamed));
        assert_ne!(device_signature(&a), device_signature(&[device("a", false), device("b", true)]));
        assert_ne!(device_signature(&a), device_signature(&[device("a", true)]));
    }

    #[test]
    fn init_script_names_a_loopback_socket_and_a_token() {
        let server = CueSinkServer { port: 4321, token: "t0k".into() };
        let script = server.init_script().unwrap();
        assert!(script.starts_with("globalThis.OPENDJ_CUE_SINK = "), "{script}");
        assert!(script.contains("ws://127.0.0.1:4321/cue"), "{script}");
        assert!(script.contains("\"token\":\"t0k\""), "{script}");
    }

    #[test]
    fn tokens_differ_between_calls() {
        assert_ne!(new_token(), new_token());
        assert_eq!(new_token().len(), 32);
    }
}
