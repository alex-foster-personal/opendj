//! Inspect `.engine.lock` before spawn and decide adopt vs spawn vs dialog.

use std::fs::OpenOptions;
use std::io::{Read, Seek, SeekFrom};
use std::os::unix::io::AsRawFd;
use std::path::Path;

use crate::engine;

const LOCK_FILE_NAME: &str = ".engine.lock";

#[derive(Debug, PartialEq, Eq)]
pub enum LaunchPlan {
    Spawn,
    Adopt {
        pid: u32,
        host: String,
        port: u16,
    },
    StopOrQuit {
        pid: u32,
        detail: String,
    },
}

#[derive(Debug, Default)]
struct LockJson {
    pid: Option<u32>,
    host: Option<String>,
    port: Option<u16>,
}

pub fn lock_path(data_dir: &Path) -> std::path::PathBuf {
    data_dir.join(LOCK_FILE_NAME)
}

fn flock_exclusive_nonblocking(fd: i32) -> bool {
    unsafe { libc::flock(fd, libc::LOCK_EX | libc::LOCK_NB) == 0 }
}

fn flock_unlock(fd: i32) {
    unsafe {
        libc::flock(fd, libc::LOCK_UN);
    }
}

pub fn pid_alive(pid: u32) -> bool {
    unsafe { libc::kill(pid as i32, 0) == 0 }
}

/// Whether a pid can still act (not dead, not a zombie).
///
/// Mirrors `scripts/lib/owner_still_running.sh`: unreadable process state is
/// treated as running so callers do not signal or respawn against uncertainty.
pub fn pid_can_act(pid: u32) -> bool {
    if pid == 0 || !pid_alive(pid) {
        return false;
    }
    match process_state(pid) {
        None => true,
        Some('Z') | Some('z') => false,
        Some(_) => true,
    }
}

fn process_state(pid: u32) -> Option<char> {
    #[cfg(target_os = "linux")]
    {
        let path = format!("/proc/{}/stat", pid);
        let content = std::fs::read_to_string(path).ok()?;
        let after_paren = content.rsplit(')').next()?;
        let state = after_paren.split_whitespace().next()?;
        return state.chars().next();
    }
    #[cfg(not(target_os = "linux"))]
    {
        let output = std::process::Command::new("ps")
            .args(["-o", "state=", "-p", &pid.to_string()])
            .output()
            .ok()?;
        if !output.status.success() {
            return None;
        }
        let stdout = String::from_utf8_lossy(&output.stdout);
        stdout.trim().chars().next()
    }
}

pub fn read_lock_fields(lock_path: &Path) -> Option<(u32, u16)> {
    if !lock_path.is_file() {
        return None;
    }
    let raw = std::fs::read(lock_path).ok()?;
    let holder = parse_lock_json(&raw);
    let pid = holder.pid?;
    let port = holder.port?;
    if pid == 0 || port == 0 {
        return None;
    }
    Some((pid, port))
}

fn parse_lock_json(raw: &[u8]) -> LockJson {
    let Ok(value) = serde_json::from_slice::<serde_json::Value>(raw) else {
        return LockJson::default();
    };
    let obj = value.as_object();
    if obj.is_none() {
        return LockJson::default();
    }
    let obj = obj.expect("checked");
    LockJson {
        pid: obj.get("pid").and_then(|v| v.as_u64()).map(|v| v as u32),
        host: obj
            .get("host")
            .and_then(|v| v.as_str())
            .map(|v| v.to_string()),
        port: obj.get("port").and_then(|v| v.as_u64()).map(|v| v as u16),
    }
}

pub fn inspect_lock(lock_path: &Path, health_check: fn(u16) -> bool) -> LaunchPlan {
    if !lock_path.is_file() {
        return LaunchPlan::Spawn;
    }
    let file = OpenOptions::new()
        .read(true)
        .write(true)
        .open(lock_path);
    let mut file = match file {
        Ok(opened) => opened,
        Err(_) => return LaunchPlan::Spawn,
    };
    let fd = file.as_raw_fd();
    if flock_exclusive_nonblocking(fd) {
        flock_unlock(fd);
        return LaunchPlan::Spawn;
    }
    let mut buffer = [0_u8; 4096];
    let _ = file.seek(SeekFrom::Start(0));
    let read = file.read(&mut buffer).unwrap_or(0);
    let holder = parse_lock_json(&buffer[..read]);
    let pid = holder.pid.unwrap_or(0);
    if pid == 0 {
        return LaunchPlan::StopOrQuit {
            pid: 0,
            detail: format!(
                "engine lock {} is held but does not name a pid",
                lock_path.display()
            ),
        };
    }
    if !pid_can_act(pid) {
        return LaunchPlan::StopOrQuit {
            pid,
            detail: format!(
                "engine lock {} is held by dead or zombie pid {}",
                lock_path.display(),
                pid
            ),
        };
    }
    let port = holder.port.unwrap_or(0);
    if port == 0 || !health_check(port) {
        return LaunchPlan::StopOrQuit {
            pid,
            detail: format!(
                "engine lock {} is held by pid {} but is not answering health",
                lock_path.display(),
                pid
            ),
        };
    }
    let host = holder
        .host
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| "127.0.0.1".to_string());
    LaunchPlan::Adopt { pid, host, port }
}

