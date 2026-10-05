//! Native MIDI transport for the installed desktop shell.
//!
//! Chrome uses Web MIDI directly. WKWebView does not expose that API, so the
//! macOS shell carries the same raw messages over CoreMIDI (through `midir`)
//! and leaves mapping, decoding, learn logging, and product behavior in the
//! existing TypeScript runtime. The bridge is deliberately transport-only.

#[cfg(target_os = "macos")]
pub(crate) mod platform {
    use std::collections::{HashMap, HashSet};
    use std::sync::Mutex;

    use midir::{Ignore, MidiInput, MidiInputConnection, MidiOutput, MidiOutputConnection};
    use serde::Serialize;
    use tauri::{AppHandle, Emitter, State};

    const MIDI_EVENT: &str = "opendj-native-midi-message";

    #[derive(Clone, Debug, Serialize)]
    #[serde(rename_all = "camelCase")]
    pub struct NativeMidiDevice {
        id: String,
        name: String,
        manufacturer: String,
        has_output: bool,
    }

    #[derive(Clone, Debug, Serialize)]
    #[serde(rename_all = "camelCase")]
    struct NativeMidiMessage {
        device_id: String,
        device_name: String,
        timestamp_micros: u64,
        data: Vec<u8>,
    }

    #[derive(Clone, Debug)]
    struct PortIdentity {
        name: String,
        occurrence: usize,
        has_output: bool,
    }

    #[derive(Default)]
    struct BridgeInner {
        devices: HashMap<String, PortIdentity>,
        inputs: HashMap<String, MidiInputConnection<()>>,
        outputs: HashMap<String, MidiOutputConnection>,
    }

    #[derive(Default)]
    pub struct NativeMidiBridge(Mutex<BridgeInner>);

    fn bridge_error(context: &str, err: impl std::fmt::Display) -> String {
        format!("{context}: {err}")
    }

    fn input_inventory() -> Result<Vec<(String, usize)>, String> {
        let input = MidiInput::new("Open DJ MIDI discovery")
            .map_err(|err| bridge_error("CoreMIDI input discovery failed", err))?;
        let mut occurrences: HashMap<String, usize> = HashMap::new();
        input
            .ports()
            .iter()
            .map(|port| {
                let name = input
                    .port_name(port)
                    .map_err(|err| bridge_error("CoreMIDI input name failed", err))?;
                let occurrence = occurrences.entry(name.clone()).or_insert(0);
                let item = (name, *occurrence);
                *occurrence += 1;
                Ok(item)
            })
            .collect()
    }

    fn output_inventory() -> Result<Vec<(String, usize)>, String> {
        let output = MidiOutput::new("Open DJ MIDI discovery")
            .map_err(|err| bridge_error("CoreMIDI output discovery failed", err))?;
        let mut occurrences: HashMap<String, usize> = HashMap::new();
        output
            .ports()
            .iter()
            .map(|port| {
                let name = output
                    .port_name(port)
                    .map_err(|err| bridge_error("CoreMIDI output name failed", err))?;
                let occurrence = occurrences.entry(name.clone()).or_insert(0);
                let item = (name, *occurrence);
                *occurrence += 1;
                Ok(item)
            })
            .collect()
    }

    fn device_id(name: &str, occurrence: usize) -> String {
        format!("coremidi:{name}:{occurrence}")
    }

    fn open_input(
        app: AppHandle,
        id: String,
        name: String,
        occurrence: usize,
    ) -> Result<MidiInputConnection<()>, String> {
        let mut input = MidiInput::new("Open DJ MIDI input")
            .map_err(|err| bridge_error("CoreMIDI input initialization failed", err))?;
        input.ignore(Ignore::None);
        let matching = input
            .ports()
            .into_iter()
            .filter(|port| input.port_name(port).ok().as_deref() == Some(name.as_str()))
            .nth(occurrence)
            .ok_or_else(|| format!("CoreMIDI input vanished before connection: {name}"))?;
        let callback_id = id.clone();
        let callback_name = name.clone();
        input
            .connect(
                &matching,
                &format!("Open DJ - {name}"),
                move |timestamp_micros, data, _| {
                    if data.is_empty() {
                        return;
                    }
                    let payload = NativeMidiMessage {
                        device_id: callback_id.clone(),
                        device_name: callback_name.clone(),
                        timestamp_micros,
                        data: data.to_vec(),
                    };
                    if let Err(err) = app.emit_to("main", MIDI_EVENT, payload) {
                        eprintln!("[native-midi] could not deliver input event: {err}");
                    }
                },
                (),
            )
            .map_err(|err| bridge_error(&format!("CoreMIDI could not open input {name}"), err))
    }

