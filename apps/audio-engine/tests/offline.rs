//! Offline render: determinism, exact bar timing, ramps, and the decode path.

mod common;

use odj_audio::offline::{render_plan_files, render_plan_files_with, RenderOptions, Solo};
use odj_audio::plan::parse_plan;
use serde_json::{json, Value};

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
    // Deck 1 starts on crossfader side A, centered: the equal-power 0.707.
    let xf = odj_audio::mixer::xf_gain(odj_audio::mixer::Assign::A, 0.5) as f32;
    // Loud at the start (control), silent once the ramp and its smoothing end.
    assert!(peak(0, 480) > 0.45 * xf, "start peak {}", peak(0, 480));
    assert!(peak(96000, 144000) < 1e-6, "tail peak {}", peak(96000, 144000));
    // Halfway through, the fader is near 0.5.
    let mid = peak(23000, 25000);
    assert!((mid - 0.25 * xf).abs() < 0.02 * xf, "mid peak {mid}");
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
    // An end nothing can reach fails at once, under the default 4 h max_ms,
    // instead of rendering (and holding) silence all the way there.
    let never = json!({"end": {"deck": 1, "bar": 3}, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}]});
    let started = std::time::Instant::now();
    let e = render_plan_files(&parse_plan(&never).unwrap(), &d).err().unwrap();
    assert!(e.message.contains("never be reached"), "{}", e.message);
    assert!(started.elapsed().as_secs() < 10, "took {:?}", started.elapsed());
    // A playing deck looping short of the end is stuck too.
    let looping = json!({"end": {"deck": 1, "position_ms": 1500}, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
        {"at": {"ms": 0}, "cmd": {"type": "loop", "deck": 1, "loop": {"in_ms": 0, "out_ms": 500}}},
        {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}}]});
    let e = render_plan_files(&parse_plan(&looping).unwrap(), &d).err().unwrap();
    assert!(e.message.contains("never be reached"), "{}", e.message);
    // Control: an event still to come keeps the render going (here it starts
    // the deck that reaches the end), so waiting is not mistaken for stuck.
    let later = json!({"end": {"deck": 1, "position_ms": 500}, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
        {"at": {"ms": 300}, "cmd": {"type": "play", "deck": 1, "playing": true}}]});
    let out = render_plan_files(&parse_plan(&later).unwrap(), &d).unwrap();
    assert_eq!(out.frames, 48000 * 800 / 1000);
    // But an event due only after the render budget ends cannot fire in it,
    // so it does not keep a stuck render going to max_ms either (Codex's
    // case: the play at frame u64::MAX).
    for at in [json!({"frame": u64::MAX}), json!({"ms": 2001})] {
        let beyond = json!({"end": {"deck": 1, "position_ms": 1000}, "max_ms": 2000, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
            {"at": at, "cmd": {"type": "play", "deck": 1, "playing": true}}]});
        let e = render_plan_files(&parse_plan(&beyond).unwrap(), &d).err().unwrap();
        assert!(e.message.contains("never be reached: at 0 ms"), "{at}: {}", e.message);
    }
    // Control: an event due exactly at the budget still fires on that frame,
    // and here it moves the deck onto the end.
    let edge = json!({"end": {"deck": 1, "position_ms": 1000}, "max_ms": 500, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
        {"at": {"ms": 500}, "cmd": {"type": "seek", "deck": 1, "position_ms": 1000}}]});
    assert_eq!(render_plan_files(&parse_plan(&edge).unwrap(), &d).unwrap().frames, 24000);
    // And max_ms still bounds an end that is reachable, just too far away.
    let far = json!({"end": {"ms": 2000}, "max_ms": 500, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}]});
    let e = render_plan_files(&parse_plan(&far).unwrap(), &d).err().unwrap();
    assert!(e.message.contains("max_ms"), "{}", e.message);
    // An absolute end past the ceiling is known too far before rendering.
    assert!(e.message.contains("is past the longest render"), "{}", e.message);
    // Control: an end exactly at the ceiling is inside it.
    let at = json!({"end": {"ms": 500}, "max_ms": 500, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}]});
    assert_eq!(render_plan_files(&parse_plan(&at).unwrap(), &d).unwrap().frames, 24000);
    // A reachable end longer than one WAV file holds (4 h at 48 kHz, under
    // the default max_ms) is refused before anything is decoded or held,
    // deck outputs or not.
    // The same for an end given in frames, up to u64::MAX.
    for end in [json!({"ms": 14_400_000}), json!({"frame": u64::MAX}), json!({"frame": 691_200_000u64})] {
        let huge = json!({"end": end, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
            {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}}]});
        for opts in [RenderOptions::default(), with_decks()] {
            let started = std::time::Instant::now();
            let e = render_plan_files_with(&parse_plan(&huge).unwrap(), &d, opts).err().unwrap();
            assert!(e.message.contains("WAV file holds"), "{end}: {}", e.message);
            assert!(started.elapsed().as_secs() < 2, "{end} took {:?}", started.elapsed());
        }
    }
    // Control: a frame end exactly at max_ms is inside it.
    let at_frame = json!({"end": {"frame": 24000}, "max_ms": 500, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}]});
    assert_eq!(render_plan_files(&parse_plan(&at_frame).unwrap(), &d).unwrap().frames, 24000);
    let past_frame = json!({"end": {"frame": 24001}, "max_ms": 500, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}]});
    let e = render_plan_files(&parse_plan(&past_frame).unwrap(), &d).err().unwrap();
    assert!(e.message.contains("is past the longest render"), "{}", e.message);
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

