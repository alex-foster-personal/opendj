//! Resolve the packaged engine origin from ``<data_dir>/.engine.lock`` and probe health.
//!
//! Mirrors the read-only contract in ``apps/shared/engine_origin.py``: the port
//! is never guessed; a missing or unusable lock refuses and names the path.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpStream};
use std::path::Path;
use std::thread;
use std::time::Duration;

use serde::{Deserialize, Serialize};

pub const EXPECTED_ENGINE_ROLE: &str = "opendj-engine";
pub const HEALTH_PATH: &str = "/api/v1/health";

const LOCK_READ_ATTEMPTS: u32 = 5;
const LOCK_READ_RETRY_MS: u64 = 10;
const SOCKET_TIMEOUT: Duration = Duration::from_secs(3);
const MAX_RESPONSE_BYTES: usize = 256 * 1024;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct EngineOrigin {
    pub host: String,
    pub port: u16,
    pub lock_path: String,
    pub base_url: String,
}

#[derive(Debug, Default)]
struct LockJson {
    pid: Option<u32>,
    host: Option<String>,
    port: Option<u16>,
    role: Option<String>,
    boot_id: Option<String>,
}

fn try_parse_lock_json(raw: &[u8]) -> Option<LockJson> {
    let trimmed = raw
        .iter()
        .position(|byte| !byte.is_ascii_whitespace())
        .map(|start| &raw[start..])
        .unwrap_or(&[]);
    if trimmed.is_empty() {
        return None;
    }
    let value = serde_json::from_slice::<serde_json::Value>(trimmed).ok()?;
    let obj = value.as_object()?;
    Some(LockJson {
        pid: obj.get("pid").and_then(|v| v.as_u64()).map(|v| v as u32),
        host: obj
            .get("host")
            .and_then(|v| v.as_str())
            .map(|v| v.to_string()),
        port: obj.get("port").and_then(|v| v.as_u64()).map(|v| v as u16),
        role: obj
            .get("role")
            .and_then(|v| v.as_str())
            .map(|v| v.to_string()),
        boot_id: obj
            .get("boot_id")
            .and_then(|v| v.as_str())
            .map(|v| v.to_string()),
    })
}

fn read_lock_json_retrying(lock_path: &Path) -> Result<LockJson, String> {
    if !lock_path.exists() {
        return Err(format!(
            "no engine lock file at {}; the engine is not running",
            lock_path.display()
        ));
    }
    let last_error = "empty or not a JSON object";
    for attempt in 0..LOCK_READ_ATTEMPTS {
        let raw = std::fs::read(lock_path).map_err(|e| {
            format!(
                "engine lock file {} could not be read: {e}",
                lock_path.display()
            )
        })?;
        if let Some(holder) = try_parse_lock_json(&raw) {
            return Ok(holder);
        }
        if attempt + 1 < LOCK_READ_ATTEMPTS {
            thread::sleep(Duration::from_millis(LOCK_READ_RETRY_MS));
        }
    }
    Err(format!(
        "engine lock file {} is not valid JSON, so its port cannot be trusted: {last_error}",
        lock_path.display()
    ))
}

fn lock_port(payload: &LockJson, lock_path: &Path) -> Result<u16, String> {
    let port = payload.port.ok_or_else(|| {
        format!(
            "engine lock file {} carries no usable port; the engine is still starting, or the file is a leftover",
            lock_path.display()
        )
    })?;
    if port == 0 {
        return Err(format!(
            "engine lock file {} carries no usable port; the engine is still starting, or the file is a leftover",
            lock_path.display()
        ));
    }
    Ok(port)
}

/// Read the lock into an origin without probing health (for tests that need the document only).
fn lock_host(payload: &LockJson) -> String {
    payload
        .host
        .as_deref()
        .filter(|h| !h.is_empty())
        .unwrap_or("127.0.0.1")
        .to_string()
}

pub fn read_origin_from_lock(lock_path: &Path) -> Result<EngineOrigin, String> {
    let payload = read_lock_json_retrying(lock_path)?;
    let host = lock_host(&payload);
    let port = lock_port(&payload, lock_path)?;
    Ok(EngineOrigin {
        base_url: format!("http://{host}:{port}"),
        host,
        port,
        lock_path: lock_path.display().to_string(),
    })
}

