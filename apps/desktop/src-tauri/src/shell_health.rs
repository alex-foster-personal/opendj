//! Loopback health surface owned by the desktop shell, not the engine.
//!
//! Agents and the bootstrap page consult `.engine.shell.json` for the port,
//! then `GET /api/v1/health` here when the engine port is dead or stale.

use std::io::{Read, Write};
use std::net::{TcpListener, TcpStream};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;
use std::time::{Duration, SystemTime, UNIX_EPOCH};

const HEALTH_PATH: &str = "/api/v1/health";
const RELAUNCH_PATH: &str = "/api/v1/relaunch";
const OUTPUT_HEALTH_PATH: &str = crate::output_health::OUTPUT_HEALTH_PATH;
const SWITCH_OUTPUT_PATH: &str = crate::output_health::SWITCH_OUTPUT_PATH;
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
        thread::spawn({
            let snapshot = Arc::clone(&snapshot);
            let relaunch_requested = Arc::clone(&relaunch_requested);
            move || serve_loop(listener, snapshot, relaunch_requested)
        });
        Ok(Self {
            port,
            snapshot,
            relaunch_requested,
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
) {
    while let Ok(stream) = accept_one(&listener) {
        let _ = handle_connection(stream, &snapshot, &relaunch_requested);
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

/// UTC RFC3339 with millisecond precision and a `+00:00` suffix.
pub fn utc_timestamp_iso() -> String {
    let duration = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch");
    let secs = duration.as_secs();
    let millis = duration.subsec_millis();
    let (year, month, day, hour, minute, second) = unix_secs_to_utc(secs);
    format!(
        "{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}+00:00"
    )
}

fn unix_secs_to_utc(secs: u64) -> (u32, u32, u32, u32, u32, u32) {
    let days = secs / 86_400;
    let time = secs % 86_400;
    let hour = (time / 3_600) as u32;
    let minute = ((time % 3_600) / 60) as u32;
    let second = (time % 60) as u32;
    let (year, month, day) = civil_from_days(days as i64);
    (year, month, day, hour, minute, second)
}

fn civil_from_days(days: i64) -> (u32, u32, u32) {
    let z = days + 719_468;
    let era = if z >= 0 { z } else { z - 146_096 } / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = mp + if mp < 10 { 3 } else { -9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year as u32, month as u32, day as u32)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpStream;
    use std::time::Duration;

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

    #[test]
    fn utc_timestamp_ends_with_plus_zero_offset() {
        let stamp = utc_timestamp_iso();
        assert!(stamp.ends_with("+00:00"), "{stamp}");
        assert!(stamp.contains('T'), "{stamp}");
    }
}
