//! A load holds one copy of a track's samples at a time, never two: the
//! decoder's buffer becomes the shared track as it is, not copied into a new
//! allocation first (Codex on 0f81c719: `Arc<[f32]>::from(Vec)` held the
//! decoded buffer and its copy at once, twice a track near the budget).
//!
//! A tracking global allocator records the most bytes live during each load.
//! A `realloc` counts as its size change, trusting the system to grow in
//! place where it can; the copy under test is an explicit second allocation,
//! which that cannot hide. This file holds one test so no other test thread
//! allocates inside the window.

mod common;

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicUsize, Ordering};

use common::{sine, temp_dir, write_wav};
use odj_audio::protocol::LoadSpec;

struct Tracking;

static LIVE: AtomicUsize = AtomicUsize::new(0);
static PEAK: AtomicUsize = AtomicUsize::new(0);

fn grew(by: usize) {
    let now = LIVE.fetch_add(by, Ordering::SeqCst) + by;
    PEAK.fetch_max(now, Ordering::SeqCst);
}

unsafe impl GlobalAlloc for Tracking {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        grew(l.size());
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        LIVE.fetch_sub(l.size(), Ordering::SeqCst);
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        if n > l.size() {
            grew(n - l.size());
        } else {
            LIVE.fetch_sub(l.size() - n, Ordering::SeqCst);
        }
        System.realloc(p, l, n)
    }
}

#[global_allocator]
static GLOBAL: Tracking = Tracking;

#[test]
fn a_load_holds_one_copy_of_the_samples() {
    let d = temp_dir("load-peak");
    // 20 s at 48 kHz: 1.92 M samples, 7.7 MB as f32.
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 20.0));
    let spec = LoadSpec { deck: 1, path: "a.wav".into(), beats: vec![], bpm: None };
    // Decoder state, file buffers and the like, beside the samples.
    let slack = 2 << 20;
    type Load<'a> = Box<dyn FnMut() -> std::sync::Arc<odj_audio::deck::Track> + 'a>;
    let mut offline = odj_audio::offline::file_loader(d.clone());
    let mut session = odj_audio::offline::session_loader(d.clone());
    let loads: Vec<(&str, Load)> = vec![
        ("offline", Box::new(|| offline(&spec, u64::MAX).unwrap())),
        ("session", Box::new(|| session(&spec).unwrap())),
    ];
    for (name, mut load) in loads {
        let base = LIVE.load(Ordering::SeqCst);
        PEAK.store(base, Ordering::SeqCst);
        let track = load();
        let peak = PEAK.load(Ordering::SeqCst) - base;
        let held = track.pcm.capacity() * 4;
        let samples = track.pcm.len() * 4;
        assert_eq!(samples, 20 * 48000 * 2 * 4);
        // The most live at once is the buffer the track keeps, not that
        // plus a copy of its samples.
        assert!(peak <= held + slack, "{name}: peaked at {peak} bytes for {samples} bytes of samples held in {held}");
        // Control: the measurement sees the samples at all.
        assert!(peak >= samples, "{name}: peaked at {peak} bytes, under the {samples} bytes of samples");
    }
    let _ = std::fs::remove_dir_all(&d);
}
