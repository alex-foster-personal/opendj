//! Bluetooth main-speaker stall: the shell-side link check and the reconnect.
//!
//! AUDIO-DEVICE-03 / AUDIO-DEVICE-05. Tue 3 Nov 2026, silver: playing a file in
//! QuickTime left BOTH QuickTime and Open DJ silent on a HYPERBOOM Bluetooth
//! speaker, while macOS still listed HYPERBOOM as the default output at 44.1 kHz
//! and `GET /api/v1/audio/output-health` said `verdict: ok`. The IO-cycle check
//! of AUDIO-DEVICE-01 counts the HAL running, and the HAL keeps running into a
//! dead Bluetooth link, so that check alone cannot see this stall.
//!
//! This module adds three HAL reads on the MAIN (default) output and judges
//! them together: `kAudioDevicePropertyDeviceIsAlive`,
//! `kAudioDevicePropertyDeviceIsRunningSomewhere`, and whether the device's
//! IO-proc sample time advances at the nominal rate against host time. Whether
//! any of them actually moves during a real HYPERBOOM stall is UNPROVEN: the
//! acoustic proof in the webview (AUDIO-DEVICE-04) is the check that hears the
//! room. The recovery is the operator's manual fix, automated: disconnect and
//! reconnect the speaker's Bluetooth address with blueutil, then make it the
//! default output again.

pub const RECONNECT_SPEAKER_PATH: &str = "/api/v1/audio/reconnect-speaker";
pub const INPUT_DEVICES_PATH: &str = "/api/v1/audio/input-devices";

/// AUDIO-DEVICE-04: the only input transports the acoustic proof may open.
/// Opening a Bluetooth input flips a headset into the HFP call profile and
/// wrecks the very output being checked, so Bluetooth is never on this list.
pub const ACOUSTIC_MIC_TRANSPORTS: [&str; 3] = ["usb", "builtin", "thunderbolt"];

/// Homebrew's blueutil on Apple Silicon. Fixed on purpose: a PATH lookup from
/// a GUI app sees launchd's PATH, which has no Homebrew, and a silent miss is
/// the failure this module exists to remove.
pub const BLUEUTIL_PATH: &str = "/opt/homebrew/bin/blueutil";

/// Seconds blueutil waits for the link to drop, then to come back.
pub const BLUEUTIL_WAIT_DISCONNECT_S: u32 = 5;
pub const BLUEUTIL_WAIT_CONNECT_S: u32 = 15;

/// How long the reconnected speaker gets to reappear in CoreAudio.
pub const DEVICE_REAPPEAR_TIMEOUT_MS: u64 = 10_000;

/// The IO-proc sample clock must advance between these fractions of its
/// nominal rate against host time. A healthy device reads about 1.0; a
/// frozen sample time reads 0.
pub const CLOCK_RATIO_MIN: f64 = 0.5;
pub const CLOCK_RATIO_MAX: f64 = 1.5;

/// A CoreAudio Bluetooth UID carries the MAC first: `10-94-97-1C-C6-43:output`
/// (HYPERBOOM on silver). Returns the address in blueutil's dashed lowercase
/// form, or `None` for anything that is not six hex pairs.
pub fn bluetooth_address_from_uid(uid: &str) -> Option<String> {
    let head = uid.split(':').next()?;
    let pairs: Vec<&str> = head.split('-').collect();
    let valid = pairs.len() == 6
        && pairs.iter().all(|pair| pair.len() == 2 && pair.chars().all(|c| c.is_ascii_hexdigit()));
    valid.then(|| pairs.join("-").to_ascii_lowercase())
}

/// Advance of the device sample clock as a fraction of its nominal rate.
/// `None` when there is no host-time span to measure over.
pub fn sample_clock_ratio(sample_delta: f64, host_delta_s: f64, nominal_rate: f64) -> Option<f64> {
    if !(host_delta_s > 0.0) || !(nominal_rate > 0.0) || !sample_delta.is_finite() {
        return None;
    }
    Some(sample_delta / (host_delta_s * nominal_rate))
}

