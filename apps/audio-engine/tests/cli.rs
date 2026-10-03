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
fn render_refuses_outputs_that_collide_before_writing_anything() {
    let d = temp_dir("cli-collide");
    write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 1.0));
    let a_before = std::fs::read(d.join("a.wav")).unwrap();
    let plan = json!({"end": {"ms": 500}, "events": [
        {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
        {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}},
        // A deck loaded twice still writes one file, so it collides with nothing.
        {"at": {"ms": 100}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}]});
    std::fs::write(d.join("plan.json"), plan.to_string()).unwrap();
    let run = |out: std::path::PathBuf, decks: Option<std::path::PathBuf>| {
        let mut c = Command::new(BIN);
        c.args(["render", "--plan"]).arg(d.join("plan.json")).arg("--out").arg(out);
        if let Some(dir) = decks {
            c.arg("--decks-out").arg(dir);
        }
        c.output().unwrap()
    };
    // Codex's case: the mix at DIR/deck1.wav would be replaced by deck 1,
    // with the summary still naming it the mix. Spelled another way too.
    for out in [d.join("decks").join("deck1.wav"), d.join("decks").join(".").join("deck1.wav")] {
        let o = run(out.clone(), Some(d.join("decks")));
        assert!(!o.status.success(), "{}", out.display());
        let err = String::from_utf8_lossy(&o.stderr);
        assert!(err.contains("deck 1's output and --out are both"), "{err}");
        assert!(!d.join("decks").exists(), "something was written");
    }
    // Codex's case: a directory spelled through one that does not exist yet
    // (which the render would create) still names the same place.
    std::fs::create_dir_all(d.join("out")).unwrap();
    let o = run(d.join("out").join("deck1.wav"), Some(d.join("missing").join("..").join("out")));
    assert!(!o.status.success());
    assert!(String::from_utf8_lossy(&o.stderr).contains("are both"), "{}", String::from_utf8_lossy(&o.stderr));
    assert!(!d.join("missing").exists() && !d.join("out").join("deck1.wav").exists(), "something was written");
    // An output over an input would destroy it: the plan, or a track.
    for (out, input) in [(d.join("plan.json"), "plan.json"), (d.join("a.wav"), "a.wav")] {
        let o = run(out, None);
        assert!(!o.status.success());
        let err = String::from_utf8_lossy(&o.stderr);
        assert!(err.contains("would overwrite") && err.contains(input), "{err}");
    }
    assert_eq!(std::fs::read(d.join("a.wav")).unwrap(), a_before);
    // Codex's case: a hard link is the file it links to under another
    // spelling, so writing it would truncate the track mid-read, or write
    // the mix and a deck output into one file.
    #[cfg(unix)]
    {
        std::fs::hard_link(d.join("a.wav"), d.join("linked.wav")).unwrap();
        let o = run(d.join("linked.wav"), None);
        assert!(!o.status.success());
        let err = String::from_utf8_lossy(&o.stderr);
        assert!(err.contains("would overwrite") && err.contains("a.wav"), "{err}");
        assert_eq!(std::fs::read(d.join("a.wav")).unwrap(), a_before);
        std::fs::create_dir_all(d.join("hl")).unwrap();
        std::fs::write(d.join("hl-mix.wav"), b"old").unwrap();
        std::fs::hard_link(d.join("hl-mix.wav"), d.join("hl").join("deck1.wav")).unwrap();
        let o = run(d.join("hl-mix.wav"), Some(d.join("hl")));
        assert!(!o.status.success());
        assert!(String::from_utf8_lossy(&o.stderr).contains("are both"), "{}", String::from_utf8_lossy(&o.stderr));
        assert_eq!(std::fs::read(d.join("hl-mix.wav")).unwrap(), b"old", "something was written");
        // Codex's case: a dangling symlink is followed by the write, which
        // creates its target, so `--out mix.wav -> deck1.wav` beside the deck
        // outputs is deck 1's file before either exists.
        std::fs::create_dir_all(d.join("sl")).unwrap();
        std::os::unix::fs::symlink("deck1.wav", d.join("sl").join("mix.wav")).unwrap();
        let o = run(d.join("sl").join("mix.wav"), Some(d.join("sl")));
        assert!(!o.status.success());
        assert!(String::from_utf8_lossy(&o.stderr).contains("are both"), "{}", String::from_utf8_lossy(&o.stderr));
        assert!(!d.join("sl").join("deck1.wav").exists(), "something was written");
        // A link met part-way through a path resolves too: `..` after a
        // directory link leaves the link's target, not the link's directory.
        std::fs::create_dir_all(d.join("sl").join("real").join("inner")).unwrap();
        std::os::unix::fs::symlink(d.join("sl").join("real").join("inner"), d.join("sl").join("dirlink")).unwrap();
        let o = run(d.join("sl").join("dirlink").join("..").join("deck1.wav"), Some(d.join("sl").join("real")));
        assert!(!o.status.success());
        assert!(String::from_utf8_lossy(&o.stderr).contains("are both"), "{}", String::from_utf8_lossy(&o.stderr));
        // Control: a dangling symlink to a file of its own renders through it.
        std::os::unix::fs::symlink("elsewhere.wav", d.join("sl").join("mix2.wav")).unwrap();
        let o = run(d.join("sl").join("mix2.wav"), Some(d.join("sl")));
        assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
        assert!(d.join("sl").join("elsewhere.wav").exists() && d.join("sl").join("deck1.wav").exists());
        // Control: a copy is a file of its own, and is overwritten.
        std::fs::copy(d.join("a.wav"), d.join("copy.wav")).unwrap();
        let o = run(d.join("copy.wav"), None);
        assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
        assert_eq!(std::fs::read(d.join("a.wav")).unwrap(), a_before);
        // Control: an existing output longer than the render is replaced
        // whole, not written over its head (outputs are opened without
        // truncating until every one is known apart).
        let o = run(d.join("fresh.wav"), None);
        assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
        let fresh = std::fs::read(d.join("fresh.wav")).unwrap();
        std::fs::write(d.join("long.wav"), vec![7u8; fresh.len() + 4096]).unwrap();
        let o = run(d.join("long.wav"), None);
        assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
        assert!(std::fs::read(d.join("long.wav")).unwrap() == fresh, "the old file's tail survived the render");
    }
    // Codex on 2f28895b and b68a0857: on a volume that folds case (or
    // Unicode form) `Deck1.wav` is deck 1's file. No rule on names can know
    // how a volume folds, so the render asks the volume: it opens every
    // output before writing any and refuses two that are one file. Probe this
    // volume the same way to know which answer is right here.
    std::fs::write(d.join("probe-case"), b"").unwrap();
    let folds = d.join("PROBE-CASE").exists();
    let ci = d.join("ci");
    for out in [ci.join("Deck1.wav"), ci.join("DECK1.WAV"), d.join("CI").join("deck1.wav")] {
        let o = run(out.clone(), Some(ci.clone()));
        let err = String::from_utf8_lossy(&o.stderr);
        assert_eq!(o.status.success(), !folds, "{} folds {folds}: {err}", out.display());
        if folds {
            assert!(err.contains("deck 1's output and --out are both"), "{err}");
            assert!(!ci.exists(), "a refused render left files behind");
        } else {
            assert!(out.exists() && ci.join("deck1.wav").exists(), "{}", out.display());
            let _ = std::fs::remove_dir_all(&ci);
            let _ = std::fs::remove_dir_all(d.join("CI"));
        }
    }
    // Once deck 1's file exists the answer is the same, and again with both
    // files there: two existing files are told apart by what they are on
    // disk, not by their names.
    std::fs::create_dir_all(&ci).unwrap();
    std::fs::write(ci.join("deck1.wav"), b"old").unwrap();
    for _ in 0..2 {
        let o = run(ci.join("Deck1.wav"), Some(ci.clone()));
        assert_eq!(o.status.success(), !folds, "folds {folds}, stderr: {}", String::from_utf8_lossy(&o.stderr));
    }
    if folds {
        assert_eq!(std::fs::read(ci.join("deck1.wav")).unwrap(), b"old", "a refused render wrote");
    }
    // Control: a name that differs in more than case is its own file.
    let o = run(ci.join("deck1-mix.wav"), Some(ci.clone()));
    assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
    // Control: a missing directory that does not lead back is elsewhere.
    let o = run(d.join("out").join("deck1.wav"), Some(d.join("missing").join("..").join("elsewhere")));
    assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
    assert!(d.join("elsewhere").join("deck1.wav").exists() && d.join("out").join("deck1.wav").exists());
    // Control: the mix beside the deck outputs, under its own name, renders.
    let o = run(d.join("mix.wav"), Some(d.clone()));
    assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
    assert!(d.join("mix.wav").exists() && d.join("deck1.wav").exists());
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
fn a_session_refuses_to_load_the_file_it_records_to() {
    // Codex's case: `--record a.wav` with a.wav loaded would decode a.wav and
    // then truncate it with the recording when the session ends.
    let d = temp_dir("cli-record");
    write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 0.5));
    write_wav(&d, "b.wav", 48000, &sine(48000, 330.0, 0.5));
    #[cfg(unix)]
    std::fs::hard_link(d.join("a.wav"), d.join("linked.wav")).unwrap();
    let a_before = std::fs::read(d.join("a.wav")).unwrap();
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "fake", "--record", "a.wav"])
        .current_dir(&d)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut next = || -> Value { serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap() };
    assert_eq!(next()["type"], "hello");
    let mut say = |v: Value| writeln!(stdin, "{v}").unwrap();
    let mut paths = vec!["a.wav", "./a.wav"];
    if cfg!(unix) {
        paths.push("linked.wav");
    }
    for path in paths {
        say(json!({"id": path, "cmd": {"type": "load", "deck": 1, "path": path}}));
        let r = next();
        assert_eq!(r["ok"], false, "{path}: {r}");
        assert!(r["error"]["message"].as_str().unwrap().contains("--record file"), "{path}: {r}");
    }
    assert_eq!(std::fs::read(d.join("a.wav")).unwrap(), a_before);
    // Control: another file loads and plays into the recording.
    say(json!({"id": "b", "cmd": {"type": "load", "deck": 1, "path": "b.wav"}}));
    assert_eq!(next()["ok"], true);
    say(json!({"id": "p", "cmd": {"type": "play", "deck": 1, "playing": true}}));
    assert_eq!(next()["ok"], true);
    say(json!({"id": "a", "cmd": {"type": "engine_advance", "ms": 100}}));
    assert_eq!(next()["ok"], true);
    next();
    say(json!({"id": "q", "cmd": {"type": "engine_shutdown"}}));
    assert_eq!(next()["ok"], true);
    assert!(child.wait().unwrap().success());
    let rec = odj_audio::decode::decode_file(&d.join("a.wav")).unwrap();
    assert_eq!(rec.pcm.len(), 4800 * 2, "the recording was written where asked");
}

