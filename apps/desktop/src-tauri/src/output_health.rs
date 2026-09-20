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

/// JSON body for `POST /api/v1/audio/switch-output`.
pub fn switch_output_json() -> String {
    platform::switch_output_json()
}

#[cfg(target_os = "macos")]
mod platform {
    use super::super::shell_health::utc_timestamp_iso;
    use std::ffi::c_void;
    use std::ptr;
    use std::sync::atomic::{AtomicU64, Ordering};
    use std::thread;
    use std::time::Duration;

    type OSStatus = i32;
    type AudioObjectID = u32;
    type AudioDeviceID = u32;
    type AudioUnit = *mut c_void;
    type AudioComponent = *mut c_void;
    type AudioDeviceIOProcID = *mut c_void;

    const NO_ERR: OSStatus = 0;
    const K_AUDIO_OBJECT_SYSTEM_OBJECT: AudioObjectID = 1;
    const K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN: u32 = 0;
    const K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL: u32 = 0;
    const K_AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE: u32 = 0x644F_7574; // 'dOut'
    const K_AUDIO_HARDWARE_PROPERTY_DEVICES: u32 = 0x6465_7623; // 'dev#'
    const K_AUDIO_DEVICE_PROPERTY_DEVICE_IS_RUNNING_SOMEWHERE: u32 = 0x6769_7372; // 'gisr'
    const K_AUDIO_DEVICE_PROPERTY_DEVICE_UID: u32 = 0x7569_6420; // 'uid '
    const K_AUDIO_OBJECT_PROPERTY_NAME: u32 = 0x6C6E_616D; // 'lnam'

    const K_AUDIO_UNIT_TYPE_OUTPUT: u32 = 0x6F75_7470; // 'outp'
    const K_AUDIO_UNIT_SUBTYPE_HAL_OUTPUT: u32 = 0x6168_616C; // 'ahal'
    const K_AUDIO_UNIT_SCOPE_OUTPUT: u32 = 0;
    const K_AUDIO_UNIT_SCOPE_INPUT: u32 = 1;
    const K_AUDIO_UNIT_SCOPE_GLOBAL: u32 = 0;
    const K_AUDIO_UNIT_PROPERTY_SET_RENDER_CALLBACK: u32 = 23;
    const K_AUDIO_UNIT_PROPERTY_STREAM_FORMAT: u32 = 8;
    const K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO: u32 = 2003;
    const K_AUDIO_OUTPUT_UNIT_PROPERTY_CURRENT_DEVICE: u32 = 2000;

    const K_AUDIO_FORMAT_LINEAR_PCM: u32 = 0x6C70_636D; // 'lpcm'
    const K_AUDIO_FORMAT_FLAG_IS_SIGNED_INTEGER: u32 = 0x4;
    const K_AUDIO_FORMAT_FLAG_IS_PACKED: u32 = 0x8;

    const PROBE_SAMPLE_RATE: f64 = 44_100.0;
    const PROBE_DURATION_MS: u64 = 100;
    const PROBE_SETTLE_MS: u64 = 120;
    const PROBE_AMPLITUDE: f32 = 0.001; // ~-60 dBFS
    const PROBE_FREQ_HZ: f32 = 440.0;

    #[repr(C)]
    struct AudioObjectPropertyAddress {
        m_selector: u32,
        m_scope: u32,
        m_element: u32,
    }

    #[repr(C)]
    struct AudioComponentDescription {
        component_type: u32,
        component_sub_type: u32,
        component_manufacturer: u32,
        component_flags: u32,
        component_flags_mask: u32,
    }

    #[repr(C)]
    struct AudioStreamBasicDescription {
        m_sample_rate: f64,
        m_format_id: u32,
        m_format_flags: u32,
        m_bytes_per_packet: u32,
        m_frames_per_packet: u32,
        m_bytes_per_frame: u32,
        m_channels_per_frame: u32,
        m_bits_per_channel: u32,
        m_reserved: u32,
    }

    #[repr(C)]
    struct AudioBuffer {
        m_number_channels: u32,
        m_data_byte_size: u32,
        m_data: *mut c_void,
    }

    #[repr(C)]
    struct AudioBufferList {
        m_number_buffers: u32,
        m_buffers: [AudioBuffer; 1],
    }

    #[repr(C)]
    struct AudioTimeStamp {
        m_sample_time: f64,
        m_host_time: u64,
        m_rate_scalar: f64,
        m_word_clock_time: u64,
        m_smpte_time: [u8; 16],
        m_flags: u32,
        m_reserved: u32,
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

