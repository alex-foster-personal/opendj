//! Loopback health surface owned by the desktop shell, not the engine.
//!
//! Agents and the bootstrap page consult `.engine.shell.json` for the port,
//! then `GET /api/v1/health` here when the engine port is dead or stale.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::Duration;

use crate::http_paths::HEALTH_PATH;
pub use odj_output_probe::clock::utc_timestamp_iso;

const RELAUNCH_PATH: &str = "/api/v1/relaunch";
const RESET_BUNDLED_PAYLOAD_PATH: &str = "/api/v1/reset-bundled-payload";
const OUTPUT_HEALTH_PATH: &str = crate::output_health::OUTPUT_HEALTH_PATH;
const SWITCH_OUTPUT_PATH: &str = crate::output_health::SWITCH_OUTPUT_PATH;
const RECONNECT_SPEAKER_PATH: &str = crate::speaker_link::RECONNECT_SPEAKER_PATH;
const INPUT_DEVICES_PATH: &str = crate::speaker_link::INPUT_DEVICES_PATH;
const SHELL_LOCK_FILE: &str = ".engine.shell.json";
const READ_TIMEOUT: Duration = Duration::from_millis(750);

/// Serializable health snapshot the supervisor thread updates.
#[derive(Debug, Clone)]
pub struct ShellHealthSnapshot {
    pub status: String,
    pub engine: String,
    pub lock_pid: Option<u32>,
    pub lock_port: Option<u16>,
    pub exit_code: Option<i32>,
    pub reason: Option<String>,
}

impl Default for ShellHealthSnapshot {
    fn default() -> Self {
        Self {
            status: "ok".into(),
            engine: "running".into(),
            lock_pid: None,
            lock_port: None,
            exit_code: None,
            reason: None,
        }
    }
}

pub struct ShellHealthServer {
    port: u16,
    snapshot: Arc<Mutex<ShellHealthSnapshot>>,
    relaunch_requested: Arc<Mutex<bool>>,
    reset_bundled_payload_requested: Arc<Mutex<bool>>,
    data_dir: PathBuf,
}

