//! A small C ABI over [`Analyzer`], for the Swift app (feature `ffi`).
//!
//! Lifecycle: `odj_live_new` -> any number of `odj_live_feed` /
//! `odj_live_next_frame` -> `odj_live_finish` (summary JSON) ->
//! `odj_live_free`. Every string returned is a NUL-terminated JSON object
//! owned by the caller and released with `odj_live_string_free`. Header:
//! `include/odj_live.h`.
//!
//! No panic crosses the boundary: each entry point catches one and reports
//! it as its error value (`NULL` or a negative count).

use std::ffi::{c_char, CString};
use std::panic::{catch_unwind, AssertUnwindSafe};

use crate::Analyzer;

/// Opaque handle.
pub struct OdjLive {
    analyzer: Analyzer,
    finished: bool,
    /// Why `odj_live_finish` failed, kept after the handle is finished so a
    /// caller can tell a failure from a second finish.
    error: Option<String>,
    /// Why the last `odj_live_next_frame` returned NULL for a failure rather
    /// than an empty queue. Cleared by every call that does not fail.
    frame_error: Option<String>,
    /// A panic while feeding, waiting for the next `odj_live_next_frame`.
    feed_panic: Option<String>,
}

fn to_c(json: String) -> *mut c_char {
    // serde_json never writes a NUL byte inside a string it produced.
    CString::new(json).map(CString::into_raw).unwrap_or(std::ptr::null_mut())
}

/// A new analyzer for mono input at `sample_rate`, emitting `hz` frames per
/// second, keeping the last [`crate::STREAM_HISTORY_S`] for its summary
/// ([`Analyzer::new_stream`]). `NULL` when either is out of range.
#[no_mangle]
pub extern "C" fn odj_live_new(sample_rate: u32, hz: f64) -> *mut OdjLive {
    catch_unwind(|| match Analyzer::new_stream(sample_rate, hz) {
        Ok(analyzer) => Box::into_raw(Box::new(OdjLive {
            analyzer,
            finished: false,
            error: None,
            frame_error: None,
            feed_panic: None,
        })),
        Err(_) => std::ptr::null_mut(),
    })
    .unwrap_or(std::ptr::null_mut())
}

/// Feed `n` mono samples. Returns the number of frames waiting, or -1 for a
/// null handle or samples pointer, a finished analyzer, or a non-finite
/// sample (the block is then dropped whole).
///
/// # Safety
/// `handle` must come from `odj_live_new` and not be freed; `samples` must
/// point at `n` readable floats.
#[no_mangle]
pub unsafe extern "C" fn odj_live_feed(handle: *mut OdjLive, samples: *const f32, n: usize) -> i64 {
    if handle.is_null() || samples.is_null() {
        return -1;
    }
    let h = &mut *handle;
    if h.finished {
        return -1;
    }
    let block = std::slice::from_raw_parts(samples, n);
    feed_guarded(h, |analyzer| analyzer.push(block))
}

/// Run one feed. Frames are made while feeding, so a panic here is a failed
/// frame: it is kept for the next `odj_live_next_frame` to report.
fn feed_guarded<E>(h: &mut OdjLive, feed: impl FnOnce(&mut Analyzer) -> Result<(), E>) -> i64 {
    let analyzer = &mut h.analyzer;
    match catch_unwind(AssertUnwindSafe(|| {
        feed(analyzer).map(|()| analyzer.pending_frames() as i64)
    })) {
        Ok(Ok(waiting)) => waiting,
        Ok(Err(_)) => -1,
        Err(_) => {
            h.feed_panic = Some("the analyzer panicked while making a frame".into());
            -1
        }
    }
}

/// Move a held feed panic into `frame_error`; true when there was one.
fn surface_feed_panic(h: &mut OdjLive) -> bool {
    match h.feed_panic.take() {
        Some(message) => {
            h.frame_error = Some(message);
            true
        }
        None => false,
    }
}

/// The oldest waiting frame as JSON. `NULL` means either no frame is waiting
/// or the frame failed; `odj_live_frame_error` tells them apart.
///
/// # Safety
/// `handle` must come from `odj_live_new` and not be freed.
#[no_mangle]
pub unsafe extern "C" fn odj_live_next_frame(handle: *mut OdjLive) -> *mut c_char {
    if handle.is_null() {
        return std::ptr::null_mut();
    }
    let h = &mut *handle;
    if surface_feed_panic(h) {
        return std::ptr::null_mut();
    }
    let made = catch_unwind(AssertUnwindSafe(|| match h.analyzer.next_frame() {
        Some(f) => serde_json::to_string(&f)
            .map(Some)
            .map_err(|e| format!("frame did not serialize: {e}")),
        None => Ok(None),
    }));
    match made {
        Ok(Ok(Some(json))) => match CString::new(json) {
            Ok(s) => {
                h.frame_error = None;
                s.into_raw()
            }
            Err(_) => {
                h.frame_error = Some("frame JSON contained a NUL byte".into());
                std::ptr::null_mut()
            }
        },
        Ok(Ok(None)) => {
            h.frame_error = None;
            std::ptr::null_mut()
        }
        Ok(Err(message)) => {
            h.frame_error = Some(message);
            std::ptr::null_mut()
        }
        Err(_) => {
            h.frame_error = Some("the analyzer panicked while making a frame".into());
            std::ptr::null_mut()
        }
    }
}

