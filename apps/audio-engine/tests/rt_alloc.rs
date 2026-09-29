//! Real-time safety: `Engine::apply` and `Engine::render` must not allocate.
//!
//! A counting global allocator watches a window that covers commands and
//! 1000 renders with two decks playing, EQ, filter and crossfader moving, and
//! a loop wrapping. This file holds one test so no other test thread can
//! allocate inside the window.

use std::alloc::{GlobalAlloc, Layout, System};
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use odj_audio::deck::{Beat, Track};
use odj_audio::engine::{Engine, EngineCmd, EqBand};
use odj_audio::mixer::Assign;

struct Counting;

static ARMED: AtomicBool = AtomicBool::new(false);
static ALLOCS: AtomicUsize = AtomicUsize::new(0);

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        if ARMED.load(Ordering::Relaxed) {
            ALLOCS.fetch_add(1, Ordering::Relaxed);
        }
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        if ARMED.load(Ordering::Relaxed) {
            ALLOCS.fetch_add(1, Ordering::Relaxed);
        }
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        if ARMED.load(Ordering::Relaxed) {
            ALLOCS.fetch_add(1, Ordering::Relaxed);
        }
        System.realloc(p, l, n)
    }
}

#[global_allocator]
static GLOBAL: Counting = Counting;

fn track(hz: f64) -> Arc<Track> {
    let sr = 44100u32;
    let frames = sr as usize * 20;
    let mut pcm = Vec::with_capacity(frames * 2);
    for i in 0..frames {
        let v = (2.0 * std::f64::consts::PI * hz * i as f64 / sr as f64).sin() as f32 * 0.5;
        pcm.push(v);
        pcm.push(v);
    }
    let beats = (0..40).map(|i| Beat { time_ms: i as f64 * 500.0, downbeat: i % 4 == 0, bpm: None }).collect();
    Arc::new(Track::new(sr, pcm, beats, None))
}

#[test]
fn apply_and_render_do_not_allocate() {
    let mut e = Engine::new(48000);
    let (a, b) = (track(220.0), track(330.0));
    // The replacement track is built BEFORE the window, and the one it
    // replaces is kept alive in `retired` so its drop happens after.
    let replacement = track(440.0);
    let mut buf = vec![0.0f32; 256 * 2];

    e.apply(EngineCmd::Load { deck: 1, track: a.clone() }).unwrap();
    e.apply(EngineCmd::Load { deck: 2, track: b.clone() }).unwrap();

    ARMED.store(true, Ordering::SeqCst);
    let before = ALLOCS.load(Ordering::SeqCst);
    for c in [
        EngineCmd::Assign { deck: 1, assign: Assign::A },
        EngineCmd::Assign { deck: 2, assign: Assign::B },
        EngineCmd::Play { deck: 1, playing: true },
        EngineCmd::Play { deck: 2, playing: true },
        EngineCmd::Tempo { deck: 2, ratio: 1.06 },
        EngineCmd::BeatLoop { deck: 1, beats: 2.0, start_ms: None },
    ] {
        e.apply(c).unwrap();
    }
    for i in 0..1000 {
        let x = (i % 100) as f64 / 100.0;
        e.apply(EngineCmd::Crossfader { value: x }).unwrap();
        e.apply(EngineCmd::Eq { deck: 1, band: EqBand::Low, value: 1.0 - x }).unwrap();
        e.apply(EngineCmd::Filter { deck: 2, value: x }).unwrap();
        e.render(&mut buf);
    }
    let retired = e.apply(EngineCmd::Load { deck: 1, track: replacement.clone() }).unwrap();
    e.render(&mut buf);
    let after = ALLOCS.load(Ordering::SeqCst);
    ARMED.store(false, Ordering::SeqCst);

    assert!(retired.is_some(), "the replaced track must come back to the caller");
    // Control: the window can see an allocation at all.
    ARMED.store(true, Ordering::SeqCst);
    let probe = ALLOCS.load(Ordering::SeqCst);
    let v: Vec<u8> = Vec::with_capacity(64);
    let seen = ALLOCS.load(Ordering::SeqCst) - probe;
    ARMED.store(false, Ordering::SeqCst);
    drop(v);
    assert!(seen >= 1, "counting allocator did not fire; the zero below would mean nothing");

    assert_eq!(after - before, 0, "apply/render allocated {} times", after - before);
    assert!(buf.iter().any(|&s| s != 0.0), "nothing rendered; the window measured silence");
}
