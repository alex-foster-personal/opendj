//! The engine's stdio as a supervisor on any OS drives it, Windows included:
//! what a text-mode writer there sends (CRLF line ends), and what the kernel
//! does to the pipes when the supervisor is gone.
//!
//! Python's `subprocess` in text mode turns every `\n` it writes into `\r\n`
//! on Windows, so the supervisor's commands arrive CRLF-terminated there. A
//! supervisor that dies takes both ends it held with it: the engine's stdin
//! reads end-of-file (covered by `cli.rs`), and its stdout has no reader,
//! which Windows reports as ERROR_NO_DATA rather than EPIPE.

mod common;

use std::io::{BufRead, BufReader, Read, Write};
use std::process::{Child, Command, ExitStatus, Stdio};
use std::time::{Duration, Instant};

use serde_json::{json, Value};

use common::*;

const BIN: &str = env!("CARGO_BIN_EXE_odj-audio");

/// The child's exit status, or a panic (with the child killed) when it is
/// still running after `within`: a hung engine must fail the test, not hang it.
fn exit_within(child: &mut Child, within: Duration, what: &str) -> ExitStatus {
    let deadline = Instant::now() + within;
    loop {
        if let Some(status) = child.try_wait().unwrap() {
            return status;
        }
        if Instant::now() >= deadline {
            child.kill().unwrap();
            child.wait().unwrap();
            panic!("{what}: odj-audio was still running after {within:?}");
        }
        std::thread::sleep(Duration::from_millis(10));
    }
}

#[test]
fn crlf_terminated_commands_are_taken_and_output_lines_end_in_lf_only() {
    let d = temp_dir("pipes-crlf");
    write_wav(&d, "a.wav", 48000, &sine(48000, 440.0, 1.0));
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "fake"])
        .current_dir(&d)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let mut stdin = child.stdin.take().unwrap();
    let mut send = |line: String| write!(stdin, "{line}\r\n").unwrap();
    send(json!({"id": "l", "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}).to_string());
    // A blank line with a CRLF end is a blank line, not a command to refuse.
    send(String::new());
    send(json!({"id": "p", "cmd": {"type": "play", "deck": 1, "playing": true}}).to_string());
    send(json!({"id": "a", "cmd": {"type": "engine_advance", "ms": 100}}).to_string());
    send(json!({"id": "q", "cmd": {"type": "engine_shutdown"}}).to_string());
    let mut raw = Vec::new();
    child.stdout.take().unwrap().read_to_end(&mut raw).unwrap();
    assert!(exit_within(&mut child, Duration::from_secs(10), "after engine_shutdown").success());
    // Control: there is output to look at, one whole line at a time.
    assert_eq!(raw.last(), Some(&b'\n'));
    assert!(!raw.contains(&b'\r'), "the engine wrote a carriage return: a reader splitting on LF would keep it");
    let lines: Vec<Value> = raw.split(|b| *b == b'\n').filter(|l| !l.is_empty()).map(|l| serde_json::from_slice(l).unwrap()).collect();
    let results: Vec<(&str, bool)> =
        lines.iter().filter(|v| v["type"] == "result").map(|v| (v["id"].as_str().unwrap(), v["ok"].as_bool().unwrap())).collect();
    // One result per command, each accepted, and none for the blank line.
    assert_eq!(results, vec![("l", true), ("p", true), ("a", true), ("q", true)], "{lines:?}");
    // Control: the commands did what they say, so they were parsed whole.
    let state = lines.iter().find(|v| v["type"] == "state").expect("the advance sent no state");
    assert_eq!(state["frame"], 4800);
    assert_eq!(state["decks"][0]["playing"], true);
}

#[test]
fn a_wall_engine_stops_when_nobody_reads_its_output_with_stdin_still_open() {
    // A supervisor killed outright never closes anything itself: the kernel
    // drops its handles. Here only the reader of stdout goes, so stdin's
    // end-of-file cannot be what stops the engine.
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "wall"])
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .unwrap();
    let stdin = child.stdin.take().unwrap();
    let mut out = BufReader::new(child.stdout.take().unwrap());
    let mut hello = String::new();
    out.read_line(&mut hello).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&hello).unwrap()["type"], "hello");
    // Control: the engine is up and stays up while its output is read.
    let mut line = String::new();
    out.read_line(&mut line).unwrap();
    assert_eq!(serde_json::from_str::<Value>(&line).unwrap()["type"], "state");
    assert!(child.try_wait().unwrap().is_none(), "the engine exited with its supervisor still there");
    drop(out);
    let status = exit_within(&mut child, Duration::from_secs(10), "after its stdout reader went away");
    let mut err = String::new();
    child.stderr.take().unwrap().read_to_string(&mut err).unwrap();
    assert!(!status.success(), "an engine that lost its output reported a clean exit");
    assert!(err.contains("the output closed, so the engine stopped"), "{err}");
    drop(stdin);
}