pub fn resolve_engine_origin_impl(lock_path: &Path) -> Result<EngineOrigin, String> {
    let payload = read_lock_json_retrying(lock_path)?;
    let role = payload.role.as_deref().unwrap_or("(missing)");
    if role != EXPECTED_ENGINE_ROLE {
        return Err(format!(
            "engine lock file {} names port {} with role={role}, but expected role={EXPECTED_ENGINE_ROLE}",
            lock_path.display(),
            payload.port.unwrap_or(0)
        ));
    }
    let host = lock_host(&payload);
    let port = lock_port(&payload, lock_path)?;
    let status = loopback_get_status(&host, port, HEALTH_PATH)?;
    let base_url = format!("http://{host}:{port}");
    if status != 200 {
        return Err(format!(
            "engine lock file {} names {base_url}, but {HEALTH_PATH} returned status {status}",
            lock_path.display()
        ));
    }
    Ok(EngineOrigin {
        host,
        port,
        lock_path: lock_path.display().to_string(),
        base_url,
    })
}

#[tauri::command]
pub fn resolve_engine_origin() -> Result<EngineOrigin, String> {
    let lock = crate::state::engine_lock_path()?;
    resolve_engine_origin_impl(&lock)
}

/// Loopback GET returning HTTP status and body (stdlib only, no HTTP crate).
pub(crate) fn loopback_get(
    host: &str,
    port: u16,
    path: &str,
) -> Result<(u16, String), String> {
    let addr: SocketAddr = format!("{host}:{port}")
        .parse()
        .map_err(|e| format!("invalid engine address {host}:{port}: {e}"))?;
    let mut stream = TcpStream::connect_timeout(&addr, SOCKET_TIMEOUT)
        .map_err(|e| format!("engine at {host}:{port} did not accept a connection: {e}"))?;
    let _ = stream.set_nodelay(true);
    let _ = stream.set_read_timeout(Some(SOCKET_TIMEOUT));
    let _ = stream.set_write_timeout(Some(SOCKET_TIMEOUT));
    let request = format!(
        "GET {path} HTTP/1.1\r\nHost: {host}:{port}\r\nConnection: close\r\nAccept: application/json\r\n\r\n"
    );
    stream
        .write_all(request.as_bytes())
        .map_err(|e| format!("engine write failed: {e}"))?;
    let mut response = Vec::new();
    let mut buf = [0_u8; 4096];
    loop {
        if response.len() >= MAX_RESPONSE_BYTES {
            break;
        }
        match stream.read(&mut buf) {
            Ok(0) => break,
            Ok(n) => response.extend_from_slice(&buf[..n]),
            Err(e) => return Err(format!("engine read failed: {e}")),
        }
    }
    let text = String::from_utf8_lossy(&response);
    let status = parse_status_line(&text)
        .ok_or_else(|| format!("engine at {host}:{port} returned no HTTP status line"))?;
    let body = text
        .split_once("\r\n\r\n")
        .map(|(_, b)| b.to_string())
        .unwrap_or_default();
    Ok((status, body))
}

pub(crate) fn loopback_get_status(host: &str, port: u16, path: &str) -> Result<u16, String> {
    loopback_get(host, port, path).map(|(status, _)| status)
}

