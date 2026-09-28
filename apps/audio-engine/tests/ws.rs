//! The loopback WebSocket (plan 20-02) as the renderer and a supervisor see it:
//! a wall-clock engine with stdio and two sockets as equal mailbox clients.

use std::io::{BufRead, BufReader, Write};
use std::net::TcpStream;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc;
use std::time::{Duration, Instant};

use serde_json::{json, Value};
use tungstenite::stream::MaybeTlsStream;
use tungstenite::{Message, WebSocket};

const BIN: &str = env!("CARGO_BIN_EXE_odj-audio");
const TOKEN: &str = "test-token-0123456789abcdef0123456789";

type Socket = WebSocket<MaybeTlsStream<TcpStream>>;

struct Engine {
    child: Child,
    stdin: Option<ChildStdin>,
    stdout: mpsc::Receiver<Value>,
    url: String,
}

fn start() -> Engine {
    let mut child = Command::new(BIN)
        .args(["serve", "--clock", "wall", "--ws", "127.0.0.1:0"])
        .env("ODJ_AUDIO_WS_TOKEN", TOKEN)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .spawn()
        .unwrap();
    let stdin = child.stdin.take();
    let (tx, rx) = mpsc::channel();
    let out = child.stdout.take().unwrap();
    std::thread::spawn(move || {
        for line in BufReader::new(out).lines() {
            let Ok(line) = line else { return };
            if tx.send(serde_json::from_str::<Value>(&line).unwrap()).is_err() {
                return;
            }
        }
    });
    let hello = rx.recv_timeout(Duration::from_secs(10)).unwrap();
    assert_eq!(hello["type"], "hello");
    let url = hello["ws"].as_str().expect("stdout hello names the socket").to_owned();
    assert!(url.starts_with("ws://127.0.0.1:") && url.ends_with("/api/v1/ws"), "{url}");
    Engine { child, stdin, stdout: rx, url }
}

fn connect(url: &str) -> Socket {
    let (mut ws, _) = tungstenite::connect(format!("{url}?token={TOKEN}")).unwrap();
    if let MaybeTlsStream::Plain(s) = ws.get_mut() {
        s.set_read_timeout(Some(Duration::from_secs(10))).unwrap();
    }
    ws
}

fn next(ws: &mut Socket) -> Value {
    loop {
        match ws.read().unwrap() {
            Message::Text(t) => return serde_json::from_str(t.as_str()).unwrap(),
            _ => continue,
        }
    }
}

fn say(ws: &mut Socket, v: Value) {
    ws.send(Message::text(v.to_string())).unwrap();
}

/// Read until `want` matches, failing after `limit` messages.
fn until(ws: &mut Socket, limit: usize, mut want: impl FnMut(&Value) -> bool) -> Vec<Value> {
    let mut seen = Vec::new();
    for _ in 0..limit {
        let v = next(ws);
        let hit = want(&v);
        seen.push(v);
        if hit {
            return seen;
        }
    }
    panic!("no match in {limit} messages: {seen:?}");
}

fn refused_with(url: &str, status: u16) {
    match tungstenite::connect(url) {
        Err(tungstenite::Error::Http(r)) => assert_eq!(r.status().as_u16(), status, "{url}"),
        Err(e) => panic!("{url}: expected HTTP {status}, got {e}"),
        Ok(_) => panic!("{url}: expected HTTP {status}, got a connection"),
    }
}

