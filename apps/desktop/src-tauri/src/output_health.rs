//! macOS CoreAudio output-device delivery probe and default-device cycling.
//!
//! Issue #923 (AUDIO-DEVICE-01): detect when the default output is alive in
//! CoreAudio but not delivering sound (Bluetooth-after-call stall). Off macOS
//! returns an honest `unknown` verdict per ADR-0125.

pub const OUTPUT_HEALTH_PATH: &str = "/api/v1/audio/output-health";
pub const SWITCH_OUTPUT_PATH: &str = "/api/v1/audio/switch-output";

/// JSON body for `GET /api/v1/audio/output-health`.
pub fn probe_output_health_json() -> String {
    platform::probe_output_health_json()
}

/// The verdict for the default output. A HAL that runs is not a room that
/// hears: the MacBook speakers keep "delivering" while the headphone jack's
/// sense has muted their amplifier, so that case outranks `ok`.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn health_verdict(delivering: bool, default_muted_by_jack: bool) -> &'static str {
    if default_muted_by_jack {
        "muted_by_jack"
    } else if delivering {
        "ok"
    } else {
        "not_delivering"
    }
}

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub const MUTED_BY_JACK_REASON: &str = "The default output is the MacBook speakers, but headphones occupy the \
     headphone jack, which mutes the speakers: the device runs and nobody hears it. Choose the headphones as the \
     output, or use split cue.";

/// JSON body for `POST /api/v1/audio/switch-output`.
pub fn switch_output_json() -> String {
    platform::switch_output_json()
}


/// A CoreAudio four-character code, packed big-endian exactly as the SDK's
/// `'dOut'` literals are. Every HAL selector and scope below is spelled through
/// this rather than as hand-typed hex: `kAudioObjectPropertyScopeGlobal` is
/// `'glob'`, not 0, and a hand-typed 0 there made every HAL query fail.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) const fn fourcc(code: &[u8; 4]) -> u32 {
    ((code[0] as u32) << 24) | ((code[1] as u32) << 16) | ((code[2] as u32) << 8) | code[3] as u32
}

/// Where each `AudioUnitSetProperty` call of the AUHAL probe tone lands.
///
/// Platform-neutral on purpose, so Linux CI tests the same table the macOS
/// code reads. For an output AUHAL, element 0 is the output bus: its OUTPUT
/// scope is the hardware side and its INPUT scope is the side the application
/// feeds. The client stream format and the render callback therefore belong on
/// input scope, element 0; set on the hardware side they try to force the
/// device itself to 44.1 kHz mono 16-bit, which stereo and Bluetooth outputs
/// reject, and every probe would return `unknown` before the tone plays.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) mod auhal_plan {
    /// `kAudioUnitScope_Global`, `kAudioUnitScope_Input`, `kAudioUnitScope_Output`
    /// (AudioUnitProperties.h).
    pub const SCOPE_GLOBAL: u32 = 0;
    pub const SCOPE_INPUT: u32 = 1;
    pub const SCOPE_OUTPUT: u32 = 2;
    /// AUHAL bus numbering: element 0 renders to the device, element 1 captures.
    pub const OUTPUT_ELEMENT: u32 = 0;
    pub const INPUT_ELEMENT: u32 = 1;

    #[derive(Clone, Copy, Debug, PartialEq, Eq)]
    pub struct Target {
        pub scope: u32,
        pub element: u32,
    }

    /// `kAudioOutputUnitProperty_EnableIO` = 1 on the output bus's hardware side.
    pub const ENABLE_OUTPUT_IO: Target = Target { scope: SCOPE_OUTPUT, element: OUTPUT_ELEMENT };
    /// `kAudioOutputUnitProperty_EnableIO` = 0 on the input bus's hardware side.
    pub const DISABLE_INPUT_IO: Target = Target { scope: SCOPE_INPUT, element: INPUT_ELEMENT };
    /// `kAudioOutputUnitProperty_CurrentDevice` is a global property.
    pub const CURRENT_DEVICE: Target = Target { scope: SCOPE_GLOBAL, element: 0 };
    /// The format the render callback writes: the client side of the output bus.
    pub const CLIENT_STREAM_FORMAT: Target = Target { scope: SCOPE_INPUT, element: OUTPUT_ELEMENT };
    /// The callback that feeds the output bus: the same client side.
    pub const RENDER_CALLBACK: Target = Target { scope: SCOPE_INPUT, element: OUTPUT_ELEMENT };
}

/// Pick the device to bounce the default output through when cycling it.
///
/// `candidates` pairs each HAL device id with its OUTPUT channel count, as
/// `kAudioDevicePropertyStreamConfiguration` on output scope reports it.
/// `kAudioHardwarePropertyDevices` lists input-only devices too (a built-in or
/// USB microphone), and making one of those the default output fails, so a
/// device with no output channels is never a target: with a microphone listed
/// before a second speaker, the speaker is chosen. A mono output (1 channel)
/// is still a valid target. `None` means no other output-capable device exists.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub(crate) fn pick_cycle_target(current: u32, candidates: &[(u32, u32)]) -> Option<u32> {
    candidates
        .iter()
        .find(|(id, output_channels)| *id != 0 && *id != current && *output_channels > 0)
        .map(|(id, _)| *id)
}