#[test]
fn a_tempo_ramp_records_every_step_it_applies() {
    let d = temp_dir("tempo-ramp");
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 5.0));
    let plan = json!({
        "end": {"ms": 2000},
        "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav", "bpm": 120}},
            {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
            {"at": {"ms": 500}, "ramp": {"type": "tempo", "deck": 1, "to": 1.08, "over": {"ms": 1000}}}
        ]
    });
    let out = render_plan_files(&parse_plan(&plan).unwrap(), &d).unwrap();
    let ramp: Vec<(u64, f64)> = out.tempo.iter().filter(|t| t.frame >= 24000).map(|t| (t.frame, t.tempo)).collect();
    // The ramp runs from frame 24000 to 72000 in 32-frame steps; every step
    // after the first (which sets the starting 1.0) changes the ratio, so each
    // must appear, 32 frames apart, ending on 1.08 at the ramp's end.
    assert_eq!(ramp.len(), 48000 / 32, "{:?}", &ramp[..ramp.len().min(8)]);
    assert!(ramp.windows(2).all(|w| w[1].0 - w[0].0 == 32 && w[1].1 > w[0].1));
    assert_eq!(*ramp.last().unwrap(), (72000, 1.08));
    // Control: a knob ramp on the same plan adds no tempo points.
    let knob = json!({
        "end": {"ms": 2000},
        "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav", "bpm": 120}},
            {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
            {"at": {"ms": 500}, "ramp": {"type": "fader", "deck": 1, "to": 0.5, "over": {"ms": 1000}}}
        ]
    });
    assert_eq!(render_plan_files(&parse_plan(&knob).unwrap(), &d).unwrap().tempo.len(), 1);
}

#[test]
fn loads_of_one_file_share_its_samples() {
    let d = temp_dir("shared-pcm");
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 1.0));
    let mut load = odj_audio::offline::file_loader(d.clone());
    let spec = |deck| odj_audio::protocol::LoadSpec { deck, path: "a.wav".into(), beats: vec![], bpm: None };
    let (a, b) = (load(&spec(1)).unwrap(), load(&spec(2)).unwrap());
    assert!(std::sync::Arc::ptr_eq(&a.pcm, &b.pcm), "a second load copied the samples");
    // Control: a different file gets its own samples.
    write_wav(&d, "b.wav", 48000, &sine(48000, 330.0, 1.0));
    let c = load(&odj_audio::protocol::LoadSpec { deck: 3, path: "b.wav".into(), beats: vec![], bpm: None }).unwrap();
    assert!(!std::sync::Arc::ptr_eq(&a.pcm, &c.pcm));
}