/// What the shell read off the main device during one probe.
#[derive(Debug, Clone, PartialEq)]
pub struct ShellLinkCheck {
    pub is_alive: bool,
    pub running_somewhere: bool,
    /// IO-proc callbacks seen during the probe window.
    pub io_cycles: u64,
    /// `None` when fewer than two callbacks arrived.
    pub clock_ratio: Option<f64>,
}

/// The first reason the main device is not delivering, or `None` when every
/// shell check passes. `running_somewhere` is reported, not judged here: an
/// idle device legitimately is not running, and the probe's own IO proc runs
/// it during the window.
pub fn shell_link_fault(check: &ShellLinkCheck) -> Option<String> {
    if !check.is_alive {
        return Some("main output reports DeviceIsAlive = 0".into());
    }
    if check.io_cycles < 2 {
        return Some(format!("main output IO proc ran {} time(s) in the probe window", check.io_cycles));
    }
    match check.clock_ratio {
        None => Some("main output sample clock could not be measured against host time".into()),
        Some(ratio) if !(CLOCK_RATIO_MIN..=CLOCK_RATIO_MAX).contains(&ratio) => Some(format!(
            "main output sample clock advanced at {ratio:.2}x its nominal rate against host time"
        )),
        Some(_) => None,
    }
}

/// The blueutil argv steps of one reconnect, in order.
pub fn blueutil_steps(address: &str) -> Vec<Vec<String>> {
    vec![
        vec!["--disconnect".into(), address.into()],
        vec!["--wait-disconnect".into(), address.into(), BLUEUTIL_WAIT_DISCONNECT_S.to_string()],
        vec!["--connect".into(), address.into()],
        vec!["--wait-connect".into(), address.into(), BLUEUTIL_WAIT_CONNECT_S.to_string()],
    ]
}

/// Whether the acoustic proof may open an input on `transport`.
pub fn acoustic_mic_allowed(transport: &str) -> bool {
    ACOUSTIC_MIC_TRANSPORTS.contains(&transport)
}

/// JSON body for `GET /api/v1/audio/input-devices`: every input with its
/// transport and whether the acoustic proof may open it.
pub fn input_devices_json() -> String {
    match platform::list_inputs() {
        Ok(inputs) => serde_json::json!({
            "inputs": inputs
                .into_iter()
                .map(|(uid, name, transport, channels)| serde_json::json!({
                    "uid": uid,
                    "name": name,
                    "transport": transport,
                    "channels": channels,
                    "acoustic_proof_allowed": acoustic_mic_allowed(transport),
                }))
                .collect::<Vec<_>>(),
            "error": null,
        })
        .to_string(),
        Err(error) => serde_json::json!({ "inputs": [], "error": error }).to_string(),
    }
}

/// JSON body for `POST /api/v1/audio/reconnect-speaker`.
pub fn reconnect_speaker_json() -> String {
    match platform::reconnect_speaker() {
        Ok(body) => body.to_string(),
        Err(error) => serde_json::json!({ "reconnected": false, "error": error }).to_string(),
    }
}

#[cfg(target_os = "macos")]
mod platform {
    use super::{
        bluetooth_address_from_uid, blueutil_steps, BLUEUTIL_PATH, DEVICE_REAPPEAR_TIMEOUT_MS,
    };
    use crate::cue_sink::list_output_devices;
    use crate::cue_sink::platform::{get_scoped, transport_name, K_AUDIO_DEVICE_PROPERTY_TRANSPORT_TYPE};
    use crate::output_health::platform::{
        channel_count, get_cf_string, get_device_list, set_default_output_device, K_AUDIO_DEVICE_PROPERTY_DEVICE_UID,
        K_AUDIO_OBJECT_PROPERTY_NAME, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL, K_AUDIO_OBJECT_PROPERTY_SCOPE_INPUT,
    };