impl ShellHealthServer {
    pub fn start(data_dir: &Path) -> Result<Self, String> {
        let listener = TcpListener::bind("127.0.0.1:0").map_err(|err| {
            format!("binding shell health listener failed: {err}")
        })?;
        let port = listener
            .local_addr()
            .map_err(|err| format!("reading shell health port failed: {err}"))?
            .port();
        listener
            .set_nonblocking(true)
            .map_err(|err| format!("shell health listener nonblocking failed: {err}"))?;
        write_shell_json(data_dir, port)?;
        let snapshot = Arc::new(Mutex::new(ShellHealthSnapshot::default()));
        let relaunch_requested = Arc::new(Mutex::new(false));
        let reset_bundled_payload_requested = Arc::new(Mutex::new(false));
        let data_dir = data_dir.to_path_buf();
        thread::spawn({
            let snapshot = Arc::clone(&snapshot);
            let relaunch_requested = Arc::clone(&relaunch_requested);
            let reset_bundled_payload_requested = Arc::clone(&reset_bundled_payload_requested);
            let data_dir = data_dir.clone();
            move || {
                serve_loop(
                    listener,
                    snapshot,
                    relaunch_requested,
                    reset_bundled_payload_requested,
                    data_dir,
                )
            }
        });
        Ok(Self {
            port,
            snapshot,
            relaunch_requested,
            reset_bundled_payload_requested,
            data_dir,
        })
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn update(&self, snapshot: ShellHealthSnapshot) {
        *self
            .snapshot
            .lock()
            .expect("shell health snapshot mutex") = snapshot;
    }

    pub fn take_relaunch_request(&self) -> bool {
        let mut slot = self
            .relaunch_requested
            .lock()
            .expect("shell relaunch mutex");
        if *slot {
            *slot = false;
            true
        } else {
            false
        }
    }

    pub fn take_reset_bundled_payload_request(&self) -> bool {
        let mut slot = self
            .reset_bundled_payload_requested
            .lock()
            .expect("shell reset mutex");
        if *slot {
            *slot = false;
            true
        } else {
            false
        }
    }
}

fn write_shell_json(data_dir: &Path, port: u16) -> Result<(), String> {
    let path = data_dir.join(SHELL_LOCK_FILE);
    let body = serde_json::json!({
        "health_port": port,
        "shell_pid": std::process::id(),
    });
    std::fs::write(&path, format!("{}\n", body)).map_err(|err| {
        format!("writing {} failed: {err}", path.display())
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).map_err(
            |err| format!("chmod {} failed: {err}", path.display()),
        )?;
    }
    Ok(())
}

fn serve_loop(
    listener: TcpListener,
    snapshot: Arc<Mutex<ShellHealthSnapshot>>,
    relaunch_requested: Arc<Mutex<bool>>,
    reset_bundled_payload_requested: Arc<Mutex<bool>>,
    data_dir: PathBuf,
) {
    while let Ok(stream) = accept_one(&listener) {
        let _ = handle_connection(
            stream,
            &snapshot,
            &relaunch_requested,
            &reset_bundled_payload_requested,
            &data_dir,
        );
    }
}

fn accept_one(listener: &TcpListener) -> Result<TcpStream, ()> {
    loop {
        match listener.accept() {
            Ok((stream, _)) => return Ok(stream),
            Err(err) if err.kind() == std::io::ErrorKind::WouldBlock => {
                thread::sleep(Duration::from_millis(25));
            }
            Err(_) => return Err(()),
        }
    }
}

fn handle_connection(
    mut stream: TcpStream,
    snapshot: &Arc<Mutex<ShellHealthSnapshot>>,
    relaunch_requested: &Arc<Mutex<bool>>,
    reset_bundled_payload_requested: &Arc<Mutex<bool>>,
    data_dir: &Path,
) -> std::io::Result<()> {
    let _ = stream.set_read_timeout(Some(READ_TIMEOUT));
    let _ = stream.set_write_timeout(Some(READ_TIMEOUT));
    let request = read_request_line(&mut stream)?;
    let method = request.split_whitespace().next().unwrap_or("");
    let path = request.split_whitespace().nth(1).unwrap_or("");
    if method == "POST" && path == RELAUNCH_PATH {
        *relaunch_requested
            .lock()
            .expect("shell relaunch mutex") = true;
        return write_json_response(&mut stream, 202, r#"{"status":"accepted"}"#);
    }
    if method == "POST" && path == RESET_BUNDLED_PAYLOAD_PATH {
        match crate::engine_dev_override::clear_configured_engine_source(data_dir) {
            Ok(()) => {
                *reset_bundled_payload_requested
                    .lock()
                    .expect("shell reset mutex") = true;
                return write_json_response(&mut stream, 202, r#"{"status":"accepted"}"#);
            }
            Err(err) => {
                let body = serde_json::json!({ "status": "error", "detail": err });
                return write_json_response(
                    &mut stream,
                    500,
                    &serde_json::to_string(&body).unwrap_or_else(|_| "{}".into()),
                );
            }
        }
    }
    if method == "GET" && path == HEALTH_PATH {
        let snap = snapshot.lock().expect("shell health snapshot mutex").clone();
        let body = serde_json::json!({
            "status": snap.status,
            "engine": snap.engine,
            "lock_pid": snap.lock_pid,
            "lock_port": snap.lock_port,
            "exit_code": snap.exit_code,
            "reason": snap.reason,
            "checked_at": utc_timestamp_iso(),
        });
        return write_json_response(
            &mut stream,
            200,
            &serde_json::to_string(&body).unwrap_or_else(|_| "{}".into()),
        );
    }
    if method == "GET" && path == OUTPUT_HEALTH_PATH {
        return write_json_response(
            &mut stream,
            200,
            &crate::output_health::probe_output_health_json(),
        );
    }
    if method == "POST" && path == SWITCH_OUTPUT_PATH {
        return write_json_response(
            &mut stream,
            200,
            &crate::output_health::switch_output_json(),
        );
    }
    if method == "GET" && path == INPUT_DEVICES_PATH {
        return write_json_response(&mut stream, 200, &crate::speaker_link::input_devices_json());
    }
    if method == "POST" && path == RECONNECT_SPEAKER_PATH {
        return write_json_response(&mut stream, 200, &crate::speaker_link::reconnect_speaker_json());
    }
    write_json_response(&mut stream, 404, r#"{"status":"not_found"}"#)
}

fn read_request_line(stream: &mut TcpStream) -> std::io::Result<String> {
    let mut buffer = [0_u8; 4096];
    let mut total = 0usize;
    while total < buffer.len() {
        let read = stream.read(&mut buffer[total..])?;
        if read == 0 {
            break;
        }
        total += read;
        if buffer[..total].windows(4).any(|window| window == b"\r\n\r\n") {
            break;
        }
    }
    let head = String::from_utf8_lossy(&buffer[..total]);
    Ok(head.lines().next().unwrap_or("").to_string())
}

fn write_json_response(stream: &mut TcpStream, status: u16, body: &str) -> std::io::Result<()> {
    let status_text = match status {
        200 => "200 OK",
        202 => "202 Accepted",
        _ => "404 Not Found",
    };
    let response = format!(
        "HTTP/1.1 {status_text}\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{}",
        body.len(),
        body
    );
    stream.write_all(response.as_bytes())?;
    stream.flush()?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::time::Duration;

    #[test]
    fn reset_bundled_payload_clears_override_and_sets_request() {
        let dir = std::env::temp_dir().join(format!("shell-health-reset-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("mkdir");
        let override_body = r#"{"engine_source_directory":"/gone/wt"}"#;
        std::fs::write(
            dir.join(crate::engine_dev_override::OVERRIDE_FILE),
            override_body,
        )
        .expect("write override");
        let server = ShellHealthServer::start(&dir).expect("start shell health");
        let address = format!("127.0.0.1:{}", server.port());
        let mut stream = TcpStream::connect_timeout(
            &address.parse().expect("parse addr"),
            Duration::from_secs(2),
        )
        .expect("connect");
        stream
            .write_all(
                format!(
                    "POST {RESET_BUNDLED_PAYLOAD_PATH} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                )
                .as_bytes(),
            )
            .expect("write");
        let mut response = String::new();
        stream
            .read_to_string(&mut response)
            .expect("read response");
        assert!(response.contains("202"), "{response}");
        assert!(
            !dir
                .join(crate::engine_dev_override::OVERRIDE_FILE)
                .is_file(),
            "override file must be removed"
        );
        assert!(server.take_reset_bundled_payload_request());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn health_endpoint_reports_engine_dead() {
        let dir = std::env::temp_dir().join(format!("shell-health-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("mkdir");
        let server = ShellHealthServer::start(&dir).expect("start shell health");
        server.update(ShellHealthSnapshot {
            status: "dead".into(),
            engine: "dead".into(),
            lock_pid: Some(24600),
            lock_port: Some(58583),
            exit_code: Some(9),
            reason: None,
        });
        let address = format!("127.0.0.1:{}", server.port());
        let mut stream = TcpStream::connect_timeout(
            &address.parse().expect("parse addr"),
            Duration::from_secs(2),
        )
        .expect("connect");
        stream
            .write_all(
                format!(
                    "GET {HEALTH_PATH} HTTP/1.1\r\nHost: {address}\r\nConnection: close\r\n\r\n"
                )
                .as_bytes(),
            )
            .expect("write");
        let mut response = String::new();
        stream
            .read_to_string(&mut response)
            .expect("read response");
        assert!(response.contains(r#""engine":"dead""#), "{response}");
        assert!(response.contains(r#""status":"dead""#), "{response}");
        let _ = std::fs::remove_dir_all(&dir);
    }
}