    type AURenderCallback = extern "C" fn(
        in_ref_con: *mut c_void,
        _io_action_flags: *mut u32,
        _in_time_stamp: *const AudioTimeStamp,
        _in_bus_number: u32,
        in_number_frames: u32,
        io_data: *mut AudioBufferList,
    ) -> OSStatus;

    #[repr(C)]
    struct AURenderCallbackStruct {
        input_proc: AURenderCallback,
        input_proc_ref_con: *mut c_void,
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
        fn AudioObjectGetPropertyData(
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
        fn AudioDeviceStart(
            in_device: AudioDeviceID,
            in_proc_id: AudioDeviceIOProcID,
        ) -> OSStatus;
        fn AudioDeviceStop(
            in_device: AudioDeviceID,
            in_proc_id: AudioDeviceIOProcID,
        ) -> OSStatus;
    }

    #[link(name = "AudioToolbox", kind = "framework")]
    extern "C" {
        fn AudioComponentFindNext(
            in_component: AudioComponent,
            in_desc: *const AudioComponentDescription,
        ) -> AudioComponent;
        fn AudioComponentInstanceNew(
            in_component: AudioComponent,
            out_instance: *mut AudioUnit,
        ) -> OSStatus;
        fn AudioComponentInstanceDispose(in_instance: AudioUnit) -> OSStatus;
        fn AudioUnitInitialize(in_unit: AudioUnit) -> OSStatus;
        fn AudioUnitUninitialize(in_unit: AudioUnit) -> OSStatus;
        fn AudioUnitSetProperty(
            in_unit: AudioUnit,
            in_id: u32,
            in_scope: u32,
            in_element: u32,
            in_data: *const c_void,
            in_data_size: u32,
        ) -> OSStatus;
        fn AudioOutputUnitStart(in_unit: AudioUnit) -> OSStatus;
        fn AudioOutputUnitStop(in_unit: AudioUnit) -> OSStatus;
    }

    fn prop_addr(selector: u32) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress {
            m_selector: selector,
            m_scope: K_AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
            m_element: K_AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
        }
    }

    fn get_u32(object_id: AudioObjectID, selector: u32) -> Result<u32, String> {
        let address = prop_addr(selector);
        let mut value = 0u32;
        let mut size = std::mem::size_of::<u32>() as u32;
        let status = AudioObjectGetPropertyData(
            object_id,
            &address,
            0,
            ptr::null(),
            &mut size,
            &mut value as *mut u32 as *mut c_void,
        );
        if status != NO_ERR {
            return Err(format!("AudioObjectGetPropertyData({selector:#x}) failed: {status}"));
        }
        Ok(value)
    }

    fn get_device_list() -> Result<Vec<AudioDeviceID>, String> {
        let address = prop_addr(K_AUDIO_HARDWARE_PROPERTY_DEVICES);
        let mut size = 0u32;
        let status = AudioObjectGetPropertyData(
            K_AUDIO_OBJECT_SYSTEM_OBJECT,
            &address,
            0,
            ptr::null(),
            &mut size,
            ptr::null_mut(),
        );
        if status != NO_ERR {
            return Err(format!("device list size query failed: {status}"));
        }
        let count = size as usize / std::mem::size_of::<AudioDeviceID>();
        let mut devices = vec![0u32; count];
        let status = AudioObjectGetPropertyData(
            K_AUDIO_OBJECT_SYSTEM_OBJECT,
            &address,
            0,
            ptr::null(),
            &mut size,
            devices.as_mut_ptr() as *mut c_void,
        );
        if status != NO_ERR {
            return Err(format!("device list read failed: {status}"));
        }
        Ok(devices)
    }

    fn get_default_output_device() -> Result<AudioDeviceID, String> {
        let address = prop_addr(K_AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE);
        let mut device = 0u32;
        let mut size = std::mem::size_of::<AudioDeviceID>() as u32;
        let status = AudioObjectGetPropertyData(
            K_AUDIO_OBJECT_SYSTEM_OBJECT,
            &address,
            0,
            ptr::null(),
            &mut size,
            &mut device as *mut u32 as *mut c_void,
        );
        if status != NO_ERR || device == 0 {
            return Err(format!("default output device query failed: {status}"));
        }
        Ok(device)
    }

    fn set_default_output_device(device: AudioDeviceID) -> Result<(), String> {
        let address = prop_addr(K_AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE);
        let status = AudioObjectSetPropertyData(
            K_AUDIO_OBJECT_SYSTEM_OBJECT,
            &address,
            0,
            ptr::null(),
            std::mem::size_of::<AudioDeviceID>() as u32,
            &device as *const u32 as *const c_void,
        );
        if status != NO_ERR {
            return Err(format!("set default output device failed: {status}"));
        }
        Ok(())
    }

