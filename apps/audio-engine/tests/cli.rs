//! The binary as a supervisor sees it: render to a WAV, and a fake-clock
//! serve session over pipes.

mod common;

use std::io::{BufRead, BufReader, Write};
use std::process::{Command, Stdio};

use serde_json::{json, Value};

use common::*;

const BIN: &str = env!("CARGO_BIN_EXE_odj-audio");

#[test]
fn render_writes_a_wav_and_a_summary() {
    let d = temp_dir("cli-render");
    write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 5.0));
    let plan = json!({"end": {"ms": 2000}, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
        {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}}
    ]});
    std::fs::write(d.join("plan.json"), plan.to_string()).unwrap();
    let out_wav = d.join("out.wav");
    let o = Command::new(BIN)
        .args(["render", "--plan"])
        .arg(d.join("plan.json"))
        .arg("--out")
        .arg(&out_wav)
        .output()
        .unwrap();
    assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
    let s: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!(s["frames"], 96000);
    assert!(s["realtime_factor"].as_f64().unwrap() > 1.0);
    let bytes = std::fs::read(&out_wav).unwrap();
    assert_eq!(&bytes[0..4], b"RIFF");
    assert_eq!(bytes.len(), 44 + 96000 * 2 * 4);
    // The written file decodes back to the same frame count.
    let back = odj_audio::decode::decode_file(&out_wav).unwrap();
    assert_eq!(back.pcm.len(), 96000 * 2);
}

#[test]
fn render_reports_what_a_scorer_needs() {
    let d = temp_dir("cli-scorer");
    write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 5.0));
    write_wav(&d, "b.wav", 48000, &sine(48000, 660.0, 5.0));
    let plan = json!({"end": {"ms": 3000}, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav", "bpm": 120}},
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 2, "path": "b.wav", "bpm": 124}},
        {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
        {"at": {"ms": 1000}, "cmd": {"type": "play", "deck": 2, "playing": true}},
        {"at": {"ms": 2000}, "cmd": {"type": "play", "deck": 1, "playing": false}}
    ]});
    let text = plan.to_string();
    std::fs::write(d.join("plan.json"), &text).unwrap();
    let o = Command::new(BIN)
        .args(["render", "--plan"])
        .arg(d.join("plan.json"))
        .arg("--out")
        .arg(d.join("mix.wav"))
        .arg("--decks-out")
        .arg(d.join("decks"))
        .output()
        .unwrap();
    assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
    let s: Value = serde_json::from_slice(&o.stdout).unwrap();
    // The plan comes back with the render, verbatim and hashed.
    assert_eq!(s["plan"], plan);
    assert_eq!(s["plan_sha256"].as_str().unwrap().len(), 64);
    assert_eq!(s["master_tempo"], false);
    let spans: Vec<Value> = s["timeline"].as_array().unwrap().iter().map(|t| json!([t["start"], t["end"], t["decks"]])).collect();
    assert_eq!(spans, vec![json!([0, 48000, [1]]), json!([48000, 96000, [1, 2]]), json!([96000, 144000, [2]])]);
    let ov = &s["overlaps"][0];
    assert_eq!(ov["decks"], json!([1, 2]));
    assert_eq!(ov["ms"], 1000.0);
    assert_eq!(ov["solo_before"], json!({"deck": 1, "frames": 48000, "ms": 1000.0}));
    assert_eq!(ov["solo_after"], json!({"deck": 2, "frames": 48000, "ms": 1000.0}));
    assert_eq!(s["tempo"][1], json!({"deck": 2, "frame": 0, "tempo": 1.0, "bpm": 124.0}));
    // One WAV per loaded deck, each the length of the mix.
    let files = s["deck_outputs"].as_array().unwrap();
    assert_eq!(files.len(), 2);
    for (i, f) in files.iter().enumerate() {
        assert_eq!(f["deck"], i + 1);
        let back = odj_audio::decode::decode_file(std::path::Path::new(f["out"].as_str().unwrap())).unwrap();
        assert_eq!(back.pcm.len(), 144000 * 2);
    }
}