    /// (uid, name, transport, input channels) for every device with inputs.
    pub fn list_inputs() -> Result<Vec<(String, String, &'static str, u32)>, String> {
        let mut inputs = Vec::new();
        for id in get_device_list()? {
            let channels = channel_count(id, K_AUDIO_OBJECT_PROPERTY_SCOPE_INPUT).unwrap_or(0);
            if channels == 0 {
                continue;
            }
            let Ok(uid) = get_cf_string(id, K_AUDIO_DEVICE_PROPERTY_DEVICE_UID) else { continue };
            let name = get_cf_string(id, K_AUDIO_OBJECT_PROPERTY_NAME).unwrap_or_else(|_| uid.clone());
            // An unreadable transport is "other", which the proof refuses.
            let transport = get_scoped::<u32>(id, K_AUDIO_DEVICE_PROPERTY_TRANSPORT_TYPE, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL)
                .map(transport_name)
                .unwrap_or("other");
            inputs.push((uid, name, transport, channels));
        }
        Ok(inputs)
    }
    use std::path::Path;
    use std::process::Command;
    use std::thread;
    use std::time::{Duration, Instant};

    fn run_blueutil(args: &[String]) -> Result<(), String> {
        let output = Command::new(BLUEUTIL_PATH)
            .args(args)
            .output()
            .map_err(|error| format!("blueutil {} could not start: {error}", args.join(" ")))?;
        if !output.status.success() {
            return Err(format!(
                "blueutil {} failed ({}): {}",
                args.join(" "),
                output.status,
                String::from_utf8_lossy(&output.stderr).trim()
            ));
        }
        Ok(())
    }

    fn device_id_for_uid(uid: &str) -> Result<Option<u32>, String> {
        Ok(get_device_list()?
            .into_iter()
            .find(|id| get_cf_string(*id, K_AUDIO_DEVICE_PROPERTY_DEVICE_UID).ok().as_deref() == Some(uid)))
    }

    fn wait_for_device(uid: &str) -> Result<u32, String> {
        let deadline = Instant::now() + Duration::from_millis(DEVICE_REAPPEAR_TIMEOUT_MS);
        loop {
            if let Some(id) = device_id_for_uid(uid)? {
                return Ok(id);
            }
            if Instant::now() >= deadline {
                return Err(format!(
                    "Bluetooth reconnected but CoreAudio did not list {uid} within {DEVICE_REAPPEAR_TIMEOUT_MS} ms"
                ));
            }
            thread::sleep(Duration::from_millis(250));
        }
    }