    fn get_cf_string(object_id: AudioObjectID, selector: u32) -> Result<String, String> {
        let address = prop_addr(selector);
        let mut cf_str: *const c_void = ptr::null();
        let mut size = std::mem::size_of::<*const c_void>() as u32;
        let status = AudioObjectGetPropertyData(
            object_id,
            &address,
            0,
            ptr::null(),
            &mut size,
            &mut cf_str as *mut *const c_void as *mut c_void,
        );
        if status != NO_ERR || cf_str.is_null() {
            return Err(format!("CFString property {selector:#x} failed: {status}"));
        }
        #[link(name = "CoreFoundation", kind = "framework")]
        extern "C" {
            fn CFStringGetCStringPtr(cf_str: *const c_void, encoding: u32) -> *const i8;
            fn CFStringGetCString(
                cf_str: *const c_void,
                buffer: *mut i8,
                buffer_size: isize,
                encoding: u32,
            ) -> bool;
            fn CFRelease(cf: *const c_void);
        }
        const K_CF_STRING_ENCODING_UTF8: u32 = 0x0800_0100;
        let ptr_utf8 = CFStringGetCStringPtr(cf_str, K_CF_STRING_ENCODING_UTF8);
        let value = if !ptr_utf8.is_null() {
            std::ffi::CStr::from_ptr(ptr_utf8)
                .to_string_lossy()
                .into_owned()
        } else {
            let mut buf = [0i8; 512];
            if !CFStringGetCString(
                cf_str,
                buf.as_mut_ptr(),
                buf.len() as isize,
                K_CF_STRING_ENCODING_UTF8,
            ) {
                CFRelease(cf_str);
                return Err("CFStringGetCString failed".into());
            }
            std::ffi::CStr::from_ptr(buf.as_ptr())
                .to_string_lossy()
                .into_owned()
        };
        CFRelease(cf_str);
        Ok(value)
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
        let list = unsafe { &*buffer_list };
        for index in 0..list.m_number_buffers as usize {
            let buffer = &list.m_buffers[index];
            if !buffer.m_data.is_null() && buffer.m_data_byte_size > 0 {
                unsafe {
                    ptr::write_bytes(
                        buffer.m_data,
                        0,
                        buffer.m_data_byte_size as usize,
                    );
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
            let counter = unsafe { &*(in_client_data as *const AtomicU64) };
            counter.fetch_add(1, Ordering::Relaxed);
        }
        zero_output_buffers(out_output_data);
        NO_ERR
    }

    impl IoCycleMonitor {
        fn start(device: AudioDeviceID, counter: *mut AtomicU64) -> Result<Self, String> {
            let mut proc_id: AudioDeviceIOProcID = ptr::null_mut();
            let status = AudioDeviceCreateIOProcID(
                device,
                device_io_proc,
                counter as *mut c_void,
                &mut proc_id,
            );
            if status != NO_ERR {
                return Err(format!("AudioDeviceCreateIOProcID failed: {status}"));
            }
            let status = AudioDeviceStart(device, proc_id);
            if status != NO_ERR {
                let _ = AudioDeviceDestroyIOProcID(device, proc_id);
                return Err(format!("AudioDeviceStart failed: {status}"));
            }
            Ok(Self {
                device,
                proc_id,
                counter,
            })
        }

        fn count(&self) -> u64 {
            unsafe { (*self.counter).load(Ordering::Relaxed) }
        }
    }

    impl Drop for IoCycleMonitor {
        fn drop(&mut self) {
            let _ = AudioDeviceStop(self.device, self.proc_id);
            let _ = AudioDeviceDestroyIOProcID(self.device, self.proc_id);
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
        let samples = unsafe {
            std::slice::from_raw_parts_mut(buffer.m_data as *mut i16, frames * channels)
        };
        let phase_inc = 2.0 * std::f32::consts::PI * state.freq_hz / state.sample_rate;
        for frame in 0..frames {
            let value =
                (state.amplitude * state.phase.sin() * 32_767.0).clamp(-32_767.0, 32_767.0) as i16;
            state.phase += phase_inc;
            if state.phase > 2.0 * std::f32::consts::PI {
                state.phase -= 2.0 * std::f32::consts::PI;
            }
            for channel in 0..channels {
                samples[frame * channels + channel] = value;
            }
        }
        NO_ERR
    }

    fn play_probe_tone_hal(device: AudioDeviceID) -> Result<(), String> {
        let desc = AudioComponentDescription {
            component_type: K_AUDIO_UNIT_TYPE_OUTPUT,
            component_sub_type: K_AUDIO_UNIT_SUBTYPE_HAL_OUTPUT,
            component_manufacturer: 0,
            component_flags: 0,
            component_flags_mask: 0,
        };
        let component = AudioComponentFindNext(ptr::null_mut(), &desc);
        if component.is_null() {
            return Err("HAL output AudioUnit component not found".into());
        }
        let mut unit: AudioUnit = ptr::null_mut();
        let status = AudioComponentInstanceNew(component, &mut unit);
        if status != NO_ERR || unit.is_null() {
            return Err(format!("AudioComponentInstanceNew failed: {status}"));
        }

        let result = (|| {
            let enable = 1u32;
            let disable = 0u32;
            let status = AudioUnitSetProperty(
                unit,
                K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO,
                K_AUDIO_UNIT_SCOPE_OUTPUT,
                0,
                &enable as *const u32 as *const c_void,
                std::mem::size_of::<u32>() as u32,
            );
            if status != NO_ERR {
                return Err(format!("enable HAL output IO failed: {status}"));
            }
            let status = AudioUnitSetProperty(
                unit,
                K_AUDIO_OUTPUT_UNIT_PROPERTY_ENABLE_IO,
                K_AUDIO_UNIT_SCOPE_INPUT,
                1,
                &disable as *const u32 as *const c_void,
                std::mem::size_of::<u32>() as u32,
            );
            if status != NO_ERR {
                return Err(format!("disable HAL input IO failed: {status}"));
            }
            let dev = device;
            let status = AudioUnitSetProperty(
                unit,
                K_AUDIO_OUTPUT_UNIT_PROPERTY_CURRENT_DEVICE,
                K_AUDIO_UNIT_SCOPE_GLOBAL,
                0,
                &dev as *const u32 as *const c_void,
                std::mem::size_of::<u32>() as u32,
            );
            if status != NO_ERR {
                return Err(format!("set HAL current device failed: {status}"));
            }

            let fmt = AudioStreamBasicDescription {
                m_sample_rate: PROBE_SAMPLE_RATE,
                m_format_id: K_AUDIO_FORMAT_LINEAR_PCM,
                m_format_flags: K_AUDIO_FORMAT_FLAG_IS_SIGNED_INTEGER | K_AUDIO_FORMAT_FLAG_IS_PACKED,
                m_bytes_per_packet: 2,
                m_frames_per_packet: 1,
                m_bytes_per_frame: 2,
                m_channels_per_frame: 1,
                m_bits_per_channel: 16,
                m_reserved: 0,
            };
            let status = AudioUnitSetProperty(
                unit,
                K_AUDIO_UNIT_PROPERTY_STREAM_FORMAT,
                K_AUDIO_UNIT_SCOPE_OUTPUT,
                0,
                &fmt as *const AudioStreamBasicDescription as *const c_void,
                std::mem::size_of::<AudioStreamBasicDescription>() as u32,
            );
            if status != NO_ERR {
                return Err(format!("set HAL stream format failed: {status}"));
            }

            let mut tone_state = ProbeToneState {
                phase: 0.0,
                sample_rate: PROBE_SAMPLE_RATE as f32,
                amplitude: PROBE_AMPLITUDE,
                freq_hz: PROBE_FREQ_HZ,
            };
            let callback = AURenderCallbackStruct {
                input_proc: probe_render_callback,
                input_proc_ref_con: &mut tone_state as *mut ProbeToneState as *mut c_void,
            };
            let status = AudioUnitSetProperty(
                unit,
                K_AUDIO_UNIT_PROPERTY_SET_RENDER_CALLBACK,
                K_AUDIO_UNIT_SCOPE_OUTPUT,
                0,
                &callback as *const AURenderCallbackStruct as *const c_void,
                std::mem::size_of::<AURenderCallbackStruct>() as u32,
            );
            if status != NO_ERR {
                return Err(format!("set HAL render callback failed: {status}"));
            }

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
        })();

        let _ = AudioComponentInstanceDispose(unit);
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
        let reason = if delivering {
            String::new()
        } else if hal_overload {
            "Default output device is not advancing IO cycles; recent HAL overload signatures were seen in the system log".into()
        } else {
            "Default output device is not delivering audio (IO cycles did not advance during probe tone)".into()
        };
        let verdict = if delivering { "ok" } else { "not_delivering" };
        Ok(serde_json::json!({
            "device_delivering": delivering,
            "verdict": verdict,
            "reason": if reason.is_empty() { serde_json::Value::Null } else { serde_json::Value::String(reason) },
            "default_device_name": name,
            "default_device_uid": uid,
            "io_cycles_advanced": io_cycles_advanced,
            "hal_overload_recent": hal_overload,
            "probe_available": true,
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
        let devices = get_device_list()?;
        let alternate = devices
            .into_iter()
            .find(|id| *id != 0 && *id != current)
            .ok_or_else(|| "only one output device available; cannot cycle".to_string())?;
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
