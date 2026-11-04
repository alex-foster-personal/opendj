//! Real-time safety of the C++ inside the render path (Signalsmith Stretch)
//! on Windows: the MSVC twin of `rt_malloc.rs`, which is Linux glibc only.
//!
//! `rt_malloc.rs` interposes glibc's `malloc`, which a Windows binary cannot
//! do: the C runtime is a DLL and its `malloc` is reached through the import
//! table, not by a symbol this test can define. What MSVC does let a program
//! replace is the global `operator new` and `operator delete` family, and
//! that is what C++ in this binary allocates through (`std::vector`, `new`).
//! The functions below are those operators under their MSVC x64 mangled
//! names, so the linker resolves the stretcher's allocations to them.
//!
//! What this sees: every `operator new` / `operator delete` made by C++
//! linked into this binary, and (through the counting global allocator)
//! every Rust allocation. What it cannot see: a direct `malloc` call from
//! C or C++, and allocations made inside the C++ runtime DLL itself. The
//! second control below is what shows the stretcher's own buffers are in
//! view; if a future stretcher allocated with `malloc`, that control would
//! still pass and this test would be blind to it.
//!
//! x86_64 only: the mangled names differ on 32-bit and ARM64EC targets, and
//! a wrong name would leave the runtime's own operators in place. One test
//! per file, so no other test thread allocates inside the window.

#![cfg(all(windows, target_env = "msvc", target_arch = "x86_64"))]
// The replaced operators are called only by C++, under the contracts of the
// C++ standard's replaceable allocation functions.
#![allow(clippy::missing_safety_doc)]

use std::alloc::{GlobalAlloc, Layout, System};
use std::ffi::c_void;
use std::sync::atomic::{AtomicBool, AtomicUsize, Ordering};
use std::sync::Arc;

use odj_audio::deck::{Beat, Track};
use odj_audio::engine::{Engine, EngineCmd};

static ARMED: AtomicBool = AtomicBool::new(false);
/// `operator new` calls from C++.
static NEWS: AtomicUsize = AtomicUsize::new(0);
/// `operator delete` calls from C++ on a pointer that is not null.
static DELETES: AtomicUsize = AtomicUsize::new(0);
/// Allocations, frees and reallocations from Rust.
static RUST: AtomicUsize = AtomicUsize::new(0);

struct Counting;

unsafe impl GlobalAlloc for Counting {
    unsafe fn alloc(&self, l: Layout) -> *mut u8 {
        count(&RUST);
        System.alloc(l)
    }
    unsafe fn dealloc(&self, p: *mut u8, l: Layout) {
        count(&RUST);
        System.dealloc(p, l)
    }
    unsafe fn realloc(&self, p: *mut u8, l: Layout, n: usize) -> *mut u8 {
        count(&RUST);
        System.realloc(p, l, n)
    }
}

#[global_allocator]
static GLOBAL: Counting = Counting;

fn count(counter: &AtomicUsize) {
    if ARMED.load(Ordering::Relaxed) {
        counter.fetch_add(1, Ordering::Relaxed);
    }
}

extern "C" {
    fn malloc(n: usize) -> *mut c_void;
    fn free(p: *mut c_void);
    fn _aligned_malloc(n: usize, align: usize) -> *mut c_void;
    fn _aligned_free(p: *mut c_void);
}

/// `operator new` must not return null; the standard has it throw
/// `bad_alloc`, which Rust cannot raise, so running out of memory ends the
/// test process instead.
unsafe fn new_plain(n: usize) -> *mut c_void {
    count(&NEWS);
    let p = malloc(n.max(1));
    if p.is_null() {
        std::process::abort();
    }
    p
}

unsafe fn new_aligned(n: usize, align: usize) -> *mut c_void {
    count(&NEWS);
    let p = _aligned_malloc(n.max(1), align);
    if p.is_null() {
        std::process::abort();
    }
    p
}

unsafe fn delete_plain(p: *mut c_void) {
    if !p.is_null() {
        count(&DELETES);
        free(p);
    }
}

unsafe fn delete_aligned(p: *mut c_void) {
    if !p.is_null() {
        count(&DELETES);
        _aligned_free(p);
    }
}

// void* operator new(size_t)
#[export_name = "??2@YAPEAX_K@Z"]
pub unsafe extern "C" fn op_new(n: usize) -> *mut c_void {
    new_plain(n)
}

// void* operator new[](size_t)
#[export_name = "??_U@YAPEAX_K@Z"]
pub unsafe extern "C" fn op_new_array(n: usize) -> *mut c_void {
    new_plain(n)
}

// void* operator new(size_t, const std::nothrow_t&)
#[export_name = "??2@YAPEAX_KAEBUnothrow_t@std@@@Z"]
pub unsafe extern "C" fn op_new_nothrow(n: usize, _tag: *const c_void) -> *mut c_void {
    new_plain(n)
}

// void* operator new[](size_t, const std::nothrow_t&)
#[export_name = "??_U@YAPEAX_KAEBUnothrow_t@std@@@Z"]
pub unsafe extern "C" fn op_new_array_nothrow(n: usize, _tag: *const c_void) -> *mut c_void {
    new_plain(n)
}