    fn rescan(app: &AppHandle, inner: &mut BridgeInner) -> Result<Vec<NativeMidiDevice>, String> {
        let inputs = input_inventory()?;
        let outputs: HashSet<(String, usize)> = output_inventory()?.into_iter().collect();
        let present_ids: HashSet<String> = inputs
            .iter()
            .map(|(name, occurrence)| device_id(name, *occurrence))
            .collect();

        inner.inputs.retain(|id, _| present_ids.contains(id));
        inner.outputs.retain(|id, _| present_ids.contains(id));
        inner.devices.retain(|id, _| present_ids.contains(id));

        let mut snapshot = Vec::with_capacity(inputs.len());
        for (name, occurrence) in inputs {
            let id = device_id(&name, occurrence);
            let has_output = outputs.contains(&(name.clone(), occurrence));
            inner.devices.insert(
                id.clone(),
                PortIdentity {
                    name: name.clone(),
                    occurrence,
                    has_output,
                },
            );
            if !inner.inputs.contains_key(&id) {
                let connection = open_input(app.clone(), id.clone(), name.clone(), occurrence)?;
                inner.inputs.insert(id.clone(), connection);
            }
            snapshot.push(NativeMidiDevice {
                id,
                name,
                manufacturer: "CoreMIDI".to_string(),
                has_output,
            });
        }
        snapshot.sort_by(|left, right| left.id.cmp(&right.id));
        Ok(snapshot)
    }

    fn open_output(identity: &PortIdentity) -> Result<MidiOutputConnection, String> {
        let output = MidiOutput::new("Open DJ MIDI output")
            .map_err(|err| bridge_error("CoreMIDI output initialization failed", err))?;
        let matching = output
            .ports()
            .into_iter()
            .filter(|port| output.port_name(port).ok().as_deref() == Some(identity.name.as_str()))
            .nth(identity.occurrence)
            .ok_or_else(|| {
                format!(
                    "CoreMIDI output vanished before connection: {}",
                    identity.name
                )
            })?;
        output
            .connect(&matching, &format!("Open DJ - {}", identity.name))
            .map_err(|err| {
                bridge_error(
                    &format!("CoreMIDI could not open output {}", identity.name),
                    err,
                )
            })
    }

    #[tauri::command]
    pub fn native_midi_snapshot(
        app: AppHandle,
        bridge: State<'_, NativeMidiBridge>,
    ) -> Result<Vec<NativeMidiDevice>, String> {
        let mut inner = bridge
            .0
            .lock()
            .map_err(|_| "native MIDI bridge mutex was poisoned".to_string())?;
        rescan(&app, &mut inner)
    }

    #[tauri::command]
    pub fn native_midi_send(
        device_id: String,
        data: Vec<u8>,
        bridge: State<'_, NativeMidiBridge>,
    ) -> Result<(), String> {
        if data.len() != 3 {
            return Err(format!(
                "native MIDI output accepts exactly one three-byte message, got {} bytes",
                data.len()
            ));
        }
        if data[0] < 0x80 || data[1] > 0x7f || data[2] > 0x7f {
            return Err(format!("invalid bounded MIDI message: {data:?}"));
        }
        let mut inner = bridge
            .0
            .lock()
            .map_err(|_| "native MIDI bridge mutex was poisoned".to_string())?;
        let identity = inner
            .devices
            .get(&device_id)
            .cloned()
            .ok_or_else(|| format!("unknown native MIDI device id {device_id}"))?;
        if !identity.has_output {
            return Err(format!(
                "native MIDI device {} has no output port",
                identity.name
            ));
        }
        if !inner.outputs.contains_key(&device_id) {
            inner
                .outputs
                .insert(device_id.clone(), open_output(&identity)?);
        }
        let send_result = inner
            .outputs
            .get_mut(&device_id)
            .expect("output inserted above")
            .send(&data);
        if let Err(err) = send_result {
            inner.outputs.remove(&device_id);
            return Err(bridge_error(
                &format!("CoreMIDI send failed for {}", identity.name),
                err,
            ));
        }
        Ok(())
    }
}

#[cfg(not(target_os = "macos"))]
pub(crate) mod platform {
    use std::sync::Mutex;

    use serde::Serialize;
    use tauri::{AppHandle, State};

    #[derive(Clone, Debug, Serialize)]
    #[serde(rename_all = "camelCase")]
    pub struct NativeMidiDevice {
        id: String,
        name: String,
        manufacturer: String,
        has_output: bool,
    }

    #[derive(Default)]
    pub struct NativeMidiBridge(Mutex<()>);

    #[tauri::command]
    pub fn native_midi_snapshot(
        _app: AppHandle,
        _bridge: State<'_, NativeMidiBridge>,
    ) -> Result<Vec<NativeMidiDevice>, String> {
        Err("native MIDI is currently implemented only for the macOS shell".to_string())
    }

    #[tauri::command]
    pub fn native_midi_send(
        _device_id: String,
        _data: Vec<u8>,
        _bridge: State<'_, NativeMidiBridge>,
    ) -> Result<(), String> {
        Err("native MIDI is currently implemented only for the macOS shell".to_string())
    }
}

pub use platform::NativeMidiBridge;