#[test]
fn ramp_endpoints_are_checked_before_rendering() {
    let ramp = |target: Value, to: f64| {
        let mut r = target;
        r["to"] = json!(to);
        r["over"] = json!({"ms": 1000});
        json!({"end": {"ms": 100}, "events": [{"at": {"ms": 0}, "ramp": r}]})
    };
    let fader = json!({"type": "fader", "deck": 1});
    let tempo = json!({"type": "tempo", "deck": 1});
    // A knob ramped out of its range is refused as the plan is read, not when
    // the ramp crosses the limit part-way through a render.
    for to in [2.0, -0.1, 1.0001] {
        let e = parse_plan(&ramp(fader.clone(), to)).unwrap_err();
        assert!(e.message.contains("events[0].ramp.to") && e.message.contains("0..1"), "{to}: {}", e.message);
    }
    for to in [0.0, -1.0, 2.5] {
        let e = parse_plan(&ramp(tempo.clone(), to)).unwrap_err();
        assert!(e.message.contains("tempo ratio within 0..2"), "{to}: {}", e.message);
    }
    // Control: the ends of each range are accepted.
    for (t, to) in [(&fader, 0.0), (&fader, 1.0), (&tempo, 2.0), (&tempo, 0.01)] {
        assert!(parse_plan(&ramp(t.clone(), to)).is_ok(), "{t} to {to}");
    }

    // A tempo beyond the deck's pitch range when the ramp starts fails there,
    // on the ramp's own event, before any of it is applied.
    let d = temp_dir("ramp-range");
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 2.0));
    let plan = |range: Option<u32>, to: f64| {
        let mut events = vec![json!({"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}})];
        if let Some(r) = range {
            events.push(json!({"at": {"ms": 0}, "cmd": {"type": "pitch_range", "deck": 1, "range": r}}));
        }
        events.push(json!({"at": {"ms": 200}, "ramp": {"type": "tempo", "deck": 1, "to": to, "over": {"ms": 500}}}));
        json!({"end": {"ms": 1000}, "events": events})
    };
    let e = render_plan_files(&parse_plan(&plan(None, 1.5)).unwrap(), &d).err().unwrap();
    assert!(e.message.starts_with("events[1]: ramp.to tempo 1.5") && e.message.contains("pitch range"), "{}", e.message);
    // Controls: with a range that fits, the same ramp renders to its end,
    // and a ramp to the very edge of a narrow range is inside it.
    let out = render_plan_files(&parse_plan(&plan(Some(100), 1.5)).unwrap(), &d).unwrap();
    assert_eq!(out.frames, 48000);
    let out = render_plan_files(&parse_plan(&plan(Some(8), 1.08)).unwrap(), &d).unwrap();
    assert_eq!(out.frames, 48000);
    let e = render_plan_files(&parse_plan(&plan(Some(8), 1.09)).unwrap(), &d).err().unwrap();
    assert!(e.message.contains("pitch range"), "{}", e.message);

    // Codex's case: a pitch range narrowed under a running tempo ramp, while
    // the tempo so far still fits, fails on the pitch_range event itself,
    // before the ramp crosses the new edge. The range 16 ramp to 1.12 is at
    // about 1.02 when the range drops to 8 at 300 ms.
    let narrowed = |after: f64, range: u32| {
        json!({"end": {"ms": 1000}, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
            {"at": {"ms": 0}, "cmd": {"type": "pitch_range", "deck": 1, "range": 16}},
            {"at": {"ms": 200}, "ramp": {"type": "tempo", "deck": 1, "to": after, "over": {"ms": 500}}},
            {"at": {"ms": 300}, "cmd": {"type": "pitch_range", "deck": 1, "range": range}}]})
    };
    let e = render_plan_files(&parse_plan(&narrowed(1.12, 8)).unwrap(), &d).err().unwrap();
    assert!(e.message.starts_with("events[3]: pitch_range") && e.message.contains("events[2]"), "{}", e.message);
    // Controls: a narrower range the ramp's target still fits renders to the
    // end, and so does widening it.
    for (to, range) in [(1.08, 8), (1.12, 100)] {
        let out = render_plan_files(&parse_plan(&narrowed(to, range)).unwrap(), &d).unwrap();
        assert_eq!(out.frames, 48000, "to {to} range {range}");
    }
}

#[test]
fn a_ramp_longer_than_any_render_is_refused() {
    let d = temp_dir("ramp-long");
    write_wav(&d, "a.wav", 48000, &sine(48000, 220.0, 2.0));
    let max = odj_audio::wav::MAX_F32_FRAMES;
    let plan = |over: Value| {
        json!({"end": {"ms": 200}, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav", "bpm": 120}},
            {"at": {"ms": 100}, "ramp": {"type": "fader", "deck": 1, "to": 0.0, "over": over}}]})
    };
    // Starting after frame 0, these would overflow the ramp's end frame.
    for over in [json!({"frames": u64::MAX}), json!({"ms": 1e300}), json!({"beats": 1e300, "deck": 1}), json!({"frames": max + 1})] {
        let e = render_plan_files(&parse_plan(&plan(over.clone())).unwrap(), &d).err().unwrap();
        assert!(e.message.starts_with("events[1]") && e.message.contains("longer than any render"), "{over}: {}", e.message);
    }
    // Control: the longest ramp a render could finish is accepted, and the
    // render ends at plan.end with it barely started.
    let out = render_plan_files(&parse_plan(&plan(json!({"frames": max}))).unwrap(), &d).unwrap();
    assert_eq!(out.frames, 9600);
}

