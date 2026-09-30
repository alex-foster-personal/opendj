//! MIDI into the engine (plan 20-03) as a supervisor sees it: recorded
//! controller bytes fed through `midi_inject` on the fake clock, no hardware.

mod common;

use std::io::{BufRead, BufReader, Lines, Write};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};

use serde_json::{json, Value};

use common::*;

const BIN: &str = env!("CARGO_BIN_EXE_odj-audio");

struct Session {
    child: Child,
    stdin: ChildStdin,
    lines: Lines<BufReader<ChildStdout>>,
}

impl Session {
    fn start(dir: &std::path::Path, extra: &[&str]) -> Session {
        let mut child = Command::new(BIN)
            .args(["serve", "--clock", "fake"])
            .args(extra)
            .current_dir(dir)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let stdin = child.stdin.take().unwrap();
        let lines = BufReader::new(child.stdout.take().unwrap()).lines();
        let mut s = Session { child, stdin, lines };
        assert_eq!(s.next()["type"], "hello");
        s
    }

    fn next(&mut self) -> Value {
        serde_json::from_str(&self.lines.next().unwrap().unwrap()).unwrap()
    }

    /// Send one line and read until its own result, returning every line
    /// before it (what a midi_inject produced) and the result itself.
    fn say(&mut self, id: &str, cmd: Value) -> (Vec<Value>, Value) {
        writeln!(self.stdin, "{}", json!({"id": id, "cmd": cmd})).unwrap();
        let mut before = Vec::new();
        loop {
            let v = self.next();
            if v["type"] == "result" && v["id"] == id {
                return (before, v);
            }
            before.push(v);
        }
    }

    fn state(&mut self) -> Value {
        let (_, r) = self.say("st", json!({"type": "engine_state"}));
        assert_eq!(r["ok"], true);
        self.next()
    }

    fn stop(mut self) {
        let (_, r) = self.say("q", json!({"type": "engine_shutdown"}));
        assert_eq!(r["ok"], true);
        assert!(self.child.wait().unwrap().success());
    }
}

fn two_decks(tag: &str) -> TestDir {
    let d = temp_dir(tag);
    write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 20.0));
    write_wav(&d, "b.wav", 48000, &sine(48000, 660.0, 20.0));
    d
}

#[test]
fn a_recorded_flx4_transition_drives_the_engine() {
    let fixture: Value =
        serde_json::from_str(include_str!("fixtures/midi/flx4-transition.json")).unwrap();
    let port = fixture["port"].as_str().unwrap();
    let d = two_decks("midi-transition");
    let mut s = Session::start(&d, &[]);
    for (id, cmd) in [
        ("l1", json!({"type": "load", "deck": 1, "path": "a.wav", "bpm": 120})),
        ("l2", json!({"type": "load", "deck": 2, "path": "b.wav", "bpm": 120})),
        ("p1", json!({"type": "play", "deck": 1, "playing": true})),
    ] {
        assert_eq!(s.say(id, cmd).1["ok"], true, "{id}");
    }
    let mut midi_results = Vec::new();
    for (i, step) in fixture["steps"].as_array().unwrap().iter().enumerate() {
        let (lines, r) = s.say(&format!("m{i}"), json!({"type": "midi_inject", "port": port, "bytes": step["bytes"]}));
        assert_eq!(r["ok"], true, "{}", step["what"]);
        for l in lines {
            assert_eq!(l["type"], "result", "{}: every step is an engine control, got {l}", step["what"]);
            assert_eq!(l["ok"], true, "{}: {l}", step["what"]);
            midi_results.push(l["id"].as_str().unwrap().to_string());
        }
        s.say(&format!("a{i}"), json!({"type": "engine_advance", "ms": 250}));
        s.next();
    }
    // One engine command per control message, in order, releases excluded:
    // tempo 1, eq 1, play 1, fader 1, crossfader 3, eq 2, filter 1, cue 1.
    let want: Vec<String> = (0..11).map(|n| format!("midi-{n}")).collect();
    assert_eq!(midi_results, want);
    let st = s.state();
    let (d1, d2) = (&st["decks"][0], &st["decks"][1]);
    assert_eq!(d1["playing"], false, "CUE while playing pauses deck 1");
    assert_eq!(d1["position_ms"], 0.0, "and returns it to its cue point, the start");
    assert_eq!(d2["playing"], true, "PLAY started deck 2 and the release did not stop it");
    assert_eq!(st["mixer"]["crossfader"], 1.0);
    assert_eq!(d2["fader"], 1.0);
    assert_eq!(d1["eq"]["low"], 0.0);
    assert_eq!(d2["eq"]["low"], 64.0 / 127.0);
    assert_eq!(d1["filter"], 55.0 / 127.0);
    // The 14-bit center (8192 of 16383) is a hair over the middle, as on the page.
    let t = d2["tempo"].as_f64().unwrap();
    assert_eq!(t, 1.0 + (2.0 * 8192.0 / 16383.0 - 1.0) * 0.16);
    s.stop();
}