/// Why the last `odj_live_next_frame` on this handle returned NULL for a
/// failure, as a JSON object `{"error": "<message>"}`. `NULL` when that call
/// returned a frame or found the queue empty, or when the handle is NULL.
/// Release the string with `odj_live_string_free`.
///
/// # Safety
/// `handle` must be a live handle from `odj_live_new`, or NULL.
#[no_mangle]
pub unsafe extern "C" fn odj_live_frame_error(handle: *const OdjLive) -> *mut c_char {
    if handle.is_null() {
        return std::ptr::null_mut();
    }
    match &(*handle).frame_error {
        Some(message) => to_c(serde_json::json!({ "error": message }).to_string()),
        None => std::ptr::null_mut(),
    }
}

/// Frames dropped because they waited more than `MAX_PENDING_S` (60 s of stream time)
/// undrained; 0 for a null handle.
///
/// # Safety
/// `handle` must come from `odj_live_new` and not be freed.
#[no_mangle]
pub unsafe extern "C" fn odj_live_dropped_frames(handle: *const OdjLive) -> u64 {
    if handle.is_null() {
        return 0;
    }
    (*handle).analyzer.dropped_frames()
}

/// End of stream: the summary as JSON (`NULL` if already finished). Frames
/// made by the flush stay available to `odj_live_next_frame`.
///
/// # Safety
/// `handle` must come from `odj_live_new` and not be freed.
#[no_mangle]
pub unsafe extern "C" fn odj_live_finish(handle: *mut OdjLive) -> *mut c_char {
    if handle.is_null() {
        return std::ptr::null_mut();
    }
    let h = &mut *handle;
    if h.finished {
        return std::ptr::null_mut();
    }
    h.finished = true;
    match catch_unwind(AssertUnwindSafe(|| h.analyzer.finish())) {
        Ok(Ok(summary)) => match serde_json::to_string(&summary) {
            Ok(json) => to_c(json),
            Err(e) => {
                h.error = Some(format!("summary did not serialize: {e}"));
                std::ptr::null_mut()
            }
        },
        Ok(Err(e)) => {
            h.error = Some(e);
            std::ptr::null_mut()
        }
        Err(_) => {
            h.error = Some("the analyzer panicked while finishing".into());
            std::ptr::null_mut()
        }
    }
}

/// Why the last `odj_live_finish` on this handle returned NULL: a failure in
/// the analyzer, a summary that would not serialize, or a panic. `NULL` when
/// it finished cleanly, when it has not finished, or when the handle is NULL.
/// Release the string with `odj_live_string_free`. The string is a JSON
/// object, `{"error": "<message>"}`, like every other string this library
/// returns.
///
/// # Safety
/// `handle` must be a live handle from `odj_live_new`, or NULL.
#[no_mangle]
pub unsafe extern "C" fn odj_live_last_error(handle: *const OdjLive) -> *mut c_char {
    if handle.is_null() {
        return std::ptr::null_mut();
    }
    match &(*handle).error {
        Some(message) => to_c(serde_json::json!({ "error": message }).to_string()),
        None => std::ptr::null_mut(),
    }
}

/// Release a string this library returned. `NULL` is ignored.
///
/// # Safety
/// `s` must come from this library and not be released twice.
#[no_mangle]
pub unsafe extern "C" fn odj_live_string_free(s: *mut c_char) {
    if !s.is_null() {
        drop(CString::from_raw(s));
    }
}

/// Release the analyzer. `NULL` is ignored.
///
/// # Safety
/// `handle` must come from `odj_live_new` and not be freed twice.
#[no_mangle]
pub unsafe extern "C" fn odj_live_free(handle: *mut OdjLive) {
    if !handle.is_null() {
        drop(Box::from_raw(handle));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_panic_while_feeding_reaches_the_next_frame_error() {
        let mut h = OdjLive {
            analyzer: Analyzer::new_stream(48_000, 10.0).expect("valid stream arguments"),
            finished: false,
            error: None,
            frame_error: None,
            feed_panic: None,
        };
        let fed: i64 = feed_guarded(&mut h, |_| -> Result<(), String> {
            panic!("frame blew up");
        });
        assert_eq!(fed, -1);
        assert!(surface_feed_panic(&mut h));
        let message = h.frame_error.as_deref().expect("the panic must not read as an empty queue");
        assert!(message.contains("panicked"), "{message}");
        assert!(!surface_feed_panic(&mut h), "reported once");
    }
}
