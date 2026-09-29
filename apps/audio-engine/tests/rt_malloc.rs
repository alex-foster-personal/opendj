//! Real-time safety of the C++ inside the render path (Signalsmith Stretch).
//!
//! `rt_alloc.rs` counts through Rust's global allocator, which C++ `new` and
//! `std::vector` never reach. This test interposes the C allocator itself, so
//! it sees every allocation from Rust and C++ alike, and watches renders with
//! Master Tempo and key shift on. Linux glibc only: interposition needs the
//! binary to export `malloc` (see `build.rs`), and glibc's `__libc_*` entry
//! points to forward to. One test per file, so no other test thread
//! allocates inside the window.

#![cfg(all(target_os = "linux", target_env = "gnu"))]
// The interposed allocator functions are called only by libc's callers,
// under the C contracts of malloc(3); there is nothing further to document.
#![allow(clippy::missing_safety_doc)]

use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use odj_audio::deck::{Beat, Track};
use odj_audio::engine::{Engine, EngineCmd};

static ARMED: AtomicBool = AtomicBool::new(false);
static MALLOCS: AtomicUsize = AtomicUsize::new(0);

extern "C" {
    fn __libc_malloc(n: usize) -> *mut c_void;
    fn __libc_calloc(n: usize, s: usize) -> *mut c_void;
    fn __libc_realloc(p: *mut c_void, n: usize) -> *mut c_void;
    fn __libc_memalign(a: usize, n: usize) -> *mut c_void;
}

fn count() {
    if ARMED.load(Ordering::Relaxed) {
        MALLOCS.fetch_add(1, Ordering::Relaxed);
    }
}

#[no_mangle]
pub unsafe extern "C" fn malloc(n: usize) -> *mut c_void {
    count();
    __libc_malloc(n)
}

#[no_mangle]
pub unsafe extern "C" fn calloc(n: usize, s: usize) -> *mut c_void {
    count();
    __libc_calloc(n, s)
}

#[no_mangle]
pub unsafe extern "C" fn realloc(p: *mut c_void, n: usize) -> *mut c_void {
    count();
    __libc_realloc(p, n)
}

#[no_mangle]
pub unsafe extern "C" fn posix_memalign(out: *mut *mut c_void, a: usize, n: usize) -> i32 {
    count();
    let p = __libc_memalign(a, n);
    if p.is_null() {
        return 12; // ENOMEM
    }
    *out = p;
    0
}

#[no_mangle]
pub unsafe extern "C" fn aligned_alloc(a: usize, n: usize) -> *mut c_void {
    count();
    __libc_memalign(a, n)
}

fn track(hz: f64) -> Arc<Track> {
    let sr = 48000u32;
    let frames = sr as usize * 20;
    let pcm: Vec<f32> = (0..frames)
        .flat_map(|i| {
            let v = (2.0 * std::f64::consts::PI * hz * i as f64 / sr as f64).sin() as f32 * 0.5;
            [v, v]
        })
        .collect();
    let beats = (0..40).map(|i| Beat { time_ms: i as f64 * 500.0, downbeat: i % 4 == 0, bpm: None }).collect();
    Arc::new(Track::new(sr, pcm, beats, None))
}

#[test]
fn stretching_decks_do_not_allocate_in_c_or_rust() {
    // Positive control first: the interposer must see an allocation, or a
    // zero below proves nothing.
    ARMED.store(true, Ordering::SeqCst);
    let v: Vec<u8> = std::hint::black_box(Vec::with_capacity(4096));
    let b = std::hint::black_box(Box::new([0u8; 64]));
    ARMED.store(false, Ordering::SeqCst);
    drop((v, b));
    assert!(MALLOCS.load(Ordering::SeqCst) >= 2, "the malloc interposer did not fire; the check below would be blind");
    // Second control: it sees C++ allocations too (the stretcher's own
    // buffers, allocated through libstdc++ `new` when it is built).
    MALLOCS.store(0, Ordering::SeqCst);
    ARMED.store(true, Ordering::SeqCst);
    let s = std::hint::black_box(odj_audio::stretch::Stretcher::new(48000.0, 0));
    ARMED.store(false, Ordering::SeqCst);
    drop(s);
    let cpp = MALLOCS.load(Ordering::SeqCst);
    assert!(cpp > 4, "only {cpp} allocations seen while building a stretcher; C++ `new` is not reaching the interposer");
    MALLOCS.store(0, Ordering::SeqCst);

    let mut e = Engine::new(48000);
    let (a, b) = (track(220.0), track(330.0));
    let mut buf = vec![0.0f32; 256 * 2];
    e.apply(EngineCmd::Load { deck: 1, track: a.clone() }).unwrap();
    e.apply(EngineCmd::Load { deck: 2, track: b.clone() }).unwrap();

    ARMED.store(true, Ordering::SeqCst);
    for c in [
        EngineCmd::MasterTempo { deck: 1, enabled: true },
        EngineCmd::Play { deck: 1, playing: true },
        EngineCmd::Play { deck: 2, playing: true },
        EngineCmd::Tempo { deck: 1, ratio: 1.06 },
        EngineCmd::KeyNudge { deck: 2, semitones: 1 },
        EngineCmd::BeatLoop { deck: 1, beats: 2.0, start_ms: None },
    ] {
        e.apply(c).unwrap();
    }
    for i in 0..1000 {
        if i == 300 {
            // Switch paths mid-play both ways, and jump: each primes or
            // crossfades the stretcher.
            e.apply(EngineCmd::MasterTempo { deck: 1, enabled: false }).unwrap();
            e.apply(EngineCmd::KeyNudge { deck: 2, semitones: -1 }).unwrap();
        }
        if i == 600 {
            e.apply(EngineCmd::MasterTempo { deck: 1, enabled: true }).unwrap();
            e.apply(EngineCmd::Seek { deck: 2, position_ms: 5000.0 }).unwrap();
            e.apply(EngineCmd::KeyNudge { deck: 2, semitones: -1 }).unwrap();
        }
        e.render(&mut buf);
    }
    ARMED.store(false, Ordering::SeqCst);
    let n = MALLOCS.load(Ordering::SeqCst);
    assert_eq!(n, 0, "{n} allocations inside apply/render with the stretcher running");
    // Control: the decks were actually stretching and audible.
    let d1 = e.snapshot().decks[0];
    assert!(d1.playing && d1.master_tempo);
    assert!(buf.iter().any(|&x| x.abs() > 0.05));
}