#[cfg(target_os = "macos")]
pub(crate) mod platform {
    use super::super::shell_health::utc_timestamp_iso;
    use super::{auhal_plan, fourcc, pick_cycle_target};
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;
    use std::time::Duration;

    pub(crate) type OSStatus = i32;
    pub(crate) type AudioObjectID = u32;
    pub(crate) type AudioDeviceID = u32;
    pub(crate) type AudioUnit = *mut c_void;
    type AudioComponent = *mut c_void;
    type AudioDeviceIOProcID = *mut c_void;

    pub(crate) const NO_ERR: OSStatus = 0;
    pub(crate) const K_AUDIO_OBJECT_SYSTEM_OBJECT: AudioObjectID = 1;
    const K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN: u32 = 0;
    pub(crate) const K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL: u32 = fourcc(b"glob");
    pub(crate) const K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT: u32 = fourcc(b"outp");
    pub(crate) const K_AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE: u32 = fourcc(b"dOut");
    const K_AUDIO_HARDWARE_PROPERTY_DEVICES: u32 = fourcc(b"dev#");
    const K_AUDIO_DEVICE_PROPERTY_DEVICE_IS_RUNNING_SOMEWHERE: u32 = fourcc(b"gone");
    pub(crate) const K_AUDIO_DEVICE_PROPERTY_DEVICE_UID: u32 = fourcc(b"uid ");
    const K_AUDIO_DEVICE_PROPERTY_STREAM_CONFIGURATION: u32 = fourcc(b"slay");
    pub(crate) const K_AUDIO_OBJECT_PROPERTY_NAME: u32 = fourcc(b"lnam");

    pub(crate) const K_AUDIO_UNIT_TYPE_OUTPUT: u32 = fourcc(b"auou");
    pub(crate) const K_AUDIO_UNIT_SUBTYPE_HAL_OUTPUT: u32 = fourcc(b"ahal");
    pub(crate) const K_AUDIO_UNIT_MANUFACTURER_APPLE: u32 = fourcc(b"appl");
    pub(crate) const K_AUDIO_UNIT_PROPERTY_SET_RENDER_CALLBACK: u32 = 23;
    pub(crate) const K_AUDIO_UNIT_PROPERTY_STREAM_FORMAT: u32 = 8;
    pub(crate) const K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO: u32 = 2003;
    pub(crate) const K_AUDIO_OUTPUT_UNIT_PROPERTY_CURRENT_DEVICE: u32 = 2000;

    pub(crate) const K_AUDIO_FORMAT_LINEAR_PCM: u32 = fourcc(b"lpcm");
    const K_AUDIO_FORMAT_FLAG_IS_SIGNED_INTEGER: u32 = 0x4;
    pub(crate) const K_AUDIO_FORMAT_FLAG_IS_PACKED: u32 = 0x8;

    const PROBE_SAMPLE_RATE: f64 = 44_100.0;
    const PROBE_DURATION_MS: u64 = 100;
    const PROBE_SETTLE_MS: u64 = 120;
    const PROBE_AMPLITUDE: f32 = 0.001; // ~-60 dBFS
    const PROBE_FREQ_HZ: f32 = 440.0;

    #[repr(C)]
    pub(crate) struct AudioObjectPropertyAddress {
        pub(crate) m_selector: u32,
        pub(crate) m_scope: u32,
        pub(crate) m_element: u32,
    }

    #[repr(C)]
    pub(crate) struct AudioComponentDescription {
        pub(crate) component_type: u32,
        pub(crate) component_sub_type: u32,
        pub(crate) component_manufacturer: u32,
        pub(crate) component_flags: u32,
        pub(crate) component_flags_mask: u32,
    }

    #[repr(C)]
    pub(crate) struct AudioStreamBasicDescription {
        pub(crate) m_sample_rate: f64,
        pub(crate) m_format_id: u32,
        pub(crate) m_format_flags: u32,
        pub(crate) m_bytes_per_packet: u32,
        pub(crate) m_frames_per_packet: u32,
        pub(crate) m_bytes_per_frame: u32,
        pub(crate) m_channels_per_frame: u32,
        pub(crate) m_bits_per_channel: u32,
        pub(crate) m_reserved: u32,
    }

    #[repr(C)]
    pub(crate) struct AudioBuffer {
        pub(crate) m_number_channels: u32,
        pub(crate) m_data_byte_size: u32,
        pub(crate) m_data: *mut c_void,
    }

    /// Variable-length in C: `m_buffers` really holds `m_number_buffers`
    /// entries, so any index past 0 is read through a raw pointer bounded by
    /// the byte size the HAL reported, never through this one-element array.
    #[repr(C)]
    pub(crate) struct AudioBufferList {
        pub(crate) m_number_buffers: u32,
        pub(crate) m_buffers: [AudioBuffer; 1],
    }

    #[repr(C)]
    pub(crate) struct AudioTimeStamp {
        pub(crate) m_sample_time: f64,
        pub(crate) m_host_time: u64,
        pub(crate) m_rate_scalar: f64,
        pub(crate) m_word_clock_time: u64,
        pub(crate) m_smpte_time: [u8; 24],
        pub(crate) m_flags: u32,
        pub(crate) m_reserved: u32,
    }

    type AudioDeviceIOProc = extern "C" fn(
        in_device: AudioDeviceID,
        _in_now: *const AudioTimeStamp,
        _in_input_data: *const AudioBufferList,
        _in_input_time: *const AudioTimeStamp,
        out_output_data: *mut AudioBufferList,
        _in_output_time: *const AudioTimeStamp,
        in_client_data: *mut c_void,
    ) -> OSStatus;