#[test]
fn render_fails_loudly_on_a_bad_plan() {
    let d = temp_dir("cli-bad");
    std::fs::write(d.join("plan.json"), r#"{"end": {"ms": 10}, "events": [{"at": {"ms": 0}, "cmd": {"type": "eq", "deck": 1, "band": "low", "value": 3}}]}"#).unwrap();
    let o = Command::new(BIN).args(["render", "--plan"]).arg(d.join("plan.json")).args(["--out", "/dev/null"]).output().unwrap();
    assert!(!o.status.success());
    assert!(String::from_utf8_lossy(&o.stderr).contains("eq.value"));
}

#[test]
fn fake_clock_session_over_pipes() {
    let d = temp_dir("cli-serve");
    write_wav(&d, "a.wav", 44100, &sine(44100, 440.0, 5.0));
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "fake"])
        .current_dir(&d)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut next = || -> Value { serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap() };

    let hello = next();
    assert_eq!(hello["type"], "hello");
    assert_eq!(hello["protocol"], 1);
    assert_eq!(hello["clock"], "fake");
    // The packager reads this to prove it shipped a device-output build.
    assert_eq!(hello["device"], cfg!(feature = "device"));
    // The page grays out what the engine lists, so the list must name the
    // refused commands and never a built one.
    let not_built: Vec<&str> =
        hello["not_built"].as_array().unwrap().iter().map(|v| v.as_str().unwrap()).collect();
    assert!(not_built.contains(&"stem_mute"), "{not_built:?}");
    for built in ["play", "seek", "tempo", "loop", "fader"] {
        assert!(!not_built.contains(&built), "{built} is built: {not_built:?}");
    }

    let mut say = |v: Value| writeln!(stdin, "{v}").unwrap();
    say(json!({"id": "l", "cmd": {"type": "load", "deck": 1, "path": "a.wav", "bpm": 120}}));
    assert_eq!(next(), json!({"type": "result", "id": "l", "ok": true}));
    say(json!({"id": "t", "cmd": {"type": "tempo", "deck": 1, "ratio": 1.1}}));
    assert_eq!(next()["ok"], true);
    say(json!({"id": "p", "cmd": {"type": "play", "deck": 1, "playing": true}}));
    assert_eq!(next()["ok"], true);
    say(json!({"id": "a", "cmd": {"type": "engine_advance", "ms": 1000}}));
    assert_eq!(next()["ok"], true);
    let st = next();
    assert_eq!(st["type"], "state");
    assert_eq!(st["frame"], 48000);
    assert_eq!(st["host_time_ns"], Value::Null);
    let d1 = &st["decks"][0];
    assert_eq!(d1["playing"], true);
    assert!((d1["position_ms"].as_f64().unwrap() - 1100.0).abs() < 1e-6, "{d1}");
    assert!((d1["rate"].as_f64().unwrap() - 1.1).abs() < 1e-12);

    say(json!({"id": "s", "cmd": {"type": "stem_mute", "deck": 1, "stem": "vocals", "muted": true}}));
    let r = next();
    assert_eq!(r["ok"], false);
    assert_eq!(r["error"]["code"], "not_implemented");
    say(json!({"id": "q", "cmd": {"type": "engine_shutdown"}}));
    assert_eq!(next()["ok"], true);
    assert!(child.wait().unwrap().success());
}

#[test]
fn wall_clock_refuses_advance_and_reports_state() {
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "wall"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let hello: Value = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
    assert_eq!(hello["clock"], "wall");
    writeln!(stdin, "{}", json!({"id": "a", "cmd": {"type": "engine_advance", "ms": 10}})).unwrap();
    writeln!(stdin, "{}", json!({"id": "x", "cmd": {"type": "crossfader", "value": 0.25}})).unwrap();
    let mut saw_wrong_clock = false;
    let mut saw_ok = false;
    let mut frames = Vec::new();
    for line in lines.by_ref() {
        let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
        match (v["type"].as_str(), v["id"].as_str()) {
            (Some("result"), Some("a")) => {
                assert_eq!(v["error"]["code"], "wrong_clock");
                saw_wrong_clock = true;
            }
            (Some("result"), Some("x")) => {
                assert_eq!(v["ok"], true);
                saw_ok = true;
            }
            (Some("state"), _) => {
                assert!(v["host_time_ns"].is_u64());
                frames.push(v["frame"].as_u64().unwrap());
                if saw_ok && frames.len() >= 4 {
                    assert_eq!(v["mixer"]["crossfader"], 0.25);
                    break;
                }
            }
            _ => {}
        }
    }
    assert!(saw_wrong_clock && saw_ok);
    // The wall clock moves: frames strictly increase between state messages.
    assert!(frames.windows(2).all(|w| w[1] > w[0]), "{frames:?}");
    drop(stdin); // EOF: the engine exits when its supervisor goes away.
    assert!(child.wait().unwrap().success());
}