#[test]
fn sockets_and_stdio_share_one_mailbox() {
    let mut e = start();

    // Who may connect: the token and the path are both checked.
    refused_with(&e.url, 401);
    refused_with(&format!("{}?token=wrong", e.url), 401);
    refused_with(&format!("{}?token={}x", e.url, TOKEN), 401);
    refused_with(&format!("{}?token={TOKEN}", e.url.replace("/api/v1/ws", "/other")), 404);

    let mut a = connect(&e.url);
    let hello = next(&mut a);
    assert_eq!(hello["type"], "hello");
    assert_eq!(hello["protocol"], 1);
    assert_eq!(hello["clock"], "wall");
    assert!(hello.get("ws").is_none(), "only the supervisor needs the URL");
    // A new client is sent state straight away.
    until(&mut a, 5, |v| v["type"] == "state");

    let mut b = connect(&e.url);
    assert_eq!(next(&mut b)["type"], "hello");

    // A command from socket A: its result goes to A only, and every client,
    // stdio included, sees the state it produced.
    say(&mut a, json!({"id": "a1", "cmd": {"type": "crossfader", "value": 0.25}}));
    let seen = until(&mut a, 100, |v| v["type"] == "result" && v["id"] == "a1");
    assert_eq!(seen.last().unwrap()["ok"], true);
    until(&mut b, 100, |v| v["type"] == "state" && v["mixer"]["crossfader"] == 0.25);
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        let v = e.stdout.recv_timeout(deadline - Instant::now()).unwrap();
        assert!(!(v["type"] == "result" && v["id"] == "a1"), "stdio got socket A's result");
        if v["type"] == "state" && v["mixer"]["crossfader"] == 0.25 {
            break;
        }
    }

    // A command on stdio answers on stdio, never on a socket.
    let stdin = e.stdin.as_mut().unwrap();
    writeln!(stdin, "{}", json!({"id": "s1", "cmd": {"type": "master_volume", "value": 0.5}})).unwrap();
    loop {
        let v = e.stdout.recv_timeout(Duration::from_secs(10)).unwrap();
        if v["type"] == "result" && v["id"] == "s1" {
            assert_eq!(v["ok"], true);
            break;
        }
    }
    let seen = until(&mut b, 100, |v| v["type"] == "state" && v["mixer"]["master_volume"] == 0.5);
    assert!(seen.iter().all(|v| v["id"] != "s1" && v["id"] != "a1"), "socket B got another client's result");

    // Refusals name their reason over the socket exactly as on stdio.
    say(&mut b, json!({"id": "b1", "cmd": {"type": "stem_mute", "deck": 1, "stem": "vocals", "muted": true}}));
    let r = until(&mut b, 100, |v| v["id"] == "b1").pop().unwrap();
    assert_eq!(r["error"]["code"], "not_implemented");
    // Several lines in one frame are several commands.
    let two = format!(
        "{}\n{}",
        json!({"id": "b2", "cmd": {"type": "crossfader", "value": 0.75}}),
        json!({"id": "b3", "cmd": {"type": "crossfader", "value": 2}})
    );
    b.send(Message::text(two)).unwrap();
    // Results can come back out of order: a line that fails to parse is
    // answered at once, a valid one after the audio thread applies it.
    let mut got = 0;
    let seen = until(&mut b, 100, |v| {
        got += (v["id"] == "b2" || v["id"] == "b3") as u32;
        got == 2
    });
    assert!(seen.iter().any(|v| v["id"] == "b2" && v["ok"] == true));
    assert!(seen.iter().any(|v| v["id"] == "b3" && v["error"]["code"] == "invalid"));
    b.send(Message::binary(vec![1u8, 2, 3])).unwrap();
    let r = until(&mut b, 100, |v| v["type"] == "result" && v["ok"] == false).pop().unwrap();
    assert_eq!(r["error"]["code"], "invalid");

    // Only the supervisor may stop the engine.
    say(&mut a, json!({"id": "a2", "cmd": {"type": "engine_shutdown"}}));
    let r = until(&mut a, 100, |v| v["id"] == "a2").pop().unwrap();
    assert_eq!(r["error"]["code"], "unsupported");

    // A socket client leaving changes nothing for the others.
    drop(a);
    say(&mut b, json!({"id": "b4", "cmd": {"type": "engine_state"}}));
    assert_eq!(until(&mut b, 100, |v| v["id"] == "b4").pop().unwrap()["ok"], true);

    // Stdin EOF: the engine exits, sockets and all.
    drop(e.stdin.take());
    let deadline = Instant::now() + Duration::from_secs(10);
    loop {
        if let Some(status) = e.child.try_wait().unwrap() {
            assert!(status.success());
            break;
        }
        assert!(Instant::now() < deadline, "engine outlived its supervisor");
        std::thread::sleep(Duration::from_millis(20));
    }
}

fn serve_fails(args: &[&str], token: Option<&str>, needle: &str) {
    let mut c = Command::new(BIN);
    c.args(args).env_remove("ODJ_AUDIO_WS_TOKEN");
    if let Some(t) = token {
        c.env("ODJ_AUDIO_WS_TOKEN", t);
    }
    let o = c.stdin(Stdio::null()).output().unwrap();
    assert!(!o.status.success(), "{args:?} should fail");
    let err = String::from_utf8_lossy(&o.stderr);
    assert!(err.contains(needle), "{args:?}: {err}");
}

#[test]
fn the_socket_refuses_to_start_unsafely() {
    serve_fails(&["serve", "--clock", "wall", "--ws", "127.0.0.1:0"], None, "ODJ_AUDIO_WS_TOKEN");
    serve_fails(&["serve", "--clock", "wall", "--ws", "127.0.0.1:0"], Some("short"), "at least 32");
    serve_fails(&["serve", "--clock", "wall", "--ws", "0.0.0.0:0"], Some(TOKEN), "loopback only");
    serve_fails(&["serve", "--clock", "fake", "--ws", "127.0.0.1:0"], Some(TOKEN), "wall and device");
    serve_fails(&["serve", "--clock", "wall", "--ws", "localhost"], Some(TOKEN), "HOST:PORT");
}