#[test]
fn a_recording_is_not_written_over_a_track_its_path_became_during_the_session() {
    // Codex on d959eae8: the --record path was checked against each track
    // only as it loaded, then created with truncation when the session
    // ended, so a path relinked to a loaded track in between emptied it.
    let d = temp_dir("cli-record-relinked");
    write_wav(&d, "b.wav", 48000, &sine(48000, 330.0, 0.5));
    let b_before = std::fs::read(d.join("b.wav")).unwrap();
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "fake", "--record", "rec.wav"])
        .current_dir(&d)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut next = || -> Value { serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap() };
    assert_eq!(next()["type"], "hello");
    let mut say = |v: Value| writeln!(stdin, "{v}").unwrap();
    say(json!({"id": "b", "cmd": {"type": "load", "deck": 1, "path": "b.wav"}}));
    assert_eq!(next()["ok"], true);
    say(json!({"id": "p", "cmd": {"type": "play", "deck": 1, "playing": true}}));
    assert_eq!(next()["ok"], true);
    say(json!({"id": "a", "cmd": {"type": "engine_advance", "ms": 100}}));
    assert_eq!(next()["ok"], true);
    // The session is live and b.wav loaded: now rec.wav becomes b.wav.
    std::fs::hard_link(d.join("b.wav"), d.join("rec.wav")).unwrap();
    drop(stdin);
    let o = child.wait_with_output().unwrap();
    assert!(!o.status.success(), "the recording was written over b.wav");
    let err = String::from_utf8_lossy(&o.stderr);
    assert!(err.contains("would overwrite") && err.contains("b.wav"), "{err}");
    assert_eq!(std::fs::read(d.join("b.wav")).unwrap(), b_before, "b.wav was changed");
}