#[test]
fn page_actions_and_refusals_come_back_as_lines() {
    let d = two_decks("midi-page");
    let mut s = Session::start(&d, &[]);
    let port = "DDJ-FLX4";
    let inject = |s: &mut Session, id: &str, bytes: Value| s.say(id, json!({"type": "midi_inject", "port": port, "bytes": bytes}));

    // Hot cue pad D on deck 2 (ch 10, note 3): the page's action, verbatim.
    let (lines, r) = inject(&mut s, "h", json!([0x99, 0x03, 0x7f]));
    assert_eq!(r["ok"], true);
    assert_eq!(
        lines,
        vec![json!({"type": "midi_action", "port": port, "action": {"type": "deck_hot_cue", "deck": 2, "slot": "D"},
                    "value": {"kind": "button", "pressed": true, "velocity": 127}})]
    );
    // PLAY on an empty deck: the engine refuses it by the MIDI command's id.
    let (lines, _) = inject(&mut s, "p", json!([0x90, 0x0b, 0x7f]));
    assert_eq!(lines.len(), 1);
    assert_eq!(lines[0]["id"], "midi-0");
    assert_eq!(lines[0]["error"]["code"], "no_track");
    // A quarter-beat loop pad (FLX4 BEAT LOOP pad 1) is refused, not rounded:
    // the engine takes whole beats, as the page's command parser does.
    // A real grid (120 BPM): a tag BPM alone is refused for beat loops.
    s.say("l", json!({"type": "load", "deck": 1, "path": "a.wav", "beatgrid_ms": [0, 500, 1000, 1500, 2000]}));
    let (lines, _) = inject(&mut s, "b", json!([0x97, 0x60, 0x7f]));
    assert_eq!(lines[0]["id"], "midi-1");
    assert_eq!(lines[0]["error"]["code"], "invalid", "{}", lines[0]);
    // Control: the 4-beat pad engages.
    let (lines, _) = inject(&mut s, "b4", json!([0x97, 0x64, 0x7f]));
    assert_eq!(lines[0]["ok"], true, "{}", lines[0]);
    assert_eq!(s.state()["decks"][0]["loop"]["beat_length"], 4.0);
    // Unbound traffic is reported with the map's hint.
    let (lines, _) = inject(&mut s, "j", json!([0x90, 0x58, 0x7f]));
    assert_eq!(lines[0]["type"], "midi_unmapped");
    assert_eq!(lines[0]["hint"], "BEAT SYNC (deck 1)");
    // Bad bytes are refused as a whole, before any are routed.
    let (lines, r) = inject(&mut s, "x", json!([0xb0, 300]));
    assert!(lines.is_empty());
    assert_eq!(r["error"]["code"], "invalid");
    s.stop();
}

#[test]
fn an_onboarded_map_wins_over_the_builtin_one() {
    let d = two_decks("midi-map");
    let map = json!({"vendor": "Test", "nameMatch": "DDJ-FLX4", "bindings": [
        {"source": {"ch": 1, "kind": "cc", "id": 19}, "invert": true,
         "action": {"type": "mixer_channel", "deck": 1, "target": "fader"}}
    ]});
    std::fs::write(d.join("maps.json"), map.to_string()).unwrap();
    let mut s = Session::start(&d, &["--midi-map", "maps.json"]);
    let (lines, _) = s.say("f", json!({"type": "midi_inject", "port": "DDJ-FLX4", "bytes": [0xb0, 0x13, 0x7f]}));
    assert_eq!(lines[0]["ok"], true);
    assert_eq!(s.state()["decks"][0]["fader"], 0.0, "the onboarded map inverts the fader");
    // Control: a control only the builtin map binds is not reachable through
    // this port any more, because the whole map is shadowed, as on the page.
    let (lines, _) = s.say("x", json!({"type": "midi_inject", "port": "DDJ-FLX4", "bytes": [0xb6, 0x1f, 0x7f]}));
    assert_eq!(lines[0]["type"], "midi_unmapped");
    s.stop();
}

#[test]
fn bad_midi_flags_fail_before_serving() {
    let d = temp_dir("midi-flags");
    std::fs::write(d.join("bad.json"), r#"{"vendor": "X", "nameMatch": "X", "bindings": [{"source": {"ch": 1, "kind": "note", "id": 1}, "action": {"type": "deck_scratch", "deck": 1}}]}"#).unwrap();
    for (args, want) in [
        (vec!["--clock", "fake", "--midi"], "midi_inject"),
        (vec!["--clock", "fake", "--midi-map", "bad.json"], "unknown action type"),
        (vec!["--clock", "fake", "--midi-map", "missing.json"], "cannot read"),
    ] {
        let o = Command::new(BIN).arg("serve").args(&args).current_dir(&d).stdin(Stdio::null()).output().unwrap();
        assert!(!o.status.success(), "{args:?}");
        assert!(o.stdout.is_empty(), "{args:?} printed hello before failing");
        let err = String::from_utf8_lossy(&o.stderr);
        assert!(err.contains(want), "{args:?}: {err}");
    }
}

/// Without the `midi` feature, `--midi` names the feature to rebuild with.
#[cfg(not(feature = "midi"))]
#[test]
fn midi_ports_need_the_midi_feature() {
    let o = Command::new(BIN).args(["serve", "--clock", "wall", "--midi"]).stdin(Stdio::null()).output().unwrap();
    assert!(!o.status.success());
    assert!(String::from_utf8_lossy(&o.stderr).contains("--features midi"));
}