#[test]
fn deck_positions_before_the_grid_or_track_are_refused() {
    let plan = |at: Value, end: Value| {
        json!({"end": end, "events": [{"at": at, "cmd": {"type": "play", "deck": 1, "playing": true}}]})
    };
    let ms = json!({"ms": 1000});
    for (key, bad) in [("bar", 0.5), ("bar", -3.0), ("beat", -0.25), ("position_ms", -1.0)] {
        let mut pos = json!({"deck": 1});
        pos[key] = json!(bad);
        // As an event's time and as the plan's end.
        let e = parse_plan(&plan(pos.clone(), ms.clone())).unwrap_err();
        assert!(e.message.contains(&format!("events[0].at.{key} must be at least")), "{key} {bad}: {}", e.message);
        let e = parse_plan(&plan(json!({"ms": 0}), pos)).unwrap_err();
        assert!(e.message.contains(&format!("plan.end.{key} must be at least")), "{key} {bad}: {}", e.message);
    }
    // Control: the first bar, the first beat and the track's start are fine.
    for (key, ok) in [("bar", 1.0), ("beat", 0.0), ("position_ms", 0.0)] {
        let mut pos = json!({"deck": 1});
        pos[key] = json!(ok);
        assert!(parse_plan(&plan(pos.clone(), ms.clone())).is_ok(), "{key} {ok}");
        assert!(parse_plan(&plan(json!({"ms": 0}), pos)).is_ok(), "{key} {ok}");
    }
}