#[test]
fn a_recording_is_not_written_over_a_track_renamed_away_during_the_session() {
    // Codex on d07e4db1: only the loaded paths were asked again, so a track
    // renamed away and replaced at its path, with the recording then linked
    // to the file that was loaded, was emptied.
    let d = temp_dir("cli-record-renamed");
    write_wav(&d, "b.wav", 48000, &sine(48000, 330.0, 0.5));
    let b_before = std::fs::read(d.join("b.wav")).unwrap();
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "fake", "--record", "rec.wav"])
        .current_dir(&d)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut next = || -> Value { serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap() };
    assert_eq!(next()["type"], "hello");
    let mut say = |v: Value| writeln!(stdin, "{v}").unwrap();
    say(json!({"id": "b", "cmd": {"type": "load", "deck": 1, "path": "b.wav"}}));
    assert_eq!(next()["ok"], true);
    say(json!({"id": "a", "cmd": {"type": "engine_advance", "ms": 100}}));
    assert_eq!(next()["ok"], true);
    std::fs::rename(d.join("b.wav"), d.join("b-old.wav")).unwrap();
    write_wav(&d, "b.wav", 48000, &sine(48000, 440.0, 0.5));
    std::fs::hard_link(d.join("b-old.wav"), d.join("rec.wav")).unwrap();
    drop(stdin);
    let o = child.wait_with_output().unwrap();
    assert!(!o.status.success(), "the recording was written over the track that was loaded");
    let err = String::from_utf8_lossy(&o.stderr);
    assert!(err.contains("since renamed or replaced"), "{err}");
    assert_eq!(std::fs::read(d.join("b-old.wav")).unwrap(), b_before, "the loaded track was changed");
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

    // An advance too long to finish is refused before rendering anything,
    // whether asked in frames or in a huge but finite ms.
    for adv in [json!({"frames": u64::MAX}), json!({"ms": 1e15}), json!({"frames": 3600 * 48000 + 1})] {
        let mut c = json!({"type": "engine_advance"});
        c.as_object_mut().unwrap().extend(adv.as_object().unwrap().clone());
        say(json!({"id": "big", "cmd": c}));
        let r = next();
        assert_eq!(r["ok"], false, "{adv}: {r}");
        assert_eq!(r["error"]["code"], "invalid", "{adv}: {r}");
    }
    say(json!({"id": "st", "cmd": {"type": "engine_state"}}));
    assert_eq!(next()["state_seq"], 1, "the first engine_state is numbered 1");
    let st = next();
    assert_eq!(st["frame"], 48000, "a refused advance renders nothing");
    assert_eq!(st["state_seq"], 1, "the state that answers it carries its number");

    say(json!({"id": "s", "cmd": {"type": "stem_mute", "deck": 1, "stem": "vocal", "muted": true}}));
    let r = next();
    assert_eq!(r["ok"], false);
    assert_eq!(r["error"]["code"], "not_implemented");
    // A numeric id comes back as the same number, and an id the engine
    // could not echo is refused without running the command.
    say(json!({"id": 7, "cmd": {"type": "engine_state"}}));
    assert_eq!(next(), json!({"type": "result", "id": 7, "ok": true, "state_seq": 2}));
    assert_eq!(next()["state_seq"], 2);
    say(json!({"id": true, "cmd": {"type": "engine_shutdown"}}));
    let r = next();
    assert_eq!((r["id"].clone(), r["ok"].clone()), (Value::Null, json!(false)), "{r}");
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
                // Heard and sent on one clock: the mapping a receiver uses
                // is their difference, and on the wall clock (no device
                // latency) that is within a block or two of now.
                let heard_in = v["heard_in_ns"].as_i64().unwrap();
                assert_eq!(heard_in, v["host_time_ns"].as_i64().unwrap() - v["sent_ns"].as_i64().unwrap());
                assert!(heard_in.abs() < 100_000_000, "heard_in_ns {heard_in}");
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

