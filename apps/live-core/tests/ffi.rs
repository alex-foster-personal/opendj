//! The C ABI (feature `ffi`), called as the Swift app will call it.
#![cfg(feature = "ffi")]

mod common;

use common::*;
use odj_live_core::ffi::*;
use std::ffi::CStr;

unsafe fn take(s: *mut std::ffi::c_char) -> Option<serde_json::Value> {
    if s.is_null() {
        return None;
    }
    let v = serde_json::from_str(CStr::from_ptr(s).to_str().unwrap()).unwrap();
    odj_live_string_free(s);
    Some(v)
}

#[test]
fn feed_frames_and_finish_through_the_c_abi() {
    let mut buf = silence(12.0);
    add_progression(&mut buf, C_MAJOR, 2.0, 0.0, 12.0);
    normalize(&mut buf);
    unsafe {
        let h = odj_live_new(RATE, 10.0);
        assert!(!h.is_null());
        let mut frames = vec![];
        for block in buf.chunks(1000) {
            let pending = odj_live_feed(h, block.as_ptr(), block.len());
            assert!(pending >= 0);
            while let Some(f) = take(odj_live_next_frame(h)) {
                frames.push(f);
            }
        }
        let summary = take(odj_live_finish(h)).unwrap();
        while let Some(f) = take(odj_live_next_frame(h)) {
            frames.push(f);
        }
        assert_eq!(frames.len(), 120);
        assert_eq!(odj_live_dropped_frames(h), 0);
        assert_eq!(frames.last().unwrap()["key"], "8B");
        assert_eq!(summary["type"], "summary");
        assert_eq!(summary["key"], "8B");
        // Finished: no second summary, no more feeding.
        assert!(odj_live_finish(h).is_null());
        assert_eq!(odj_live_feed(h, buf.as_ptr(), 10), -1);
        odj_live_free(h);
    }
}

#[test]
fn bad_arguments_are_refused_not_undefined() {
    unsafe {
        assert!(odj_live_new(44100, 0.0).is_null());
        assert!(odj_live_new(100, 10.0).is_null());
        assert_eq!(odj_live_feed(std::ptr::null_mut(), [0f32].as_ptr(), 1), -1);
        assert!(odj_live_next_frame(std::ptr::null_mut()).is_null());
        assert!(odj_live_finish(std::ptr::null_mut()).is_null());
        odj_live_string_free(std::ptr::null_mut());
        odj_live_free(std::ptr::null_mut());
        assert_eq!(odj_live_dropped_frames(std::ptr::null()), 0);
        let h = odj_live_new(22050, 10.0);
        assert_eq!(odj_live_feed(h, std::ptr::null(), 5), -1);
        // A null samples pointer is -1 whatever n says; an empty block at a
        // real pointer is fine.
        assert_eq!(odj_live_feed(h, std::ptr::null(), 0), -1);
        let empty: [f32; 0] = [];
        assert_eq!(odj_live_feed(h, empty.as_ptr(), 0), 0);
        let bad = [0.0f32, f32::NAN];
        assert_eq!(odj_live_feed(h, bad.as_ptr(), 2), -1);
        odj_live_free(h);
    }
}

#[test]
fn an_empty_queue_is_not_a_frame_error() {
    // A NULL from odj_live_next_frame is an empty queue unless the frame
    // error says otherwise: nothing was fed, so nothing is waiting and
    // nothing failed.
    unsafe {
        let h = odj_live_new(RATE, 10.0);
        assert!(odj_live_next_frame(h).is_null());
        assert!(odj_live_frame_error(h).is_null(), "an empty queue records no error");
        assert!(odj_live_frame_error(std::ptr::null()).is_null());
        odj_live_free(h);
    }
}

#[test]
fn last_error_is_null_when_finish_succeeds_or_is_repeated() {
    unsafe {
        assert!(odj_live_last_error(std::ptr::null()).is_null());
        let h = odj_live_new(RATE, 10.0);
        assert!(odj_live_last_error(h).is_null(), "no error before finishing");
        assert!(take(odj_live_finish(h)).is_some());
        assert!(odj_live_last_error(h).is_null(), "a clean finish records no error");
        // A second finish is lifecycle misuse, not a failure: NULL, and still no error.
        assert!(odj_live_finish(h).is_null());
        assert!(odj_live_last_error(h).is_null(), "a repeated finish is not a failure");
        odj_live_free(h);
    }
}
