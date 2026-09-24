//! Offline render: determinism, exact bar timing, ramps, and the decode path.

mod common;

use odj_audio::offline::{render_plan_files, render_plan_files_with, RenderOptions, Solo};
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

// What a transition scorer reads from a render (plan 20-08): each deck's own
// audio, which decks are heard when, and each deck's tempo.

fn with_decks() -> RenderOptions {
    RenderOptions { deck_outputs: true }
}

#[test]
fn deck_outputs_sum_to_the_mix_and_leave_it_unchanged() {
    let d = fixture_dir();
    let plan = parse_plan(&two_deck_plan(256)).unwrap();
    let plain = render_plan_files(&plan, &d).unwrap();
    let split = render_plan_files_with(&plan, &d, with_decks()).unwrap();
    assert_eq!(plain.sha256, split.sha256, "asking for deck outputs changed the mix");
    assert!(plain.decks.is_empty());
    assert_eq!(split.decks.iter().map(|o| o.deck).collect::<Vec<_>>(), vec![1, 2]);
    let (d1, d2) = (&split.decks[0].pcm, &split.decks[1].pcm);
    assert_eq!(d1.len(), split.pcm.len());
    assert_eq!(d2.len(), split.pcm.len());
    // Master gain is 1, so the mix is exactly the decks summed in deck order.
    for (j, m) in split.pcm.iter().enumerate() {
        assert_eq!(*m, (0.0 + d1[j]) + d2[j], "sample {j}");
    }
    // Control: the split is real, not the mix copied twice. Deck 2 is silent
    // before it starts (bar 3 of deck 1, frame 192000) and deck 1 after the
    // crossfader reaches B.
    let peak = |p: &[f32], a: usize, b: usize| p[a * 2..b * 2].iter().fold(0.0f32, |m, &x| m.max(x.abs()));
    assert_eq!(peak(d2, 0, 192000), 0.0);
    assert!(peak(d1, 0, 192000) > 0.4);
    assert!(peak(d2, 700000, 768000) > 0.4);
    assert!(peak(d1, 700000, 768000) < 1e-6);
    assert_ne!(split.decks[0].sha256, split.decks[1].sha256);
}

#[test]
fn timeline_finds_the_overlap_and_the_solo_either_side() {
    let d = fixture_dir();
    let out = render_plan_files(&parse_plan(&two_deck_plan(256)).unwrap(), &d).unwrap();
    let decks: Vec<u8> = out.timeline.iter().map(|s| s.decks).collect();
    assert_eq!(decks, vec![0b01, 0b11, 0b10], "{:?}", out.timeline);
    let (a, both, b) = (&out.timeline[0], &out.timeline[1], &out.timeline[2]);
    assert_eq!((a.start, a.end, both.end, b.end), (0, both.start, b.start, 768000));
    // Deck 2 starts at frame 192000 with the crossfader fully on A, so it is
    // playing but not heard: the overlap starts only once the ramp lifts it
    // above -60 dB, on the next 10 ms step.
    assert!(both.start > 192000 && both.start <= 192000 + 4 * 480, "{}", both.start);
    assert_eq!(both.start % 480, 0);
    // Deck 1 drops out near the end of the 4-bar crossfade (frame 576000),
    // and its low-EQ kill at bar 5 does not count as leaving the mix.
    assert!((574000..=578000).contains(&both.end), "{}", both.end);
    assert_eq!(out.overlaps.len(), 1);
    let o = &out.overlaps[0];
    assert_eq!((o.start, o.end, o.decks), (both.start, both.end, 0b11));
    assert_eq!(o.solo_before, Some(Solo { deck: 1, frames: both.start }));
    assert_eq!(o.solo_after, Some(Solo { deck: 2, frames: 768000 - both.end }));
    // Same timeline whatever the block size.
    let small = render_plan_files(&parse_plan(&two_deck_plan(61)).unwrap(), &d).unwrap();
    assert_eq!(out.timeline, small.timeline);
}

#[test]
fn timeline_edges_land_on_command_frames() {
    let d = temp_dir("edges");
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 5.0));
    let plan = json!({
        "end": {"frame": 60000},
        "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav", "bpm": 128}},
            {"at": {"frame": 1000}, "cmd": {"type": "play", "deck": 1, "playing": true}},
            {"at": {"frame": 50000}, "cmd": {"type": "play", "deck": 1, "playing": false}}
        ]
    });
    let out = render_plan_files(&parse_plan(&plan).unwrap(), &d).unwrap();
    let spans: Vec<(u64, u64, u8)> = out.timeline.iter().map(|s| (s.start, s.end, s.decks)).collect();
    // Neither 1000 nor 50000 is on the 10 ms grid: edges come from the commands.
    assert_eq!(spans, vec![(0, 1000, 0), (1000, 50000, 1), (50000, 60000, 0)]);
    assert!(out.overlaps.is_empty());
    assert_eq!(out.tempo.len(), 1);
    assert_eq!(out.tempo[0].bpm, Some(128.0), "tag BPM when there is no grid");
}

#[test]
fn tempo_points_follow_tempo_commands() {
    let d = fixture_dir();
    let out = render_plan_files(&parse_plan(&two_deck_plan(256)).unwrap(), &d).unwrap();
    let pts: Vec<(u8, u64, f64, Option<f64>)> = out.tempo.iter().map(|t| (t.deck, t.frame, t.tempo, t.bpm)).collect();
    // Both decks load at frame 0 on a 120 BPM grid; deck 2 goes to 1.04 at
    // beat 20 of deck 1 (frame 480000). Knob ramps add no points.
    let bpm = 120.0 * 1.04;
    assert_eq!(pts, vec![(1, 0, 1.0, Some(120.0)), (2, 0, 1.0, Some(120.0)), (2, 480000, 1.04, Some(bpm))]);
}