    pub(crate) type AURenderCallback = extern "C" fn(
        in_ref_con: *mut c_void,
        _io_action_flags: *mut u32,
        _in_time_stamp: *const AudioTimeStamp,
        _in_bus_number: u32,
        in_number_frames: u32,
        io_data: *mut AudioBufferList,
    ) -> OSStatus;

    #[repr(C)]
    pub(crate) struct AURenderCallbackStruct {
        pub(crate) input_proc: AURenderCallback,
        pub(crate) input_proc_ref_con: *mut c_void,
    }

    struct IoCycleMonitor {
        device: AudioDeviceID,
        proc_id: AudioDeviceIOProcID,
        counter: *mut AtomicU64,
    }

    struct ProbeToneState {
        phase: f32,
        sample_rate: f32,
        amplitude: f32,
        freq_hz: f32,
    }

    #[link(name = "CoreAudio", kind = "framework")]
    extern "C" {
        fn AudioObjectGetPropertyDataSize(
            in_object_id: AudioObjectID,
            in_address: *const AudioObjectPropertyAddress,
            in_qualifier_data_size: u32,
            in_qualifier_data: *const c_void,
            out_data_size: *mut u32,
        ) -> OSStatus;
        pub(crate) fn AudioObjectGetPropertyData(
            in_object_id: AudioObjectID,
            in_address: *const AudioObjectPropertyAddress,
            in_qualifier_data_size: u32,
            in_qualifier_data: *const c_void,
            io_data_size: *mut u32,
            out_data: *mut c_void,
        ) -> OSStatus;
        fn AudioObjectSetPropertyData(
            in_object_id: AudioObjectID,
            in_address: *const AudioObjectPropertyAddress,
            in_qualifier_data_size: u32,
            in_qualifier_data: *const c_void,
            in_data_size: u32,
            in_data: *const c_void,
        ) -> OSStatus;
        fn AudioDeviceCreateIOProcID(
            in_device: AudioDeviceID,
            in_proc: AudioDeviceIOProc,
            in_client_data: *mut c_void,
            out_io_proc_id: *mut AudioDeviceIOProcID,
        ) -> OSStatus;
        fn AudioDeviceDestroyIOProcID(
            in_device: AudioDeviceID,
            in_io_proc_id: AudioDeviceIOProcID,
        ) -> OSStatus;
        fn AudioDeviceStart(in_device: AudioDeviceID, in_proc_id: AudioDeviceIOProcID) -> OSStatus;
        fn AudioDeviceStop(in_device: AudioDeviceID, in_proc_id: AudioDeviceIOProcID) -> OSStatus;
    }

    #[link(name = "AudioToolbox", kind = "framework")]
    extern "C" {
        pub(crate) fn AudioComponentFindNext(
            in_component: AudioComponent,
            in_desc: *const AudioComponentDescription,
        ) -> AudioComponent;
        pub(crate) fn AudioComponentInstanceNew(
            in_component: AudioComponent,
            out_instance: *mut AudioUnit,
        ) -> OSStatus;
        pub(crate) fn AudioComponentInstanceDispose(in_instance: AudioUnit) -> OSStatus;
        pub(crate) fn AudioUnitInitialize(in_unit: AudioUnit) -> OSStatus;
        pub(crate) fn AudioUnitUninitialize(in_unit: AudioUnit) -> OSStatus;
        fn AudioUnitSetProperty(
            in_unit: AudioUnit,
            in_id: u32,
            in_scope: u32,
            in_element: u32,
            in_data: *const c_void,
            in_data_size: u32,
        ) -> OSStatus;
        pub(crate) fn AudioOutputUnitStart(in_unit: AudioUnit) -> OSStatus;
        pub(crate) fn AudioOutputUnitStop(in_unit: AudioUnit) -> OSStatus;
    }

    #[link(name = "CoreFoundation", kind = "framework")]
    extern "C" {
        fn CFStringGetCStringPtr(cf_str: *const c_void, encoding: u32) -> *const std::ffi::c_char;
        fn CFStringGetCString(
            cf_str: *const c_void,
            buffer: *mut std::ffi::c_char,
            buffer_size: isize,
            encoding: u32,
        ) -> bool;
        fn CFRelease(cf: *const c_void);
    }