#[test]
fn each_engine_state_is_answered_by_the_first_state_carrying_its_number() {
    // Codex on d07e4db1: states carry no request id and the periodic feed
    // keeps flowing, so the answer to an `engine_state` is marked by number.
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "wall"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut next = || -> Value { serde_json::from_str(&lines.next().unwrap().unwrap()).unwrap() };
    assert_eq!(next()["type"], "hello");
    let mut last_seq = 0;
    for (k, x) in [0.25, 0.75, 0.5].into_iter().enumerate() {
        writeln!(stdin, "{}", json!({"id": "x", "cmd": {"type": "crossfader", "value": x}})).unwrap();
        writeln!(stdin, "{}", json!({"id": "s", "cmd": {"type": "engine_state"}})).unwrap();
        let mut want = None;
        loop {
            let v = next();
            if v["type"] == "state" {
                let n = v["state_seq"].as_u64().unwrap();
                assert!(n >= last_seq, "state_seq went back: {n} after {last_seq}");
                last_seq = n;
                if want.is_some_and(|w| n >= w) {
                    assert_eq!(v["mixer"]["crossfader"], x, "the state carrying request {n} is from before it");
                    break;
                }
            } else if v["id"] == "s" {
                assert_eq!(v["ok"], true);
                assert_eq!(v["state_seq"], k as u64 + 1, "{v}");
                want = v["state_seq"].as_u64();
            }
        }
    }
    drop(stdin);
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

/// `set_beatgrid` over the threaded (wall clock) session: sent while the load
/// is still decoding, it waits behind it and then takes effect, and it is
/// refused on an empty deck.
#[test]
fn set_beatgrid_reaches_a_loaded_deck_without_reloading() {
    let d = temp_dir("cli-set-beatgrid");
    let wav = write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 4.0));
    let mut child = Command::new(BIN).args(["serve", "--clock", "wall"]).stdin(Stdio::piped()).stdout(Stdio::piped()).spawn().unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
    let mut until = |pred: &dyn Fn(&Value) -> bool| -> Value {
        for line in lines.by_ref() {
            let v: Value = serde_json::from_str(&line.unwrap()).unwrap();
            if pred(&v) {
                return v;
            }
        }
        panic!("stream ended");
    };
    let mut say = |v: Value| writeln!(stdin, "{v}").unwrap();
    // Control first: a track with no grid and no BPM cannot beat-jump.
    say(json!({"id": "l0", "cmd": {"type": "load", "deck": 1, "path": wav.to_str().unwrap()}}));
    say(json!({"id": "j0", "cmd": {"type": "beat_jump", "deck": 1, "beats": 1}}));
    assert_eq!(until(&|v| v["id"] == "j0")["ok"], false);
    say(json!({"id": "l", "cmd": {"type": "load", "deck": 1, "path": wav.to_str().unwrap()}}));
    say(json!({"id": "g", "cmd": {"type": "set_beatgrid", "deck": 1, "beatgrid_ms": [0, 400, 800, 1200]}}));
    say(json!({"id": "j", "cmd": {"type": "beat_jump", "deck": 1, "beats": 1}}));
    let g = until(&|v| v["id"] == "g");
    assert_eq!(g["ok"], true, "{g}");
    let j = until(&|v| v["id"] == "j");
    assert_eq!(j["ok"], true, "{j}");
    // Sent after, since its immediate reply would overtake the queued ones.
    say(json!({"id": "e", "cmd": {"type": "set_beatgrid", "deck": 2, "beatgrid_ms": [0, 500]}}));
    let e = until(&|v| v["id"] == "e");
    assert_eq!(e["error"]["code"], "no_track", "{e}");
    say(json!({"id": "s", "cmd": {"type": "engine_state"}}));
    let st = until(&|v| v["type"] == "state");
    let pos = st["decks"][0]["position_ms"].as_f64().unwrap();
    assert!((pos - 400.0).abs() < 1e-6, "the jump used the new grid: {pos}");
    drop(stdin);
    assert!(child.wait().unwrap().success());
}

