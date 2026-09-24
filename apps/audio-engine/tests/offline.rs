//! Offline render: determinism, exact bar timing, ramps, and the decode path.

mod common;

use odj_audio::offline::render_plan_files;
use odj_audio::plan::parse_plan;
use serde_json::json;

use common::*;

fn two_deck_plan(block: usize) -> serde_json::Value {
    json!({
        "sample_rate": 48000,
        "block_frames": block,
        "end": {"deck": 1, "bar": 9},
        "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav", "beatgrid": grid_120_json(80)}},
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 2, "path": "b.wav", "beatgrid": grid_120_json(80)}},
            {"at": {"ms": 0}, "cmd": {"type": "assign", "deck": 1, "assign": "A"}},
            {"at": {"ms": 0}, "cmd": {"type": "assign", "deck": 2, "assign": "B"}},
            {"at": {"ms": 0}, "cmd": {"type": "crossfader", "value": 0.0}},
            {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
            {"at": {"deck": 1, "bar": 3}, "cmd": {"type": "play", "deck": 2, "playing": true}},
            {"at": {"deck": 1, "bar": 3}, "ramp": {"type": "crossfader", "to": 1.0, "over": {"bars": 4, "deck": 1}}},
            {"at": {"deck": 1, "bar": 5}, "ramp": {"type": "eq", "deck": 1, "band": "low", "to": 0.0, "over": {"beats": 4, "deck": 1}}},
            {"at": {"deck": 1, "beat": 20}, "cmd": {"type": "tempo", "deck": 2, "ratio": 1.04}}
        ]
    })
}

fn fixture_dir() -> std::path::PathBuf {
    let d = temp_dir("offline");
    write_wav(&d, "a.wav", 44100, &sine(44100, 220.0, 40.0));
    write_wav(&d, "b.wav", 48000, &sine(48000, 330.0, 40.0));
    d
}

#[test]
fn same_plan_same_bytes_and_block_size_does_not_matter() {
    let d = fixture_dir();
    let a = render_plan_files(&parse_plan(&two_deck_plan(256)).unwrap(), &d).unwrap();
    let b = render_plan_files(&parse_plan(&two_deck_plan(256)).unwrap(), &d).unwrap();
    assert_eq!(a.sha256, b.sha256, "two renders of one plan differ");
    let c = render_plan_files(&parse_plan(&two_deck_plan(61)).unwrap(), &d).unwrap();
    assert_eq!(a.frames, c.frames);
    assert_eq!(a.fired, c.fired, "event timing depends on block size");
    assert_eq!(a.sha256, c.sha256, "audio depends on block size");
    // Control: a plan that differs by one knob must NOT hash the same, or the
    // hash comparison above proves nothing.
    let mut p = two_deck_plan(256);
    p["events"][4]["cmd"]["value"] = json!(0.1);
    let e = render_plan_files(&parse_plan(&p).unwrap(), &d).unwrap();
    assert_ne!(a.sha256, e.sha256);
}

#[test]
fn bar_events_fire_on_the_exact_frame() {
    let d = fixture_dir();
    let out = render_plan_files(&parse_plan(&two_deck_plan(256)).unwrap(), &d).unwrap();
    // Deck 1 is a 44.1 kHz file at tempo 1 rendered at 48 kHz: bar 3 is 4000 ms
    // of track, so 192000 output frames; bar 9 (the end) is 16000 ms.
    let at = |i: usize| out.fired.iter().find(|f| f.event == i).map(|f| f.frame);
    assert_eq!(at(6), Some(192000));
    assert_eq!(at(7), Some(192000));
    assert_eq!(at(8), Some(384000));
    assert_eq!(at(9), Some(480000));
    assert_eq!(out.frames, 768000);
}

#[test]
fn bar_events_follow_tempo() {
    let d = temp_dir("tempo");
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 30.0));
    let plan = json!({
        "end": {"deck": 1, "bar": 6},
        "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav", "beatgrid": grid_120_json(60)}},
            {"at": {"ms": 0}, "cmd": {"type": "tempo", "deck": 1, "ratio": 1.05}},
            {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
            {"at": {"deck": 1, "bar": 5}, "cmd": {"type": "master_mute", "muted": false}}
        ]
    });
    let out = render_plan_files(&parse_plan(&plan).unwrap(), &d).unwrap();
    // Bar 5 is 8000 ms of track = 384000 source frames at step 1.05.
    let want = (384000.0f64 / 1.05).ceil() as u64;
    assert_eq!(out.fired.iter().find(|f| f.event == 3).unwrap().frame, want);
}

#[test]
fn a_ramp_lands_on_its_target() {
    let d = temp_dir("ramp");
    write_wav(&d, "a.wav", 48000, &sine(48000, 1000.0, 10.0));
    let plan = json!({
        "end": {"ms": 3000},
        "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
            {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
            {"at": {"ms": 0}, "ramp": {"type": "fader", "deck": 1, "to": 0.0, "over": {"ms": 1000}}}
        ]
    });
    let out = render_plan_files(&parse_plan(&plan).unwrap(), &d).unwrap();
    let peak = |a: usize, b: usize| out.pcm[a * 2..b * 2].iter().fold(0.0f32, |m, &x| m.max(x.abs()));
    // Loud at the start (control), silent once the ramp and its smoothing end.
    assert!(peak(0, 480) > 0.45, "start peak {}", peak(0, 480));
    assert!(peak(96000, 144000) < 1e-6, "tail peak {}", peak(96000, 144000));
    // Halfway through, the fader is near 0.5.
    let mid = peak(23000, 25000);
    assert!((mid - 0.25).abs() < 0.02, "mid peak {mid}");
}

#[test]
fn render_is_faster_than_real_time() {
    let d = fixture_dir();
    let out = render_plan_files(&parse_plan(&two_deck_plan(256)).unwrap(), &d).unwrap();
    // A floor, not the benchmark: debug builds of the test profile still run
    // opt-level 2. The measured figure goes in 20-VERIFICATION.md.
    assert!(out.realtime_factor() > 5.0, "realtime factor {}", out.realtime_factor());
}

#[test]
fn plan_errors_name_the_event() {
    let d = temp_dir("err");
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 2.0));
    let plan = json!({
        "end": {"ms": 1000},
        "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
            {"at": {"ms": 10}, "cmd": {"type": "beat_jump", "deck": 1, "beats": 4}}
        ]
    });
    let e = render_plan_files(&parse_plan(&plan).unwrap(), &d).err().unwrap();
    assert!(e.message.starts_with("events[1]"), "{}", e.message);
    let missing = json!({"end": {"ms": 10}, "events": [{"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "nope.wav"}}]});
    let e = render_plan_files(&parse_plan(&missing).unwrap(), &d).err().unwrap();
    assert!(e.message.contains("nope.wav"), "{}", e.message);
    let never = json!({"end": {"deck": 1, "bar": 3}, "max_ms": 500, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}]});
    let e = render_plan_files(&parse_plan(&never).unwrap(), &d).err().unwrap();
    assert!(e.message.contains("max_ms"), "{}", e.message);
}