    pub(crate) fn prop_addr(selector: u32, scope: u32) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress {
            m_selector: selector,
            m_scope: scope,
            m_element: K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
        }
    }

    pub(crate) fn global_addr(selector: u32) -> AudioObjectPropertyAddress {
        prop_addr(selector, K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL)
    }

    pub(crate) fn get_u32(object_id: AudioObjectID, selector: u32) -> Result<u32, String> {
        let address = global_addr(selector);
        let mut value = 0u32;
        let mut size = std::mem::size_of::<u32>() as u32;
        // SAFETY: `value` is a live u32 and `size` says exactly that many bytes.
        let status = unsafe {
            AudioObjectGetPropertyData(
                object_id,
                &address,
                0,
                ptr::null(),
                &mut size,
                &mut value as *mut u32 as *mut c_void,
            )
        };
        if status != NO_ERR {
            return Err(format!("AudioObjectGetPropertyData({selector:#x}) failed: {status}"));
        }
        Ok(value)
    }

    /// Byte size of a variable-length property. `AudioObjectGetPropertyData`
    /// with a zero-size, null buffer is not a size query: it writes nothing and
    /// reports 0, which read as "no devices" and made every cycle fail.
    pub(crate) fn property_size(
        object_id: AudioObjectID,
        address: &AudioObjectPropertyAddress,
    ) -> Result<u32, String> {
        let mut size = 0u32;
        // SAFETY: `size` is a live u32 the HAL writes the byte count into.
        let status = unsafe {
            AudioObjectGetPropertyDataSize(object_id, address, 0, ptr::null(), &mut size)
        };
        if status != NO_ERR {
            return Err(format!(
                "AudioObjectGetPropertyDataSize({:#x}) failed: {status}",
                address.m_selector
            ));
        }
        Ok(size)
    }

    pub(crate) fn get_device_list() -> Result<Vec<AudioDeviceID>, String> {
        let address = global_addr(K_AUDIO_HARDWARE_PROPERTY_DEVICES);
        let mut size = property_size(K_AUDIO_OBJECT_SYSTEM_OBJECT, &address)?;
        let count = size as usize / std::mem::size_of::<AudioDeviceID>();
        let mut devices = vec![0u32; count];
        // SAFETY: `devices` holds `count` u32s, which is `size` bytes rounded down.
        size = (count * std::mem::size_of::<AudioDeviceID>()) as u32;
        let status = unsafe {
            AudioObjectGetPropertyData(
                K_AUDIO_OBJECT_SYSTEM_OBJECT,
                &address,
                0,
                ptr::null(),
                &mut size,
                devices.as_mut_ptr() as *mut c_void,
            )
        };
        if status != NO_ERR {
            return Err(format!("device list read failed: {status}"));
        }
        devices.truncate(size as usize / std::mem::size_of::<AudioDeviceID>());
        Ok(devices)
    }

    /// Total OUTPUT channels a device exposes, summed over its output streams.
    /// 0 for an input-only device such as a microphone.
    pub(crate) fn output_channel_count(device: AudioDeviceID) -> Result<u32, String> {
        let address = prop_addr(
            K_AUDIO_DEVICE_PROPERTY_STREAM_CONFIGURATION,
            K_AUDIO_OBJECT_PROPERTY_SCOPE_OUTPUT,
        );
        let size = property_size(device, &address)? as usize;
        let header = std::mem::offset_of!(AudioBufferList, m_buffers);
        if size < header {
            return Ok(0);
        }
        // u64 backing keeps the list 8-byte aligned for its pointer fields.
        let mut backing = vec![0u64; size.div_ceil(8)];
        let mut io_size = size as u32;
        // SAFETY: `backing` spans at least `size` bytes, as `io_size` says.
        let status = unsafe {
            AudioObjectGetPropertyData(
                device,
                &address,
                0,
                ptr::null(),
                &mut io_size,
                backing.as_mut_ptr() as *mut c_void,
            )
        };
        if status != NO_ERR {
            return Err(format!("stream configuration read failed: {status}"));
        }
        let list = backing.as_ptr() as *const AudioBufferList;
        let written = (io_size as usize).min(size);
        let fits = written.saturating_sub(header) / std::mem::size_of::<AudioBuffer>();
        // SAFETY: `list` points at `written` initialized, aligned bytes; only
        // the buffers that fit inside them are read.
        let channels = unsafe {
            let declared = (*list).m_number_buffers as usize;
            let first = ptr::addr_of!((*list).m_buffers) as *const AudioBuffer;
            (0..declared.min(fits)).map(|index| (*first.add(index)).m_number_channels).sum()
        };
        Ok(channels)
    }

    pub(crate) fn get_default_output_device() -> Result<AudioDeviceID, String> {
        let device = get_u32(K_AUDIO_OBJECT_SYSTEM_OBJECT, K_AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE)
            .map_err(|error| format!("default output device query failed: {error}"))?;
        if device == 0 {
            return Err("default output device query returned no device".into());
        }
        Ok(device)
    }

    pub(crate) fn set_default_output_device(device: AudioDeviceID) -> Result<(), String> {
        let address = global_addr(K_AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE);
        // SAFETY: `device` is a live u32 and the size says exactly that.
        let status = unsafe {
            AudioObjectSetPropertyData(
                K_AUDIO_OBJECT_SYSTEM_OBJECT,
                &address,
                0,
                ptr::null(),
                std::mem::size_of::<AudioDeviceID>() as u32,
                &device as *const u32 as *const c_void,
            )
        };
        if status != NO_ERR {
            return Err(format!("set default output device failed: {status}"));
        }
        Ok(())
    }

    pub(crate) fn get_cf_string(object_id: AudioObjectID, selector: u32) -> Result<String, String> {
        let address = global_addr(selector);
        let mut cf_str: *const c_void = ptr::null();
        let mut size = std::mem::size_of::<*const c_void>() as u32;
        // SAFETY: `cf_str` is a live pointer-sized slot and the size says so.
        let status = unsafe {
            AudioObjectGetPropertyData(
                object_id,
                &address,
                0,
                ptr::null(),
                &mut size,
                &mut cf_str as *mut *const c_void as *mut c_void,
            )
        };
        if status != NO_ERR || cf_str.is_null() {
            return Err(format!("CFString property {selector:#x} failed: {status}"));
        }
        const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
        // SAFETY: `cf_str` is a CFString the HAL returned with +1 retain; it is
        // read while alive and released exactly once on every path below.
        unsafe {
            let ptr_utf8 = CFStringGetCStringPtr(cf_str, K_CF_STRING_ENCODING_UTF8);
            let value = if !ptr_utf8.is_null() {
                std::ffi::CStr::from_ptr(ptr_utf8).to_string_lossy().into_owned()
            } else {
                let mut buf = [0 as std::ffi::c_char; 512];
                if !CFStringGetCString(
                    cf_str,
                    buf.as_mut_ptr(),
                    buf.len() as isize,
                    K_CF_STRING_ENCODING_UTF8,
                ) {
                    CFRelease(cf_str);
                    return Err("CFStringGetCString failed".into());
                }
                std::ffi::CStr::from_ptr(buf.as_ptr()).to_string_lossy().into_owned()
            };
            CFRelease(cf_str);
            Ok(value)
        }
    }

    fn device_is_running(device: AudioDeviceID) -> Result<bool, String> {
        let running = get_u32(device, K_AUDIO_DEVICE_PROPERTY_DEVICE_IS_RUNNING_SOMEWHERE)?;
        Ok(running != 0)
    }

    fn hal_overload_recent() -> bool {
        let output = std::process::Command::new("log")
            .args([
                "show",
                "--last",
                "2s",
                "--style",
                "compact",
                "--predicate",
                "eventMessage CONTAINS \"skipping cycle due to overload\" OR eventMessage CONTAINS \"AudioSkywalkReadLoop overwait\"",
            ])
            .output();
        match output {
            Ok(out) => {
                let text = String::from_utf8_lossy(&out.stdout);
                !text.trim().is_empty()
            }
            Err(_) => false,
        }
    }

    fn zero_output_buffers(buffer_list: *mut AudioBufferList) {
        if buffer_list.is_null() {
            return;
        }
        // SAFETY: the HAL hands the IOProc a valid list of `m_number_buffers`
        // buffers; each is reached through a raw pointer, not the 1-element array.
        unsafe {
            let count = (*buffer_list).m_number_buffers as usize;
            let first = ptr::addr_of_mut!((*buffer_list).m_buffers) as *mut AudioBuffer;
            for index in 0..count {
                let buffer = &*first.add(index);
                if !buffer.m_data.is_null() && buffer.m_data_byte_size > 0 {
                    ptr::write_bytes(buffer.m_data as *mut u8, 0, buffer.m_data_byte_size as usize);
                }
            }
        }
    }

    extern "C" fn device_io_proc(
        _in_device: AudioDeviceID,
        _in_now: *const AudioTimeStamp,
        _in_input_data: *const AudioBufferList,
        _in_input_time: *const AudioTimeStamp,
        out_output_data: *mut AudioBufferList,
        _in_output_time: *const AudioTimeStamp,
        in_client_data: *mut c_void,
    ) -> OSStatus {
        if !in_client_data.is_null() {
            // SAFETY: the client data is the monitor's counter, alive until
            // `IoCycleMonitor::drop` has stopped and destroyed this IOProc.
            let counter = unsafe { &*(in_client_data as *const AtomicU64) };
            counter.fetch_add(1, Ordering::Relaxed);
        }
        zero_output_buffers(out_output_data);
        NO_ERR
    }

    impl IoCycleMonitor {
        fn start(device: AudioDeviceID, counter: *mut AtomicU64) -> Result<Self, String> {
            let mut proc_id: AudioDeviceIOProcID = ptr::null_mut();
            // SAFETY: `counter` outlives the monitor (see probe_output_health),
            // and the IOProc is destroyed in Drop before it goes away.
            let status = unsafe {
                AudioDeviceCreateIOProcID(device, device_io_proc, counter as *mut c_void, &mut proc_id)
            };
            if status != NO_ERR {
                return Err(format!("AudioDeviceCreateIOProcID failed: {status}"));
            }
            // SAFETY: `proc_id` was just created for this device.
            let status = unsafe { AudioDeviceStart(device, proc_id) };
            if status != NO_ERR {
                // SAFETY: same live `proc_id`, destroyed once.
                let _ = unsafe { AudioDeviceDestroyIOProcID(device, proc_id) };
                return Err(format!("AudioDeviceStart failed: {status}"));
            }
            Ok(Self {
                device,
                proc_id,
                counter,
            })
        }

        fn count(&self) -> u64 {
            // SAFETY: the counter outlives the monitor.
            unsafe { (*self.counter).load(Ordering::Relaxed) }
        }
    }

    impl Drop for IoCycleMonitor {
        fn drop(&mut self) {
            // SAFETY: `proc_id` is live until this destroy.
            unsafe {
                let _ = AudioDeviceStop(self.device, self.proc_id);
                let _ = AudioDeviceDestroyIOProcID(self.device, self.proc_id);
            }
        }
    }

    extern "C" fn probe_render_callback(
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
        // SAFETY: the ref con is the tone state that outlives the started unit,
        // and `io_data` is the AU's buffer list for this render cycle. The
        // client format is packed interleaved i16, so buffer 0 holds
        // `frames * channels` samples.
        let state = unsafe { &mut *(in_ref_con as *mut ProbeToneState) };
        let list = unsafe { &mut *io_data };
        if list.m_number_buffers == 0 {
            return NO_ERR;
        }
        let buffer = &mut list.m_buffers[0];
        if buffer.m_data.is_null() {
            return NO_ERR;
        }
        let frames = in_number_frames as usize;
        let channels = buffer.m_number_channels.max(1) as usize;
        let capacity = buffer.m_data_byte_size as usize / std::mem::size_of::<i16>();
        let len = (frames * channels).min(capacity);
        let samples = unsafe { std::slice::from_raw_parts_mut(buffer.m_data as *mut i16, len) };
        let phase_inc = 2.0 * std::f32::consts::PI * state.freq_hz / state.sample_rate;
        for frame in samples.chunks_mut(channels) {
            let value =
                (state.amplitude * state.phase.sin() * 32_767.0).clamp(-32_767.0, 32_767.0) as i16;
            state.phase += phase_inc;
            if state.phase > 2.0 * std::f32::consts::PI {
                state.phase -= 2.0 * std::f32::consts::PI;
            }
            frame.fill(value);
        }
        NO_ERR
    }

    /// `AudioUnitSetProperty` at one `auhal_plan` target.
    ///
    /// # Safety
    /// `unit` must be a live AudioUnit and `value` must point at a live `T`.
    pub(crate) unsafe fn set_unit_property<T>(
        unit: AudioUnit,
        property: u32,
        target: auhal_plan::Target,
        value: *const T,
        what: &str,
    ) -> Result<(), String> {
        let status = AudioUnitSetProperty(
            unit,
            property,
            target.scope,
            target.element,
            value as *const c_void,
            std::mem::size_of::<T>() as u32,
        );
        if status != NO_ERR {
            return Err(format!("{what} failed: {status}"));
        }
        Ok(())
    }

    fn play_probe_tone_hal(device: AudioDeviceID) -> Result<(), String> {
        let desc = AudioComponentDescription {
            component_type: K_AUDIO_UNIT_TYPE_OUTPUT,
            component_sub_type: K_AUDIO_UNIT_SUBTYPE_HAL_OUTPUT,
            component_manufacturer: K_AUDIO_UNIT_MANUFACTURER_APPLE,
            component_flags: 0,
            component_flags_mask: 0,
        };
        // SAFETY: `desc` is a valid description; a null result is handled.
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

        let mut tone_state = ProbeToneState {
            phase: 0.0,
            sample_rate: PROBE_SAMPLE_RATE as f32,
            amplitude: PROBE_AMPLITUDE,
            freq_hz: PROBE_FREQ_HZ,
        };
        // SAFETY: `unit` stays live until the dispose after this block, and
        // `tone_state` outlives the unit's start/stop window inside it.
        let result = unsafe {
            (|| {
                let enable = 1u32;
                let disable = 0u32;
                set_unit_property(
                    unit,
                    K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO,
                    auhal_plan::ENABLE_OUTPUT_IO,
                    &enable,
                    "enable HAL output IO",
                )?;
                set_unit_property(
                    unit,
                    K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO,
                    auhal_plan::DISABLE_INPUT_IO,
                    &disable,
                    "disable HAL input IO",
                )?;
                set_unit_property(
                    unit,
                    K_AUDIO_OUTPUT_UNIT_PROPERTY_CURRENT_DEVICE,
                    auhal_plan::CURRENT_DEVICE,
                    &device,
                    "set HAL current device",
                )?;
                let fmt = AudioStreamBasicDescription {
                    m_sample_rate: PROBE_SAMPLE_RATE,
                    m_format_id: K_AUDIO_FORMAT_LINEAR_PCM,
                    m_format_flags: K_AUDIO_FORMAT_FLAG_IS_SIGNED_INTEGER
                        | K_AUDIO_FORMAT_FLAG_IS_PACKED,
                    m_bytes_per_packet: 2,
                    m_frames_per_packet: 1,
                    m_bytes_per_frame: 2,
                    m_channels_per_frame: 1,
                    m_bits_per_channel: 16,
                    m_reserved: 0,
                };
                set_unit_property(
                    unit,
                    K_AUDIO_UNIT_PROPERTY_STREAM_FORMAT,
                    auhal_plan::CLIENT_STREAM_FORMAT,
                    &fmt,
                    "set HAL client stream format",
                )?;
                let callback = AURenderCallbackStruct {
                    input_proc: probe_render_callback,
                    input_proc_ref_con: &mut tone_state as *mut ProbeToneState as *mut c_void,
                };
                set_unit_property(
                    unit,
                    K_AUDIO_UNIT_PROPERTY_SET_RENDER_CALLBACK,
                    auhal_plan::RENDER_CALLBACK,
                    &callback,
                    "set HAL render callback",
                )?;

                let status = AudioUnitInitialize(unit);
                if status != NO_ERR {
                    return Err(format!("AudioUnitInitialize failed: {status}"));
                }
                let status = AudioOutputUnitStart(unit);
                if status != NO_ERR {
                    let _ = AudioUnitUninitialize(unit);
                    return Err(format!("AudioOutputUnitStart failed: {status}"));
                }

                thread::sleep(Duration::from_millis(PROBE_DURATION_MS));
                let _ = AudioOutputUnitStop(unit);
                let _ = AudioUnitUninitialize(unit);
                Ok(())
            })()
        };

        // SAFETY: `unit` is live and disposed exactly once.
        let _ = unsafe { AudioComponentInstanceDispose(unit) };
        result
    }

    fn device_delivering_verdict(
        running_before: bool,
        running_after: bool,
        io_cycles_advanced: bool,
    ) -> bool {
        io_cycles_advanced || (running_after && !running_before)
    }

    pub fn probe_output_health_json() -> String {
        match probe_output_health() {
            Ok(body) => body,
            Err(reason) => serde_json::json!({
                "device_delivering": null,
                "verdict": "unknown",
                "reason": reason,
                "default_device_name": null,
                "default_device_uid": null,
                "io_cycles_advanced": null,
                "hal_overload_recent": null,
                "probe_available": false,
                "master_pin_fault": crate::cue_sink::master_fault_json(),
                "checked_at": utc_timestamp_iso(),
            })
            .to_string(),
        }
    }

    fn probe_output_health() -> Result<String, String> {
        let device = get_default_output_device()?;
        let name = get_cf_string(device, K_AUDIO_OBJECT_PROPERTY_NAME).ok();
        let uid = get_cf_string(device, K_AUDIO_DEVICE_PROPERTY_DEVICE_UID).ok();
        let running_before = device_is_running(device)?;

        let counter = AtomicU64::new(0);
        let monitor = IoCycleMonitor::start(device, &counter as *const AtomicU64 as *mut AtomicU64)?;
        let cycle_before = monitor.count();

        play_probe_tone_hal(device)?;
        thread::sleep(Duration::from_millis(PROBE_SETTLE_MS));

        let cycle_after = monitor.count();
        drop(monitor);

        let running_after = device_is_running(device)?;
        let io_cycles_advanced = cycle_after > cycle_before;
        let hal_overload = hal_overload_recent();
        let delivering = device_delivering_verdict(running_before, running_after, io_cycles_advanced);
        // A listing that fails leaves the mute unknown, not false: say so.
        let (default_muted_by_jack, listing_error) = match crate::cue_sink::list_output_devices() {
            Ok(devices) => (
                uid.as_deref()
                    .and_then(|default_uid| devices.iter().find(|d| d.uid == default_uid))
                    .is_some_and(|device| crate::cue_sink::muted_by_jack(device, &devices)),
                None,
            ),
            Err(err) => (false, Some(err)),
        };
        let reason = if default_muted_by_jack {
            super::MUTED_BY_JACK_REASON.to_string()
        } else if let Some(err) = listing_error.as_ref() {
            format!("output listing failed, so a jack-muted speaker cannot be ruled out: {err}")
        } else if delivering {
            String::new()
        } else if hal_overload {
            "Default output device is not advancing IO cycles; recent HAL overload signatures were seen in the system log".into()
        } else {
            "Default output device is not delivering audio (IO cycles did not advance during probe tone)".into()
        };
        let verdict = match (listing_error.is_some(), super::health_verdict(delivering, default_muted_by_jack)) {
            (true, "ok") => "unknown",
            (_, verdict) => verdict,
        };
        Ok(serde_json::json!({
            "device_delivering": delivering,
            "verdict": verdict,
            "reason": if reason.is_empty() { serde_json::Value::Null } else { serde_json::Value::String(reason) },
            "default_device_name": name,
            "default_device_uid": uid,
            "io_cycles_advanced": io_cycles_advanced,
            "hal_overload_recent": hal_overload,
            "probe_available": true,
            "default_muted_by_jack": default_muted_by_jack,
            "master_pin_fault": crate::cue_sink::master_fault_json(),
            "checked_at": utc_timestamp_iso(),
        })
        .to_string())
    }

    pub fn switch_output_json() -> String {
        match switch_output() {
            Ok(body) => body,
            Err(reason) => serde_json::json!({
                "cycled": false,
                "error": reason,
            })
            .to_string(),
        }
    }

    fn switch_output() -> Result<String, String> {
        let current = get_default_output_device()?;
        let from_name = get_cf_string(current, K_AUDIO_OBJECT_PROPERTY_NAME)?;
        // A device whose stream configuration cannot be read is treated as
        // having no outputs: it is never a safe target to make the default.
        let candidates: Vec<(AudioDeviceID, u32)> = get_device_list()?
            .into_iter()
            .map(|id| (id, output_channel_count(id).unwrap_or(0)))
            .collect();
        let alternate = pick_cycle_target(current, &candidates).ok_or_else(|| {
            "no other output-capable device available; cannot cycle".to_string()
        })?;
        let via_name = get_cf_string(alternate, K_AUDIO_OBJECT_PROPERTY_NAME)?;
        set_default_output_device(alternate)?;
        thread::sleep(Duration::from_millis(200));
        set_default_output_device(current)?;
        let restored_name = get_cf_string(current, K_AUDIO_OBJECT_PROPERTY_NAME)?;
        Ok(serde_json::json!({
            "cycled": true,
            "from": from_name,
            "via": via_name,
            "restored": restored_name,
        })
        .to_string())
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn delivering_requires_cycle_advance_or_running_transition() {
            assert!(device_delivering_verdict(false, true, false));
            assert!(device_delivering_verdict(true, true, true));
            assert!(!device_delivering_verdict(true, true, false));
            assert!(!device_delivering_verdict(false, false, false));
        }

        #[test]
        fn probe_returns_json_with_checked_at() {
            let body = probe_output_health_json();
            assert!(body.contains("\"checked_at\""), "{body}");
            assert!(body.contains("\"verdict\""), "{body}");
        }

        #[test]
        fn switch_output_json_has_cycled_field() {
            let body = switch_output_json();
            assert!(body.contains("\"cycled\""), "{body}");
        }
    }
}