/// A tracked MP3 (mono, 22.05 kHz, about 3 s) from the repo's fixtures.
fn mp3_fixture() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/phase7-dedup/src-128.mp3")
}

/// A tracked AAC clip (stereo, 44.1 kHz, about 60 s).
fn m4a_fixture() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../scripts/bench/clips-edge/am-contra-heart-peripheral-cfg-a.m4a")
}

/// A tracked mono AAC clip (22.05 kHz, about 3 s) with an edit list.
fn m4a_mono_fixture() -> std::path::PathBuf {
    std::path::PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/phase7-dedup/src.m4a")
}

/// Interleaved f32 samples and (rate, channels) of a float WAV `decode` wrote.
fn read_f32_wav(p: &std::path::Path) -> (u32, u16, Vec<f32>) {
    let b = std::fs::read(p).unwrap();
    assert_eq!(&b[0..4], b"RIFF");
    assert_eq!(&b[8..16], b"WAVEfmt ");
    assert_eq!(u16::from_le_bytes([b[20], b[21]]), 3, "IEEE float");
    let ch = u16::from_le_bytes([b[22], b[23]]);
    let sr = u32::from_le_bytes([b[24], b[25], b[26], b[27]]);
    assert_eq!(&b[36..40], b"data");
    let n = u32::from_le_bytes([b[40], b[41], b[42], b[43]]) as usize;
    assert_eq!(b.len(), 44 + n, "the data size in the header is the file's");
    assert_eq!(u32::from_le_bytes([b[4], b[5], b[6], b[7]]) as usize, 36 + n);
    let pcm = b[44..].chunks_exact(4).map(|c| f32::from_le_bytes([c[0], c[1], c[2], c[3]])).collect();
    (sr, ch, pcm)
}