// void operator delete(void*)
#[export_name = "??3@YAXPEAX@Z"]
pub unsafe extern "C" fn op_delete(p: *mut c_void) {
    delete_plain(p)
}

// void operator delete(void*, size_t)
#[export_name = "??3@YAXPEAX_K@Z"]
pub unsafe extern "C" fn op_delete_sized(p: *mut c_void, _n: usize) {
    delete_plain(p)
}

// void operator delete[](void*)
#[export_name = "??_V@YAXPEAX@Z"]
pub unsafe extern "C" fn op_delete_array(p: *mut c_void) {
    delete_plain(p)
}

// void operator delete[](void*, size_t)
#[export_name = "??_V@YAXPEAX_K@Z"]
pub unsafe extern "C" fn op_delete_array_sized(p: *mut c_void, _n: usize) {
    delete_plain(p)
}

// void* operator new(size_t, std::align_val_t)
#[export_name = "??2@YAPEAX_KW4align_val_t@std@@@Z"]
pub unsafe extern "C" fn op_new_aligned(n: usize, align: usize) -> *mut c_void {
    new_aligned(n, align)
}

// void* operator new[](size_t, std::align_val_t)
#[export_name = "??_U@YAPEAX_KW4align_val_t@std@@@Z"]
pub unsafe extern "C" fn op_new_array_aligned(n: usize, align: usize) -> *mut c_void {
    new_aligned(n, align)
}

// void operator delete(void*, std::align_val_t)
#[export_name = "??3@YAXPEAXW4align_val_t@std@@@Z"]
pub unsafe extern "C" fn op_delete_aligned(p: *mut c_void, _align: usize) {
    delete_aligned(p)
}

// void operator delete(void*, size_t, std::align_val_t)
#[export_name = "??3@YAXPEAX_KW4align_val_t@std@@@Z"]
pub unsafe extern "C" fn op_delete_sized_aligned(p: *mut c_void, _n: usize, _align: usize) {
    delete_aligned(p)
}

// void operator delete[](void*, std::align_val_t)
#[export_name = "??_V@YAXPEAXW4align_val_t@std@@@Z"]
pub unsafe extern "C" fn op_delete_array_aligned(p: *mut c_void, _align: usize) {
    delete_aligned(p)
}

// void operator delete[](void*, size_t, std::align_val_t)
#[export_name = "??_V@YAXPEAX_KW4align_val_t@std@@@Z"]
pub unsafe extern "C" fn op_delete_array_sized_aligned(p: *mut c_void, _n: usize, _align: usize) {
    delete_aligned(p)
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

fn seen() -> (usize, usize, usize) {
    (NEWS.load(Ordering::SeqCst), DELETES.load(Ordering::SeqCst), RUST.load(Ordering::SeqCst))
}

fn reset() {
    for c in [&NEWS, &DELETES, &RUST] {
        c.store(0, Ordering::SeqCst);
    }
}

#[test]
fn stretching_decks_do_not_allocate_in_c_or_rust() {
    // Positive control first: the Rust counter must see an allocation, or a
    // zero below proves nothing.
    ARMED.store(true, Ordering::SeqCst);
    let v: Vec<u8> = std::hint::black_box(Vec::with_capacity(4096));
    ARMED.store(false, Ordering::SeqCst);
    drop(v);
    assert!(seen().2 >= 1, "the counting global allocator did not fire; the check below would be blind");
    reset();
    // Second control: the replaced operators see C++ allocations (the
    // stretcher's own buffers, allocated through `operator new` when it is
    // built) and their frees (when it is dropped). If the linker had kept
    // the runtime's operators, both counts would be zero here.
    ARMED.store(true, Ordering::SeqCst);
    let s = std::hint::black_box(odj_audio::stretch::Stretcher::new(48000.0, 0));
    drop(s);
    ARMED.store(false, Ordering::SeqCst);
    let (news, deletes, _) = seen();
    assert!(news > 4, "only {news} `operator new` calls seen while building a stretcher; C++ is not reaching the replaced operators");
    assert!(deletes > 4, "only {deletes} `operator delete` calls seen while dropping a stretcher; C++ is not reaching the replaced operators");
    reset();

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
            e.apply(EngineCmd::Seek { deck: 2, position_ms: 5000.0, quantize: true }).unwrap();
            e.apply(EngineCmd::KeyNudge { deck: 2, semitones: -1 }).unwrap();
        }
        e.render(&mut buf);
    }
    ARMED.store(false, Ordering::SeqCst);
    let (news, deletes, rust) = seen();
    assert_eq!(
        (news, deletes, rust),
        (0, 0, 0),
        "inside apply/render with the stretcher running: {news} operator new, {deletes} operator delete, {rust} Rust allocator calls"
    );
    // Control: the decks were actually stretching and audible.
    let d1 = e.snapshot().decks[0];
    assert!(d1.playing && d1.master_tempo);
    assert!(buf.iter().any(|&x| x.abs() > 0.05));
}