fn parse_status_line(response: &str) -> Option<u16> {
    let line = response.lines().next()?;
    let mut parts = line.split_whitespace();
    let _version = parts.next()?;
    parts.next()?.parse().ok()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::path::PathBuf;
    use std::process::{Child, Command, Stdio};
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::Mutex;

    static SPAWN_GUARD: Mutex<()> = Mutex::new(());
    static SPAWN_SEQ: AtomicUsize = AtomicUsize::new(0);

    struct DisposableEngine {
        child: Child,
        pub lock_path: PathBuf,
        pub port: u16,
        pub smartlist_id: Option<String>,
    }

    impl Drop for DisposableEngine {
        fn drop(&mut self) {
            let _ = self.child.kill();
            let _ = self.child.wait();
        }
    }

    fn repo_root() -> PathBuf {
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .and_then(|p| p.parent())
            .and_then(|p| p.parent())
            .map(PathBuf::from)
            .unwrap_or_else(|| PathBuf::from("."))
    }

    fn spawn_disposable(mode: &str) -> DisposableEngine {
        let _guard = SPAWN_GUARD.lock().unwrap_or_else(|e| e.into_inner());
        let seq = SPAWN_SEQ.fetch_add(1, Ordering::Relaxed);
        let data_dir = std::env::temp_dir().join(format!(
            "launcher-engine-test-{}-{}-{}",
            std::process::id(),
            seq,
            mode
        ));
        let _ = fs::remove_dir_all(&data_dir);
        fs::create_dir_all(&data_dir).expect("temp data dir");
        let root = repo_root();
        let mut child = Command::new("uv")
            .current_dir(&root)
            .env("PYTHONPATH", &root)
            .args([
                "run",
                "python",
                "tests/launcher/disposable_engine_server.py",
                "--data-dir",
            ])
            .arg(&data_dir)
            .args(["--mode", mode])
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .spawn()
            .expect("spawn disposable engine");
        use std::io::BufRead;
        let mut line = String::new();
        {
            let mut stdout = child.stdout.take().expect("stdout");
            let mut reader = std::io::BufReader::new(&mut stdout);
            reader
                .read_line(&mut line)
                .expect("read readiness json");
        }
        let doc: serde_json::Value = serde_json::from_str(line.trim()).expect("readiness json");
        let port = doc["port"].as_u64().expect("port") as u16;
        for _ in 0..100 {
            if loopback_get_status("127.0.0.1", port, HEALTH_PATH).unwrap_or(0) == 200 {
                break;
            }
            thread::sleep(Duration::from_millis(50));
        }
        DisposableEngine {
            port,
            lock_path: PathBuf::from(doc["lock_path"].as_str().expect("lock_path")),
            smartlist_id: doc
                .get("smartlist_id")
                .and_then(|v| v.as_str())
                .map(str::to_string),
            child,
        }
    }

    #[test]
    fn missing_lock_refuses_and_names_path() {
        let missing = std::env::temp_dir().join(format!(
            "launcher-missing-lock-{}.lock",
            std::process::id()
        ));
        let _ = fs::remove_file(&missing);
        let err = resolve_engine_origin_impl(&missing).unwrap_err();
        assert!(err.contains(&missing.display().to_string()));
        assert!(!err.contains(":8686"));
        assert!(!err.contains("http://"));
    }

    #[test]
    fn unusable_lock_json_refuses_and_names_path() {
        let lock = std::env::temp_dir().join(format!(
            "launcher-bad-lock-{}.lock",
            std::process::id()
        ));
        fs::write(&lock, "not-json").expect("write lock");
        let err = resolve_engine_origin_impl(&lock).unwrap_err();
        assert!(err.contains(&lock.display().to_string()));
        let _ = fs::remove_file(&lock);
    }

    #[test]
    fn resolve_engine_origin_after_health_probe() {
        let engine = spawn_disposable("with-db");
        let origin = resolve_engine_origin_impl(&engine.lock_path).expect("resolve");
        assert_eq!(origin.port, engine.port);
        assert_eq!(origin.host, "127.0.0.1");
        assert!(origin.base_url.contains(&engine.port.to_string()));
    }

    #[test]
    fn list_smartlists_uses_resolved_origin() {
        let engine = spawn_disposable("with-db");
        let origin = resolve_engine_origin_impl(&engine.lock_path).expect("resolve");
        let rows = super::super::smartlists::list_smartlists_impl(&origin).expect("list");
        let sid = engine.smartlist_id.as_deref().expect("smartlist id");
        assert!(rows.iter().any(|r| r.id == sid));
    }

    #[test]
    fn smartlist_tracks_returns_live_payload() {
        let engine = spawn_disposable("with-db");
        let origin = resolve_engine_origin_impl(&engine.lock_path).expect("resolve");
        let sid = engine.smartlist_id.as_deref().expect("smartlist id");
        let tracks =
            super::super::smartlists::smartlist_tracks_impl(&origin, sid).expect("tracks");
        assert_eq!(tracks.smartlist_id, sid);
        assert!(!tracks.items.is_empty());
    }

    #[test]
    fn smartlists_db_unavailable_surfaces_code() {
        let engine = spawn_disposable("no-db");
        let origin = resolve_engine_origin_impl(&engine.lock_path).expect("resolve");
        let err = super::super::smartlists::list_smartlists_impl(&origin).unwrap_err();
        assert!(err.contains("SMARTLISTS_DB_UNAVAILABLE"));
    }
}