pub fn origin_for_adopt(host: &str, port: u16) -> String {
    format!("http://{host}:{port}")
}

/// SIGTERM then SIGKILL a holder pid, using killpg when it is a group leader.
pub fn stop_holder_pid(pid: u32) {
    let pgid = pid as i32;
    engine::append_shell_log("shutdown", &format!("stopping adopted engine pid {pid}: SIGTERM"));
    let sigterm_sent = std::time::Instant::now();
    unsafe {
        if libc::killpg(pgid, libc::SIGTERM) != 0 {
            libc::kill(pgid, libc::SIGTERM);
        }
    }
    let deadline = sigterm_sent + engine::SHUTDOWN_GRACE;
    while std::time::Instant::now() < deadline {
        if !pid_alive(pid) {
            engine::append_shell_log(
                "shutdown",
                &format!(
                    "adopted engine pid {pid} exited {}ms after SIGTERM",
                    sigterm_sent.elapsed().as_millis()
                ),
            );
            return;
        }
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    engine::append_shell_log(
        "shutdown",
        &format!(
            "adopted engine pid {pid} did not exit within {}ms of SIGTERM; escalating to SIGKILL",
            sigterm_sent.elapsed().as_millis()
        ),
    );
    unsafe {
        if libc::killpg(pgid, libc::SIGKILL) != 0 {
            libc::kill(pgid, libc::SIGKILL);
        }
    }
    while pid_alive(pid) {
        std::thread::sleep(std::time::Duration::from_millis(50));
    }
    engine::append_shell_log("shutdown", &format!("adopted engine pid {pid} gone after SIGKILL"));
}

#[cfg(unix)]
fn spawn_zombie_child() -> (u32, libc::pid_t) {
    unsafe {
        let pid = libc::fork();
        assert!(pid >= 0, "fork failed");
        if pid == 0 {
            libc::_exit(0);
        }
        (pid as u32, pid)
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fs;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread;
    use std::time::Duration;

    fn health_always_true(_port: u16) -> bool {
        true
    }

    fn health_always_false(_port: u16) -> bool {
        false
    }

    fn set_dead_proxy_env() {
        std::env::set_var("HTTP_PROXY", "http://127.0.0.1:1");
        std::env::set_var("HTTPS_PROXY", "http://127.0.0.1:1");
        std::env::set_var("http_proxy", "http://127.0.0.1:1");
        std::env::set_var("https_proxy", "http://127.0.0.1:1");
    }

    fn wait_health_ok(port: u16) -> bool {
        for _ in 0..30 {
            if engine::health_ok(port) {
                return true;
            }
            thread::sleep(Duration::from_millis(20));
        }
        false
    }

    fn spawn_health_server() -> u16 {
        let (ready_tx, ready_rx) = std::sync::mpsc::channel();
        thread::spawn(move || {
            let listener = TcpListener::bind("127.0.0.1:0").expect("bind health server");
            let port = listener.local_addr().expect("local addr").port();
            ready_tx.send(port).expect("report health server port");
            let response =
                "HTTP/1.1 200 OK\r\nContent-Length: 2\r\nConnection: close\r\n\r\nok";
            for _ in 0..20 {
                if let Ok((mut stream, _)) = listener.accept() {
                    let _ = stream.set_read_timeout(Some(Duration::from_secs(2)));
                    let mut buf = [0_u8; 256];
                    let mut total = 0usize;
                    while total < buf.len() {
                        match stream.read(&mut buf[total..]) {
                            Ok(0) => break,
                            Ok(n) => {
                                total += n;
                                if total >= 4 && buf[..total].windows(4).any(|w| w == b"\r\n\r\n") {
                                    break;
                                }
                            }
                            Err(_) => break,
                        }
                    }
                    let _ = stream.write_all(response.as_bytes());
                    let _ = stream.flush();
                }
            }
        });
        ready_rx.recv().expect("health server port")
    }

    #[test]
    fn missing_lock_file_means_spawn() {
        let dir = std::env::temp_dir().join(format!("launch-missing-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        assert_eq!(
            inspect_lock(&dir.join(".engine.lock"), health_always_true),
            LaunchPlan::Spawn
        );
    }

    #[test]
    fn free_lock_file_means_spawn() {
        let dir = std::env::temp_dir().join(format!("launch-free-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("mkdir");
        let lock_path = dir.join(".engine.lock");
        fs::write(&lock_path, b"{}").expect("write lock");
        assert_eq!(inspect_lock(&lock_path, health_always_true), LaunchPlan::Spawn);
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn held_live_healthy_lock_means_adopt() {
        let dir = std::env::temp_dir().join(format!("launch-adopt-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("mkdir");
        let lock_path = dir.join(".engine.lock");
        let port = spawn_health_server();
        let pid = std::process::id();
        let blob = format!(
            r#"{{"pid":{pid},"host":"127.0.0.1","port":{port},"heartbeat_at":"2099-01-01T00:00:00.000+00:00"}}"#
        );
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&lock_path)
            .expect("open lock");
        let fd = file.as_raw_fd();
        assert_eq!(
            unsafe { libc::flock(fd, libc::LOCK_EX) },
            0,
            "hold lock for test"
        );
        file.write_all(blob.as_bytes()).expect("write holder json");
        set_dead_proxy_env();
        assert!(wait_health_ok(port), "health server should answer before adopt inspect");
        assert_eq!(
            inspect_lock(&lock_path, engine::health_ok),
            LaunchPlan::Adopt {
                pid,
                host: "127.0.0.1".to_string(),
                port,
            }
        );
        unsafe {
            libc::flock(fd, libc::LOCK_UN);
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn held_live_unhealthy_lock_means_stop_or_quit() {
        let dir = std::env::temp_dir().join(format!("launch-stop-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("mkdir");
        let lock_path = dir.join(".engine.lock");
        let pid = std::process::id();
        let blob = format!(r#"{{"pid":{pid},"host":"127.0.0.1","port":9,"heartbeat_at":"2099"}}"#);
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&lock_path)
            .expect("open lock");
        let fd = file.as_raw_fd();
        assert_eq!(unsafe { libc::flock(fd, libc::LOCK_EX) }, 0);
        file.write_all(blob.as_bytes()).expect("write holder json");
        let plan = inspect_lock(&lock_path, health_always_false);
        assert!(matches!(plan, LaunchPlan::StopOrQuit { .. }));
        unsafe {
            libc::flock(fd, libc::LOCK_UN);
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn held_dead_pid_is_not_adopt() {
        let dir = std::env::temp_dir().join(format!("launch-dead-{}", std::process::id()));
        let _ = fs::remove_dir_all(&dir);
        fs::create_dir_all(&dir).expect("mkdir");
        let lock_path = dir.join(".engine.lock");
        let blob = r#"{"pid":1,"host":"127.0.0.1","port":9}"#;
        let mut file = OpenOptions::new()
            .read(true)
            .write(true)
            .create(true)
            .truncate(true)
            .open(&lock_path)
            .expect("open lock");
        let fd = file.as_raw_fd();
        assert_eq!(unsafe { libc::flock(fd, libc::LOCK_EX) }, 0);
        file.write_all(blob.as_bytes()).expect("write holder json");
        let plan = inspect_lock(&lock_path, health_always_true);
        assert!(matches!(plan, LaunchPlan::StopOrQuit { .. }));
        assert_ne!(plan, LaunchPlan::Spawn);
        unsafe {
            libc::flock(fd, libc::LOCK_UN);
        }
        let _ = fs::remove_dir_all(&dir);
    }

    #[test]
    fn health_ok_true_for_local_200_server() {
        let port = spawn_health_server();
        set_dead_proxy_env();
        assert!(wait_health_ok(port), "health_ok should accept a local 200 response");
    }

    #[test]
    #[cfg(unix)]
    fn pid_can_act_rejects_a_zombie_child() {
        let (child_pid, raw_pid) = spawn_zombie_child();
        thread::sleep(Duration::from_millis(50));
        assert!(pid_alive(child_pid), "zombie should answer kill -0");
        assert!(!pid_can_act(child_pid), "zombie must not count as acting");
        let mut status = 0;
        assert_eq!(
            unsafe { libc::waitpid(raw_pid, &mut status, 0) },
            raw_pid,
            "parent must reap zombie"
        );
        assert!(!pid_alive(child_pid));
    }

    #[test]
    fn pid_can_act_rejects_zero() {
        assert!(!pid_can_act(0));
    }
}