/// A load that fails to decode refuses the commands queued behind it rather
/// than running them against the deck's previous track. A FIFO holds the
/// decode open, so the queued command is deterministically behind the load.
#[cfg(unix)]
#[test]
fn a_failed_load_refuses_the_commands_queued_behind_it() {
    let d = temp_dir("cli-failed-load");
    let good = write_wav(&d, "a.wav", 44100, &sine(44100, 440.0, 5.0));
    let fifo = d.join("slow.wav");
    assert!(Command::new("mkfifo").arg(&fifo).status().unwrap().success());
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "wall"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut result_for = |id: &str| -> Value {
        for line in lines.by_ref() {
            let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
            if v["type"] == "result" && v["id"] == id {
                return v;
            }
        }
        panic!("no result for {id}");
    };
    let load = |id: &str, path: &std::path::Path| {
        json!({"id": id, "cmd": {"type": "load", "deck": 1, "path": path.to_str().unwrap()}})
    };
    writeln!(stdin, "{}", load("good", &good)).unwrap();
    assert_eq!(result_for("good")["ok"], true);

    writeln!(stdin, "{}", load("bad", &fifo)).unwrap();
    writeln!(stdin, "{}", json!({"id": "queued", "cmd": {"type": "play", "deck": 1, "playing": true}})).unwrap();
    // Feed the decoder bytes that are not audio, then close: the load fails.
    std::fs::write(&fifo, b"not audio at all").unwrap();
    assert_eq!(result_for("bad")["ok"], false);
    let queued = result_for("queued");
    assert_eq!(queued["ok"], false, "{queued}");
    assert!(queued["error"]["message"].as_str().unwrap().contains("waited on failed"), "{queued}");

    // Control: with nothing pending, the same command runs on the old track.
    writeln!(stdin, "{}", json!({"id": "direct", "cmd": {"type": "play", "deck": 1, "playing": true}})).unwrap();
    assert_eq!(result_for("direct")["ok"], true);
    drop(stdin);
    assert!(child.wait().unwrap().success());
}

/// Every command sent before stdin closes gets its result, even when the
/// audio side is asleep between long blocks at that moment (~85 ms here).
#[test]
fn commands_sent_just_before_eof_still_get_results() {
    for round in 0..5 {
        let mut child = Command::new(BIN)
            .args(["serve", "--clock", "wall", "--sample-rate", "12000", "--block", "1024"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdin = child.stdin.take().unwrap();
        let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
        let hello: Value = serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap();
        assert_eq!(hello["clock"], "wall");
        writeln!(stdin, "{}", json!({"id": "last", "cmd": {"type": "crossfader", "value": 0.5}})).unwrap();
        drop(stdin);
        let got = lines.any(|l| {
            let v: Value = serde_json::from_str(&l.unwrap()).unwrap();
            v["type"] == "result" && v["id"] == "last" && v["ok"] == true
        });
        assert!(got, "round {round}: no result for a command sent before EOF");
        assert!(child.wait().unwrap().success());
    }
}

/// A load still decoding when stdin closes finishes before the engine exits,
/// and the command parked behind it runs; one that never finishes is refused
/// once shutdown stops waiting, along with what is parked behind it.
#[cfg(unix)]
#[test]
fn loads_in_flight_at_eof_still_get_results() {
    let d = temp_dir("cli-load-at-eof");
    let wav = write_wav(&d, "b.wav", 44100, &sine(44100, 440.0, 2.0));
    let wav_bytes = std::fs::read(&wav).unwrap();
    for finishes in [true, false] {
        let fifo = d.join(format!("slow-{finishes}.wav"));
        assert!(Command::new("mkfifo").arg(&fifo).status().unwrap().success());
        let mut child = Command::new(BIN)
            .args(["serve", "--clock", "wall"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .unwrap();
        let mut stdin = child.stdin.take().unwrap();
        let lines = BufReader::new(child.stdout.take().unwrap()).lines();
        writeln!(stdin, "{}", json!({"id": "slow", "cmd": {"type": "load", "deck": 1, "path": fifo.to_str().unwrap()}}))
            .unwrap();
        writeln!(stdin, "{}", json!({"id": "queued", "cmd": {"type": "play", "deck": 1, "playing": true}})).unwrap();
        drop(stdin);
        if finishes {
            // Give the engine time to see EOF while the decode is still open.
            std::thread::sleep(std::time::Duration::from_millis(200));
            // On its own thread: if the engine has already gone, opening the
            // FIFO blocks forever, and the missing results below must fail.
            let (fifo, bytes) = (fifo.clone(), wav_bytes.clone());
            std::thread::spawn(move || std::fs::write(&fifo, &bytes));
        }
        let results: Vec<Value> = lines
            .map(|l| serde_json::from_str::<Value>(&l.unwrap()).unwrap())
            .filter(|v| v["type"] == "result")
            .collect();
        let of = |id: &str| results.iter().find(|v| v["id"] == id).cloned();
        let slow = of("slow").unwrap_or_else(|| panic!("finishes={finishes}: no result for the load: {results:?}"));
        let queued = of("queued").unwrap_or_else(|| panic!("finishes={finishes}: no result for the play: {results:?}"));
        assert_eq!(slow["ok"], finishes, "{slow}");
        assert_eq!(queued["ok"], finishes, "{queued}");
        if !finishes {
            assert!(slow["error"]["message"].as_str().unwrap().contains("shut down"), "{slow}");
        }
        assert!(child.wait().unwrap().success());
    }
}