    pub fn reconnect_speaker() -> Result<serde_json::Value, String> {
        let started = Instant::now();
        let devices = list_output_devices()?;
        let main = devices
            .iter()
            .find(|device| device.is_default)
            .ok_or("no default (main) output device to reconnect")?;
        if main.transport != "bluetooth" {
            return Err(format!(
                "main output {} is {}, not Bluetooth: there is no speaker link to reconnect",
                main.name, main.transport
            ));
        }
        let address = bluetooth_address_from_uid(&main.uid)
            .ok_or_else(|| format!("main output uid {} carries no Bluetooth address", main.uid))?;
        if !Path::new(BLUEUTIL_PATH).exists() {
            return Err(format!("blueutil not found at {BLUEUTIL_PATH}; install it with `brew install blueutil`"));
        }
        for step in blueutil_steps(&address) {
            run_blueutil(&step)?;
        }
        let device = wait_for_device(&main.uid)?;
        set_default_output_device(device)?;
        Ok(serde_json::json!({
            "reconnected": true,
            "device_name": main.name,
            "device_uid": main.uid,
            "bluetooth_address": address,
            "elapsed_ms": started.elapsed().as_millis() as u64,
        }))
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    pub fn list_inputs() -> Result<Vec<(String, String, &'static str, u32)>, String> {
        Err("installed macOS shell required to list CoreAudio inputs".into())
    }

    pub fn reconnect_speaker() -> Result<serde_json::Value, String> {
        Err("installed macOS shell required to reconnect a Bluetooth speaker".into())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn hyperboom_uid_yields_its_bluetooth_address() {
        assert_eq!(
            bluetooth_address_from_uid("10-94-97-1C-C6-43:output").as_deref(),
            Some("10-94-97-1c-c6-43"),
            "if the silver HYPERBOOM uid does not parse then reconnect is broken"
        );
        assert_eq!(bluetooth_address_from_uid("CC-98-8B-4A-58-E3:input").as_deref(), Some("cc-98-8b-4a-58-e3"));
        assert_eq!(bluetooth_address_from_uid("CC-98-8B-4A-58-E3").as_deref(), Some("cc-98-8b-4a-58-e3"));
    }

    #[test]
    fn non_bluetooth_uids_have_no_address() {
        for uid in ["BuiltInSpeakerDevice", "BlackHole2ch_UID", "1E6D829E-0000-0000-0722-0104B5462778", "", "ZZ-98-8B-4A-58-E3:output"] {
            assert_eq!(bluetooth_address_from_uid(uid), None, "if {uid:?} parses as an address then reconnect is broken");
        }
    }

    #[test]
    fn clock_ratio_reads_one_for_a_healthy_device_and_zero_for_a_frozen_one() {
        let healthy = sample_clock_ratio(44_100.0 * 0.2, 0.2, 44_100.0).expect("span");
        assert!((healthy - 1.0).abs() < 1e-9);
        assert_eq!(sample_clock_ratio(0.0, 0.2, 44_100.0), Some(0.0));
        assert_eq!(sample_clock_ratio(100.0, 0.0, 44_100.0), None);
        assert_eq!(sample_clock_ratio(100.0, 0.2, 0.0), None);
    }

    fn healthy() -> ShellLinkCheck {
        ShellLinkCheck { is_alive: true, running_somewhere: true, io_cycles: 18, clock_ratio: Some(1.0) }
    }

    #[test]
    fn a_healthy_main_device_has_no_shell_fault() {
        assert_eq!(shell_link_fault(&healthy()), None);
        // running_somewhere is reported, never judged: idle is not a stall.
        assert_eq!(shell_link_fault(&ShellLinkCheck { running_somewhere: false, ..healthy() }), None);
    }

    #[test]
    fn each_failed_shell_check_is_a_fault() {
        let dead = ShellLinkCheck { is_alive: false, ..healthy() };
        assert!(shell_link_fault(&dead).expect("dead").contains("DeviceIsAlive"), "if a dead device is ok then detect is broken");
        let silent = ShellLinkCheck { io_cycles: 1, clock_ratio: None, ..healthy() };
        assert!(shell_link_fault(&silent).expect("silent").contains("IO proc"));
        let frozen = ShellLinkCheck { clock_ratio: Some(0.0), ..healthy() };
        assert!(shell_link_fault(&frozen).expect("frozen").contains("0.00x"), "if a frozen sample clock is ok then detect is broken");
        let racing = ShellLinkCheck { clock_ratio: Some(2.0), ..healthy() };
        assert!(shell_link_fault(&racing).is_some());
        let unmeasured = ShellLinkCheck { clock_ratio: None, ..healthy() };
        assert!(shell_link_fault(&unmeasured).is_some());
    }

    #[test]
    fn reconnect_disconnects_waits_then_connects_and_waits() {
        let steps = blueutil_steps("10-94-97-1c-c6-43");
        let verbs: Vec<&str> = steps.iter().map(|step| step[0].as_str()).collect();
        assert_eq!(verbs, ["--disconnect", "--wait-disconnect", "--connect", "--wait-connect"]);
        assert!(steps.iter().all(|step| step[1] == "10-94-97-1c-c6-43"));
    }

    #[test]
    fn the_acoustic_proof_never_opens_a_bluetooth_input() {
        assert!(!acoustic_mic_allowed("bluetooth"), "if a Bluetooth mic is allowed then HFP wrecks the speaker under test");
        for refused in ["airplay", "virtual", "aggregate", "other", "display"] {
            assert!(!acoustic_mic_allowed(refused), "{refused} must be refused");
        }
        for wired in ["usb", "builtin", "thunderbolt"] {
            assert!(acoustic_mic_allowed(wired), "if a {wired} mic is refused then the MX Brio cannot prove sound");
        }
    }

    #[test]
    fn blueutil_path_is_the_fixed_homebrew_path() {
        assert_eq!(BLUEUTIL_PATH, "/opt/homebrew/bin/blueutil");
    }
}