#[cfg(not(target_os = "macos"))]
mod platform {
    use super::super::shell_health::utc_timestamp_iso;

    fn unavailable_json() -> String {
        serde_json::json!({
            "device_delivering": null,
            "verdict": "unknown",
            "reason": "installed macOS shell required for OS output probe",
            "default_device_name": null,
            "default_device_uid": null,
            "io_cycles_advanced": null,
            "hal_overload_recent": null,
            "probe_available": false,
            "master_pin_fault": serde_json::Value::Null,
            "checked_at": utc_timestamp_iso(),
        })
        .to_string()
    }

    pub fn probe_output_health_json() -> String {
        unavailable_json()
    }

    pub fn switch_output_json() -> String {
        serde_json::json!({
            "cycled": false,
            "error": "installed macOS shell required for output device cycling",
        })
        .to_string()
    }

    #[cfg(test)]
    mod tests {
        use super::*;

        #[test]
        fn probe_returns_unknown_off_macos() {
            let body = probe_output_health_json();
            assert!(body.contains("\"verdict\":\"unknown\""), "{body}");
            assert!(body.contains("\"probe_available\":false"), "{body}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::{auhal_plan, fourcc, health_verdict, pick_cycle_target};

    #[test]
    fn a_jack_muted_default_is_never_ok_even_while_the_hal_delivers() {
        assert_eq!(health_verdict(true, true), "muted_by_jack", "build 10: delivering, muted, reported ok");
        assert_eq!(health_verdict(false, true), "muted_by_jack");
        // Controls: an unmuted device keeps the delivering verdict both ways.
        assert_eq!(health_verdict(true, false), "ok");
        assert_eq!(health_verdict(false, false), "not_delivering");
    }

    #[test]
    fn fourcc_packs_big_endian_like_the_sdk_literals() {
        // Values as printed by the SDK headers' own four-char literals.
        assert_eq!(fourcc(b"dOut"), 0x644F_7574);
        assert_eq!(fourcc(b"glob"), 0x676C_6F62);
        assert_eq!(fourcc(b"outp"), 0x6F75_7470);
        assert_eq!(fourcc(b"auou"), 0x6175_6F75);
    }

    #[test]
    fn auhal_client_format_and_callback_sit_on_the_input_scope_of_the_output_bus() {
        // Codex P1 on PR #3654: on the hardware (output) scope these try to
        // force the device itself to 44.1 kHz mono 16-bit.
        for (what, target) in [
            ("client stream format", auhal_plan::CLIENT_STREAM_FORMAT),
            ("render callback", auhal_plan::RENDER_CALLBACK),
        ] {
            assert_eq!(target.scope, auhal_plan::SCOPE_INPUT, "{what} must be on the client side");
            assert_eq!(target.element, auhal_plan::OUTPUT_ELEMENT, "{what} must be on the output bus");
        }
    }

    #[test]
    fn auhal_io_enables_stay_on_the_hardware_side_of_each_bus() {
        // Control against the overshoot "move every property to input scope":
        // EnableIO is a hardware-side property, output bus on output scope and
        // input bus on input scope.
        assert_eq!(
            auhal_plan::ENABLE_OUTPUT_IO,
            auhal_plan::Target { scope: auhal_plan::SCOPE_OUTPUT, element: auhal_plan::OUTPUT_ELEMENT }
        );
        assert_eq!(
            auhal_plan::DISABLE_INPUT_IO,
            auhal_plan::Target { scope: auhal_plan::SCOPE_INPUT, element: auhal_plan::INPUT_ELEMENT }
        );
        assert_eq!(auhal_plan::CURRENT_DEVICE.scope, auhal_plan::SCOPE_GLOBAL);
        // kAudioUnitScope_* from AudioUnitProperties.h; a 0 for output scope
        // silently addressed the global scope instead.
        assert_eq!(
            (auhal_plan::SCOPE_GLOBAL, auhal_plan::SCOPE_INPUT, auhal_plan::SCOPE_OUTPUT),
            (0, 1, 2)
        );
    }

    #[test]
    fn cycle_target_skips_an_input_only_device_listed_before_a_speaker() {
        // Codex P1 on PR #3654: current output 40, microphone 41 (0 output
        // channels) listed before speaker 42.
        let devices = [(40, 2), (41, 0), (42, 2)];
        assert_eq!(pick_cycle_target(40, &devices), Some(42));
    }

    #[test]
    fn cycle_target_is_none_when_only_input_devices_remain() {
        let devices = [(41, 0), (40, 2), (43, 0)];
        assert_eq!(pick_cycle_target(40, &devices), None);
    }

    #[test]
    fn cycle_target_accepts_a_mono_output_and_never_the_current_or_null_device() {
        // Control against overshooting the filter (e.g. requiring stereo): a
        // 1-channel output is a real output and must stay eligible.
        let devices = [(0, 2), (40, 2), (44, 1)];
        assert_eq!(pick_cycle_target(40, &devices), Some(44));
        assert_eq!(pick_cycle_target(40, &[(40, 2)]), None);
    }
}
