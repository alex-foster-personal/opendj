//! Protocol v1 over a loopback WebSocket (plan 20-02).
//!
//! The same newline-delimited JSON as stdio, one or more lines per text
//! message. The renderer, agents and Python connect here as equal clients of
//! the one mailbox, so the 30 Hz state feed reaches the page without passing
//! through Python.
//!
//! Who may connect:
//!
//! - The listener binds a loopback address only; `bind` refuses anything else.
//! - A browser lets any page open a socket to 127.0.0.1, so loopback alone is
//!   not a boundary. Every client presents the token its supervisor handed the
//!   engine (`ODJ_AUDIO_WS_TOKEN`) as `?token=`; the supervisor publishes it
//!   only to same-origin callers of its own route.
//! - The handshake answers 404 for any path but `WS_PATH`, 401 for a missing or
//!   wrong token, and 503 once `MAX_WS_CLIENTS` are connected.
//!
//! Each connection runs on its own thread with a short read timeout, flushing
//! its queued outbound lines between reads. A client that stops reading fills
//! its queue and is dropped by the hub, so it never holds up the engine or the
//! other clients.

use std::cell::Cell;
use std::io;
use std::net::{IpAddr, SocketAddr, TcpListener, TcpStream};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Arc};
use std::time::Duration;

use serde_json::Value;
use tungstenite::handshake::server::{ErrorResponse, Request, Response};
use tungstenite::http::StatusCode;
use tungstenite::{Message, WebSocket};

use crate::engine::ErrorCode;
use crate::protocol::{self, ProtoError};
use crate::serve::{ClientId, Hub, CLIENT_QUEUE};

/// The one path the socket answers on.
pub const WS_PATH: &str = "/api/v1/ws";

/// Shortest token the engine accepts, in characters.
pub const MIN_TOKEN_LEN: usize = 32;

/// How long a connection may take to finish its handshake.
const HANDSHAKE_TIMEOUT: Duration = Duration::from_secs(5);
/// How long a connection waits for an inbound frame before flushing outbound
/// lines again. Bounds the extra latency on results and state.
const POLL: Duration = Duration::from_millis(2);
/// A write that cannot finish in this long means the client is gone.
const WRITE_TIMEOUT: Duration = Duration::from_secs(2);

/// Bind the listener. Port 0 picks a free port; the chosen one is on
/// `local_addr()` and in the stdout `hello`.
pub fn bind(addr: SocketAddr) -> io::Result<TcpListener> {
    if !addr.ip().is_loopback() {
        return Err(io::Error::new(
            io::ErrorKind::InvalidInput,
            format!("--ws binds loopback only; {} is not a loopback address", addr.ip()),
        ));
    }
    TcpListener::bind(addr)
}

/// The URL a client connects to, without its token.
pub fn url(addr: SocketAddr) -> String {
    match addr.ip() {
        IpAddr::V4(ip) => format!("ws://{ip}:{}{WS_PATH}", addr.port()),
        IpAddr::V6(ip) => format!("ws://[{ip}]:{}{WS_PATH}", addr.port()),
    }
}

/// Check a token for use: long enough to be unguessable.
pub fn check_token(token: &str) -> Result<(), String> {
    if token.chars().count() < MIN_TOKEN_LEN {
        return Err(format!("ODJ_AUDIO_WS_TOKEN must be at least {MIN_TOKEN_LEN} characters"));
    }
    Ok(())
}

/// Equal-time comparison, so a wrong guess learns nothing from timing.
fn same(a: &[u8], b: &[u8]) -> bool {
    if a.len() != b.len() {
        return false;
    }
    a.iter().zip(b).fold(0u8, |acc, (x, y)| acc | (x ^ y)) == 0
}

fn token_of(query: Option<&str>) -> Option<&str> {
    query?.split('&').find_map(|kv| kv.strip_prefix("token="))
}

fn refuse(code: StatusCode, why: &str) -> ErrorResponse {
    let mut r = ErrorResponse::new(Some(why.to_owned()));
    *r.status_mut() = code;
    r
}

/// Accept connections for the life of the process.
pub fn spawn_accept(
    listener: TcpListener,
    token: String,
    hub: Arc<Hub>,
    hello: Value,
    state_req: Arc<AtomicBool>,
    on_line: impl Fn(ClientId, String) + Send + Sync + 'static,
) -> io::Result<()> {
    let token: Arc<str> = token.into();
    let on_line = Arc::new(on_line);
    let hello: Arc<str> = hello.to_string().into();
    std::thread::Builder::new().name("odj-audio-ws".into()).spawn(move || {
        for stream in listener.incoming() {
            let Ok(stream) = stream else { continue };
            let (token, hub, hello, state_req, on_line) =
                (token.clone(), hub.clone(), hello.clone(), state_req.clone(), on_line.clone());
            let _ = std::thread::Builder::new().name("odj-audio-ws-client".into()).spawn(move || {
                connection(stream, &token, &hub, &hello, &state_req, &*on_line);
            });
        }
    })?;
    Ok(())
}