#[test]
fn decode_writes_compressed_audio_as_a_wav_at_its_own_rate_and_channels() {
    let d = temp_dir("cli-decode");
    // (source, rate, channels, frames ffmpeg 6.1 decodes, the deck's frames,
    // frames of encoder priming both trim). MP3 is gapless in both. Both
    // apply an M4A's edit list (src/mp4edit.rs), so both start after the
    // 1024 frames of AAC priming; at the edit's end the deck cuts exactly
    // while `decode` keeps the straddling packet whole, as ffmpeg does.
    let cases = [
        (mp3_fixture(), 22050u32, 1u16, 66_150u64, 66_150u64, 0usize),
        (m4a_fixture(), 44100, 2, 2_646_016, 2_646_000, 1024),
        (m4a_mono_fixture(), 22050, 1, 66_560, 66_150, 1024),
    ];
    for (src, want_sr, want_ch, ffmpeg_frames, deck_frames, priming) in cases {
        let out = d.join(format!("{}.wav", src.file_stem().unwrap().to_string_lossy()));
        let o = Command::new(BIN).arg("decode").arg("--in").arg(&src).arg("--out").arg(&out).output().unwrap();
        assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
        let s: Value = serde_json::from_slice(&o.stdout).unwrap();
        assert_eq!(s["type"], "decode");
        assert_eq!(s["sample_rate"], want_sr, "{}", src.display());
        assert_eq!(s["channels"], want_ch, "{}", src.display());
        assert_eq!(s["edit_list"], if priming > 0 { "applied" } else { "none" }, "{}", src.display());
        assert_eq!(s["trimmed_start_frames"], priming as u64, "{}", src.display());
        let (sr, ch, pcm) = read_f32_wav(&out);
        assert_eq!((sr, ch), (want_sr, want_ch));
        assert_eq!(s["frames"].as_u64().unwrap() as usize, pcm.len() / ch as usize);
        assert_eq!(pcm.len() as u64 / u64::from(ch), ffmpeg_frames, "ffmpeg's frame count: {}", src.display());
        // The same samples a deck decodes, streamed instead of held, from the
        // same first frame: frame i is the deck's frame i, so neither is offset
        // against the other or against ffmpeg. The deck's decode is stereo, so
        // compare each frame's first channel and, for mono, the copy it makes
        // for the right side.
        let deck = odj_audio::decode::decode_file(&src).unwrap();
        assert_eq!(deck.sample_rate, want_sr);
        assert_eq!(deck.pcm.len() as u64 / 2, deck_frames, "the deck's frame count: {}", src.display());
        for (i, (frame, (l, r))) in pcm.chunks_exact(ch as usize).zip(deck.pcm.chunks_exact(2).map(|f| (f[0], f[1]))).enumerate() {
            assert_eq!(frame[0], l, "frame {i} of {}", src.display());
            assert_eq!(*frame.get(1).unwrap_or(&frame[0]), r, "frame {i} of {}", src.display());
        }
        assert!(pcm.iter().any(|s| s.abs() > 0.01), "decoded audio, not silence");
    }
}

#[test]
fn decode_never_replaces_a_file_and_leaves_nothing_when_it_fails() {
    let d = temp_dir("cli-decode-refuse");
    // OUT naming the source itself: refused, and the source is untouched.
    let src = d.join("src.mp3");
    std::fs::copy(mp3_fixture(), &src).unwrap();
    let before = std::fs::read(&src).unwrap();
    let o = Command::new(BIN).arg("decode").arg("--in").arg(&src).arg("--out").arg(&src).output().unwrap();
    assert!(!o.status.success());
    assert!(String::from_utf8_lossy(&o.stderr).contains("never replaces"), "{}", String::from_utf8_lossy(&o.stderr));
    assert_eq!(std::fs::read(&src).unwrap(), before, "the source must be untouched");
    // Input that is not audio: an error, and no partial OUT left behind.
    let junk = d.join("junk.mp3");
    std::fs::write(&junk, b"this is not an mp3").unwrap();
    let out = d.join("junk.wav");
    let o = Command::new(BIN).arg("decode").arg("--in").arg(&junk).arg("--out").arg(&out).output().unwrap();
    assert!(!o.status.success());
    assert!(!out.exists(), "a failed decode removes the OUT it created");
    // Control: the same OUT path is written once the input is real audio.
    let o = Command::new(BIN).arg("decode").arg("--in").arg(&src).arg("--out").arg(&out).output().unwrap();
    assert!(o.status.success(), "stderr: {}", String::from_utf8_lossy(&o.stderr));
    assert!(out.is_file());
    // Missing flags are usage errors, not a panic.
    let o = Command::new(BIN).args(["decode", "--in"]).arg(&src).output().unwrap();
    assert_eq!(o.status.code(), Some(2));
}

/// `decode` writes the file's PCM to stdout and names what it wrote on stderr.
#[test]
fn decode_streams_pcm_and_a_summary() {
    let d = temp_dir("cli-decode");
    let wav = write_wav(&d, "a.wav", 44100, &sine(44100, 440.0, 1.0));
    let out = Command::new(BIN).args(["decode", "--mono", "--format", "s16le"]).arg(&wav).output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    assert_eq!(out.stdout.len(), 44100 * 2, "one s16 sample per frame in mono");
    let summary: Value = serde_json::from_str(String::from_utf8_lossy(&out.stderr).trim().lines().last().unwrap()).unwrap();
    assert_eq!(summary, json!({"sample_rate": 44100, "channels": 1, "frames": 44100, "format": "s16le"}));

    // --rate resamples to exactly round(frames * to / from) frames.
    let out = Command::new(BIN).args(["decode", "--rate", "48000"]).arg(&wav).output().unwrap();
    assert!(out.status.success());
    assert_eq!(out.stdout.len(), 48000 * 2 * 4, "stereo f32 at 48 kHz");

    // Not audio: a failure with a reason, and no PCM a reader could mistake for a decode.
    let junk = d.join("junk.mp3");
    std::fs::write(&junk, b"not audio at all").unwrap();
    let out = Command::new(BIN).arg("decode").arg(&junk).output().unwrap();
    assert!(!out.status.success());
    assert!(out.stdout.is_empty());
}