#[test]
fn a_time_or_length_naming_two_places_is_refused() {
    let with = |at: Value, over: Value| {
        parse_plan(&json!({"end": {"ms": 1000}, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
            {"at": at, "ramp": {"type": "crossfader", "to": 1.0, "over": over}}]}))
    };
    let ok_over = json!({"ms": 100});
    let ok_at = json!({"ms": 10});
    // Codex's case: bar and beat together used to render at the bar and drop
    // the beat. Every pair of time or length fields is refused the same way.
    for at in [
        json!({"deck": 1, "bar": 2, "beat": 100}),
        json!({"deck": 1, "beat": 4, "position_ms": 900}),
        json!({"deck": 1, "bar": 2, "ms": 5}),
        json!({"frame": 480, "ms": 10}),
    ] {
        let e = with(at.clone(), ok_over.clone()).unwrap_err();
        assert!(e.message.contains("give exactly one"), "{at}: {}", e.message);
    }
    for over in [json!({"ms": 100, "frames": 4800}), json!({"deck": 1, "beats": 4, "bars": 1})] {
        let e = with(ok_at.clone(), over.clone()).unwrap_err();
        assert!(e.message.contains("give exactly one"), "{over}: {}", e.message);
    }
    let e = parse_plan(&json!({"end": {"deck": 1, "bar": 3, "beat": 1}, "events": []})).unwrap_err();
    assert!(e.message.starts_with("plan.end"), "{}", e.message);
    // Controls: each field alone still parses.
    for at in [json!({"deck": 1, "bar": 2}), json!({"deck": 1, "beat": 4}), json!({"deck": 1, "position_ms": 900}), json!({"frame": 480}), json!({"ms": 10})] {
        assert!(with(at.clone(), ok_over.clone()).is_ok(), "{at}");
    }
    for over in [json!({"ms": 100}), json!({"frames": 4800}), json!({"deck": 1, "beats": 4}), json!({"deck": 1, "bars": 1})] {
        assert!(with(ok_at.clone(), over.clone()).is_ok(), "{over}");
    }
}

#[test]
fn a_direct_knob_command_ends_a_ramp_on_that_knob() {
    let d = temp_dir("ramp-yield");
    write_wav(&d, "a.wav", 48000, &sine(48000, 1000.0, 10.0));
    let render = |events: Vec<Value>| {
        let mut all = vec![
            json!({"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}),
            json!({"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}}),
        ];
        all.extend(events);
        render_plan_files(&parse_plan(&json!({"end": {"ms": 2000}, "events": all})).unwrap(), &d).unwrap()
    };
    let xf = odj_audio::mixer::xf_gain(odj_audio::mixer::Assign::A, 0.5) as f32;
    let peak = |out: &odj_audio::offline::RenderOutput, a: usize, b: usize| {
        out.pcm[a * 2..b * 2].iter().fold(0.0f32, |m, &x| m.max(x.abs())) / (0.5 * xf)
    };
    let ramp = json!({"at": {"ms": 0}, "ramp": {"type": "fader", "deck": 1, "to": 0.0, "over": {"ms": 1000}}});
    let direct = |ms: u64| json!({"at": {"ms": ms}, "cmd": {"type": "fader", "deck": 1, "value": 0.9}});
    // Codex's case: the fader set to 0.9 halfway through a ramp to 0 stays
    // at 0.9; the ramp no longer carries it on down.
    let out = render(vec![ramp.clone(), direct(500)]);
    assert!((peak(&out, 33600, 36000) - 0.9).abs() < 0.02, "after the command {}", peak(&out, 33600, 36000));
    assert!((peak(&out, 72000, 96000) - 0.9).abs() < 0.02, "long after {}", peak(&out, 72000, 96000));
    // On the same frame, plan order decides: the command after the ramp wins.
    let out = render(vec![ramp.clone(), direct(0)]);
    assert!((peak(&out, 72000, 96000) - 0.9).abs() < 0.02, "{}", peak(&out, 72000, 96000));
    // Controls: a ramp after the command still runs to its end, and a
    // command on another knob leaves the ramp alone.
    let out = render(vec![direct(0), ramp.clone()]);
    assert!(peak(&out, 72000, 96000) < 1e-6, "{}", peak(&out, 72000, 96000));
    let other = json!({"at": {"ms": 500}, "cmd": {"type": "trim", "deck": 1, "value": 0.25}});
    let out = render(vec![ramp.clone(), other]);
    assert!(peak(&out, 72000, 96000) < 1e-6, "{}", peak(&out, 72000, 96000));
    // A load resets the deck's tempo to 1, as on the page, so it ends a tempo
    // ramp on that deck too: the tempo stays 1 after it.
    let out = render(vec![
        json!({"at": {"ms": 0}, "ramp": {"type": "tempo", "deck": 1, "to": 1.08, "over": {"ms": 1000}}}),
        json!({"at": {"ms": 500}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}),
    ]);
    let last = out.tempo.iter().rfind(|t| t.deck == 1).unwrap();
    assert_eq!((last.frame, last.tempo), (24000, 1.0));
}