// The handshake callback's error type is tungstenite's own HTTP response.
#[allow(clippy::result_large_err)]
fn connection(
    stream: TcpStream,
    token: &str,
    hub: &Hub,
    hello: &str,
    state_req: &AtomicBool,
    on_line: &(impl Fn(ClientId, String) + ?Sized),
) {
    let _ = stream.set_nodelay(true);
    if stream.set_read_timeout(Some(HANDSHAKE_TIMEOUT)).is_err() || stream.set_write_timeout(Some(WRITE_TIMEOUT)).is_err() {
        return;
    }
    let (tx, rx) = mpsc::sync_channel::<String>(CLIENT_QUEUE);
    // Registered inside the handshake, so a refusal for a full house goes out
    // as a 503 before the upgrade rather than as a close after it.
    let client: Cell<Option<ClientId>> = Cell::new(None);
    let check = |req: &Request, resp: Response| -> Result<Response, ErrorResponse> {
        if req.uri().path() != WS_PATH {
            return Err(refuse(StatusCode::NOT_FOUND, "odj-audio answers on /api/v1/ws only"));
        }
        match token_of(req.uri().query()) {
            Some(t) if same(t.as_bytes(), token.as_bytes()) => {}
            _ => return Err(refuse(StatusCode::UNAUTHORIZED, "missing or wrong token")),
        }
        client.set(hub.add_socket(tx));
        if client.get().is_none() {
            return Err(refuse(StatusCode::SERVICE_UNAVAILABLE, "too many socket clients"));
        }
        Ok(resp)
    };
    let ws = tungstenite::accept_hdr(stream, check).ok();
    let (Some(mut ws), Some(id)) = (ws, client.get()) else {
        if let Some(id) = client.get() {
            hub.remove(id);
        }
        return;
    };
    if ws.get_ref().set_read_timeout(Some(POLL)).is_ok() && ws.send(Message::text(hello)).is_ok() {
        // A new client sees the engine's current state without waiting for
        // the next scheduled snapshot.
        state_req.store(true, Ordering::Relaxed);
        run(&mut ws, id, &rx, hub, on_line);
    }
    hub.remove(id);
    let _ = ws.close(None);
    let _ = ws.flush();
}

fn run(
    ws: &mut WebSocket<TcpStream>,
    id: ClientId,
    rx: &mpsc::Receiver<String>,
    hub: &Hub,
    on_line: &(impl Fn(ClientId, String) + ?Sized),
) {
    loop {
        loop {
            match rx.try_recv() {
                Ok(line) => {
                    if ws.write(Message::text(line)).is_err() {
                        return;
                    }
                }
                Err(mpsc::TryRecvError::Empty) => break,
                // The hub dropped this client for falling behind.
                Err(mpsc::TryRecvError::Disconnected) => return,
            }
        }
        if ws.flush().is_err() {
            return;
        }
        match ws.read() {
            Ok(Message::Text(text)) => {
                for line in text.as_str().lines().filter(|l| !l.trim().is_empty()) {
                    on_line(id, line.to_owned());
                }
            }
            Ok(Message::Binary(_)) => {
                let e = ProtoError::new(ErrorCode::Invalid, "protocol v1 is JSON text; binary frames are refused");
                hub.send_to(id, &protocol::result_json(None, &Err(e)));
            }
            Ok(Message::Close(_)) => return,
            // Pings are answered by tungstenite on the next flush.
            Ok(_) => {}
            Err(tungstenite::Error::Io(e))
                if matches!(e.kind(), io::ErrorKind::WouldBlock | io::ErrorKind::TimedOut) => {}
            Err(_) => return,
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn binds_loopback_only() {
        assert!(bind("0.0.0.0:0".parse().unwrap()).is_err());
        let l = bind("127.0.0.1:0".parse().unwrap()).unwrap();
        let a = l.local_addr().unwrap();
        assert_eq!(url(a), format!("ws://127.0.0.1:{}/api/v1/ws", a.port()));
    }

    #[test]
    fn tokens_compare_whole() {
        assert_eq!(token_of(Some("a=1&token=abc")), Some("abc"));
        assert_eq!(token_of(Some("tokens=abc")), None);
        assert_eq!(token_of(None), None);
        assert!(same(b"abc", b"abc"));
        assert!(!same(b"abc", b"abd"));
        assert!(!same(b"abc", b"ab"));
        assert!(check_token("short").is_err());
        assert!(check_token(&"x".repeat(MIN_TOKEN_LEN)).is_ok());
    }
}