/// `probe` reports the length the header states, without decoding.
#[test]
fn probe_reports_the_stated_length() {
    let d = temp_dir("cli-probe");
    let wav = write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 2.5));
    let out = Command::new(BIN).arg("probe").arg(&wav).output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["sample_rate"], 48000);
    assert_eq!(v["frames"], 120000);
    assert_eq!(v["duration_s"], 2.5);
    assert_eq!(v["source"], "header");
}

/// An AAC m4a decodes in time with the file: its encoder priming frames are
/// trimmed by the MP4 edit list, which symphonia itself does not apply. The
/// fixture (ffmpeg's AAC encoder, 1024 priming frames) is one second with a
/// click at exactly 0.25 s; untrimmed, the click lands 1024 frames late.
#[test]
fn an_m4a_decodes_without_its_priming_frames() {
    let m4a = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/audio/click-250ms-aac.m4a");
    let out = Command::new(BIN).args(["decode", "--mono", m4a]).output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let pcm: Vec<f32> = out.stdout.chunks_exact(4).map(|b| f32::from_le_bytes(b.try_into().unwrap())).collect();
    assert_eq!(pcm.len(), 44100, "the edit's playable length, padding dropped");
    let peak = (0..pcm.len()).max_by(|&a, &b| pcm[a].abs().total_cmp(&pcm[b].abs())).unwrap();
    assert!((11025..11040).contains(&peak), "click at frame {peak}, expected 11025 (0.25 s)");

    let out = Command::new(BIN).args(["probe", m4a]).output().unwrap();
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["frames"], 44100);
    assert_eq!(v["delay"], 1024);
}

/// An AAC m4a with no edit list, only iTunes gapless metadata, also decodes
/// in time: Apple's encoders commonly write just the `iTunSMPB` tag, and with
/// nothing reading it those files played 2112 frames (47.9 ms) late. The
/// fixture is the same one-second click encoded by ffmpeg with
/// `-use_editlist 0`, then tagged with an `iTunSMPB` stating its 1024 priming
/// frames and 44100-frame length, so the tag is the only thing that can trim it.
#[test]
fn an_m4a_with_only_an_itunes_gapless_tag_decodes_without_its_priming() {
    let m4a = concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/audio/click-250ms-aac-itunsmpb.m4a");
    let raw = std::fs::read(m4a).unwrap();
    assert!(!raw.windows(4).any(|w| w == b"elst"), "the fixture must have no edit list");
    assert!(raw.windows(8).any(|w| w == b"iTunSMPB"), "the fixture must carry the gapless tag");
    let out = Command::new(BIN).args(["decode", "--mono", m4a]).output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let pcm: Vec<f32> = out.stdout.chunks_exact(4).map(|b| f32::from_le_bytes(b.try_into().unwrap())).collect();
    assert_eq!(pcm.len(), 44100, "the tag's original length, padding dropped");
    let peak = (0..pcm.len()).max_by(|&a, &b| pcm[a].abs().total_cmp(&pcm[b].abs())).unwrap();
    assert!((11025..11040).contains(&peak), "click at frame {peak}, expected 11025 (0.25 s)");

    let out = Command::new(BIN).args(["probe", m4a]).output().unwrap();
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["frames"], 44100);
    assert_eq!(v["delay"], 1024);

    // The streaming `decode --in/--out` (stems, vocals) cuts at the tag's
    // exact length too, rather than keeping the straddling packet whole as
    // it does for an edit list.
    let d = temp_dir("cli-itunsmpb");
    let wav = d.join("out.wav");
    let o = Command::new(BIN).args(["decode", "--in", m4a, "--out"]).arg(&wav).output().unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    let s: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!((s["edit_list"].as_str(), s["trimmed_start_frames"].as_u64()), (Some("applied"), Some(1024)));
    let (_, ch, pcm) = read_f32_wav(&wav);
    assert_eq!(pcm.len() / ch as usize, 44100, "the tag's exact length, straddling packet cut");
    let peak = (0..pcm.len()).max_by(|&a, &b| pcm[a].abs().total_cmp(&pcm[b].abs())).unwrap() / ch as usize;
    assert!((11025..11040).contains(&peak), "click at frame {peak}, expected 11025 (0.25 s)");

    // A stale tag whose delay (0xF000 = 61440) outlasts the stream (45
    // packets, 46080 frames) is ignored by both paths, which decode untrimmed rather
    // than fail or go silent.
    let stale_bytes = {
        let i = raw.windows(8).position(|w| w == b"00000400").unwrap();
        let mut b = raw.clone();
        b[i..i + 8].copy_from_slice(b"0000F000");
        b
    };
    let stale = d.join("stale.m4a");
    std::fs::write(&stale, stale_bytes).unwrap();
    let wav = d.join("stale.wav");
    let o = Command::new(BIN).arg("decode").arg("--in").arg(&stale).arg("--out").arg(&wav).output().unwrap();
    assert!(o.status.success(), "{}", String::from_utf8_lossy(&o.stderr));
    let s: Value = serde_json::from_slice(&o.stdout).unwrap();
    assert_eq!((s["edit_list"].as_str(), s["trimmed_start_frames"].as_u64()), (Some("none"), Some(0)));
    let (_, ch, pcm) = read_f32_wav(&wav);
    assert_eq!(pcm.len() / ch as usize, 46080, "every decoded frame, untrimmed");
    let deck = odj_audio::decode::decode_file(&stale).unwrap();
    assert_eq!(deck.pcm.len() / 2, 46080, "the deck ignores the same edit");
}

/// An MP3 behind a large ID3v2 tag (several MB of embedded artwork) still
/// opens. symphonia's probe counted the tag against its 1 MiB scan limit and
/// gave up with "no suitable format reader found", while ffmpeg read the file.
#[test]
fn an_mp3_behind_a_large_id3_tag_opens() {
    let d = temp_dir("cli-big-tag");
    let mp3 = std::fs::read(concat!(env!("CARGO_MANIFEST_DIR"), "/tests/fixtures/audio/click-250ms.mp3")).unwrap();
    // One 3 MB private frame in an ID3v2.3 tag; sizes are syncsafe.
    let payload = vec![0u8; 3_000_000];
    let mut frame = b"PRIV".to_vec();
    frame.extend_from_slice(&(payload.len() as u32).to_be_bytes());
    frame.extend_from_slice(&[0, 0]);
    frame.extend_from_slice(&payload);
    let n = frame.len() as u32;
    let mut file = b"ID3\x03\x00\x00".to_vec();
    file.extend_from_slice(&[(n >> 21) as u8 & 0x7f, (n >> 14) as u8 & 0x7f, (n >> 7) as u8 & 0x7f, n as u8 & 0x7f]);
    file.extend_from_slice(&frame);
    file.extend_from_slice(&mp3);
    let tagged = d.join("tagged.mp3");
    std::fs::write(&tagged, file).unwrap();

    let out = Command::new(BIN).args(["probe"]).arg(&tagged).output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let v: Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["sample_rate"], 44100);

    let out = Command::new(BIN).args(["decode", "--mono"]).arg(&tagged).output().unwrap();
    assert!(out.status.success(), "{}", String::from_utf8_lossy(&out.stderr));
    let pcm: Vec<f32> = out.stdout.chunks_exact(4).map(|b| f32::from_le_bytes(b.try_into().unwrap())).collect();
    let peak = (0..pcm.len()).max_by(|&a, &b| pcm[a].abs().total_cmp(&pcm[b].abs())).unwrap();
    assert!((11000..11060).contains(&peak), "click at frame {peak}, expected about 11025 (0.25 s)");
}

#[test]
fn version_says_whether_this_build_can_record_and_record_refuses_cleanly_without_it() {
    // The sets recorder reads `capture` before choosing odj-audio over
    // ffmpeg, so it must match the build.
    let out = Command::new(BIN).arg("version").output().unwrap();
    assert!(out.status.success());
    let v: serde_json::Value = serde_json::from_slice(&out.stdout).unwrap();
    assert_eq!(v["capture"], serde_json::Value::Bool(cfg!(feature = "device")), "{v}");
    if cfg!(feature = "device") {
        return;
    }
    let tmp = tempfile::tempdir().unwrap();
    for args in [&["input-devices"][..], &["record", "--dir", tmp.path().to_str().unwrap(), "--device-index", "0"]] {
        let out = Command::new(BIN).args(args).output().unwrap();
        assert_eq!(out.status.code(), Some(2), "{args:?}");
        let err = String::from_utf8_lossy(&out.stderr);
        assert!(err.contains("rebuild with --features device"), "{args:?}: {err}");
    }
    assert_eq!(std::fs::read_dir(tmp.path()).unwrap().count(), 0, "nothing written");
    // A bad command line is named before the missing feature.
    let out = Command::new(BIN).args(["record", "--dir", "d"]).output().unwrap();
    assert!(String::from_utf8_lossy(&out.stderr).contains("exactly one of --device"));
}
