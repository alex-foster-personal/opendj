//! The bundled engine, supervised by the shell.
//!
//! WHAT CHANGED, AND WHY IT IS STILL A THIN SHELL
//!
//! The shell used to be a window onto an engine somebody else had started,
//! which made the artifact a demo rather than an app: it only worked on a
//! machine with the repo, a venv and a terminal. It now starts the engine
//! that ships inside its own bundle. That is lifecycle, not product logic:
//! no `#[tauri::command]` handlers, no HTTP client for the API, no database.
//! Everything a user sees is still served by the engine over HTTP.
//!
//! THE FOUR RULES THIS MODULE ENFORCES
//!
//! 1. The port is chosen by the OS, never hardcoded. Two Open DJ builds and
//!    any number of dev servers coexist on one Mac only if nobody claims a
//!    fixed number.
//! 2. The data directory is this bundle's own Application Support directory.
//!    It is never the repo's data/, and it is never a real rekordbox
//!    library.
//! 3. Rekordbox writeback is actively unset before the engine is spawned.
//!    One-way safety is absolute, so the shell does not merely decline to
//!    enable it -- it removes any inherited opt-in.
//! 4. A boot that does not reach a healthy /api/v1/health inside the timeout
//!    raises a native error dialog naming the failure. A blank window that
//!    never resolves is not an outcome this shell has.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

/// The payload directory inside the bundle, relative to Contents/Resources.
pub const PAYLOAD_DIR: &str = "payload";

/// The one entry point the shell knows. Everything else about the payload
/// (interpreter, dependency layout, module names) is the payload's business.
pub const ENGINE_LAUNCHER: &str = "bin/opendj-engine";

/// How long a cold engine boot may take before the shell calls it failed.
/// Measured boots on this Mac are ~1.5s; 30s is a wide margin over a first
/// launch on a slower machine while still being a bound rather than a wait.
pub const BOOT_TIMEOUT: Duration = Duration::from_secs(30);

const HEALTH_PATH: &str = "/api/v1/health";
const POLL_INTERVAL: Duration = Duration::from_millis(150);
const SOCKET_TIMEOUT: Duration = Duration::from_millis(750);

/// Environment the shell strips before spawning the engine.
///
/// A build launched from a developer's terminal inherits that terminal's
/// exported .env. MDT_DATA_DIR would silently point the installed app at a
/// worktree's library, and MDT_REKORDBOX_WRITEBACK_ENABLED would arm a
/// destructive path the app must never arm. WEB_CONCURRENCY makes the engine
/// refuse to boot at all.
const STRIPPED_ENV: [&str; 4] = [
    "MDT_DATA_DIR",
    "MDT_REKORDBOX_WRITEBACK_ENABLED",
    "MUSIC_DJ_STATE_BACKEND",
    "WEB_CONCURRENCY",
];

#[derive(Debug)]
pub struct EngineError {
    pub headline: String,
    pub detail: String,
}

impl EngineError {
    fn new(headline: impl Into<String>, detail: impl Into<String>) -> Self {
        Self {
            headline: headline.into(),
            detail: detail.into(),
        }
    }
}

impl std::fmt::Display for EngineError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}\n\n{}", self.headline, self.detail)
    }
}

/// A spawned engine and the address it was told to serve on.
pub struct Engine {
    child: Child,
    port: u16,
    data_dir: PathBuf,
    log_path: PathBuf,
}

impl Engine {
    pub fn origin(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    /// Wait for the engine to answer its own health route.
    ///
    /// Polling health rather than trusting the spawn is the whole point: a
    /// process that started and then refused its data dir is exactly the
    /// failure a naive shell renders as an empty window.
    pub fn wait_until_healthy(&mut self, timeout: Duration) -> Result<(), EngineError> {
        let deadline = Instant::now() + timeout;
        loop {
            if let Some(status) = self.child.try_wait().unwrap_or(None) {
                return Err(EngineError::new(
                    "The Open DJ engine stopped while starting up.",
                    format!(
                        "It exited with {status} before answering {HEALTH_PATH}.\n\
                         Data directory: {}\n\
                         Engine log: {}",
                        self.data_dir.display(),
                        self.log_path.display()
                    ),
                ));
            }
            if health_ok(self.port) {
                return Ok(());
            }
            if Instant::now() >= deadline {
                return Err(EngineError::new(
                    "The Open DJ engine did not finish starting.",
                    format!(
                        "It is still running but did not answer \
                         http://127.0.0.1:{}{HEALTH_PATH} within {}s.\n\
                         Data directory: {}\n\
                         Engine log: {}",
                        self.port,
                        timeout.as_secs(),
                        self.data_dir.display(),
                        self.log_path.display()
                    ),
                ));
            }
            std::thread::sleep(POLL_INTERVAL);
        }
    }

    /// Terminate the engine AND anything it spawned.
    ///
    /// The engine runs jobs in child processes, so killing only the daemon
    /// leaves orphans holding the data dir's lock and a tester's next launch
    /// fails for no visible reason. The child was placed in its own process
    /// group at spawn precisely so one signal can reach all of it.
    pub fn shutdown(&mut self) {
        let pid = self.child.id() as i32;
        // SAFETY: killpg on a pgid this process created. A negative or zero
        // pid is impossible here because Child::id() is the spawned pid.
        unsafe {
            libc::killpg(pid, libc::SIGTERM);
        }
        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            match self.child.try_wait() {
                Ok(Some(_)) => return,
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(_) => break,
            }
        }
        unsafe {
            libc::killpg(pid, libc::SIGKILL);
        }
        let _ = self.child.wait();
    }
}

// ----- port ---------------------------------------------------------------
/// Ask the OS for a loopback port nobody is using.
///
/// Binding port 0 and reading back what was assigned is the only way to get
/// a port that is free RIGHT NOW. A hardcoded number is a collision waiting
/// for the second app, and this Mac already runs several engines at once.
/// The listener is dropped immediately; the window between that and the
/// engine's own bind is a race the engine reports by failing to bind, which
/// the health poll then surfaces as a named error rather than a hang.
pub fn free_loopback_port() -> Result<u16, EngineError> {
    let listener = TcpListener::bind("127.0.0.1:0").map_err(|err| {
        EngineError::new(
            "Open DJ could not reserve a local port.",
            format!("Binding 127.0.0.1:0 failed: {err}"),
        )
    })?;
    let port = listener
        .local_addr()
        .map_err(|err| {
            EngineError::new(
                "Open DJ could not read back the port it reserved.",
                err.to_string(),
            )
        })?
        .port();
    drop(listener);
    Ok(port)
}

// ----- health -------------------------------------------------------------
/// One loopback GET, status line only.
///
/// Deliberately not an HTTP crate. The shell makes exactly one kind of
/// request, to one fixed path, on loopback, and reads one line of the
/// response. Pulling in a full client (and, with it, a TLS stack) to do that
/// would add a network-capable dependency to a binary whose entire security
/// story is that it never talks to the network.
fn health_ok(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, SOCKET_TIMEOUT) else {
        return false;
    };
    if stream.set_read_timeout(Some(SOCKET_TIMEOUT)).is_err() {
        return false;
    }
    let request = format!(
        "GET {HEALTH_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\
         Connection: close\r\nAccept: application/json\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = Vec::new();
    if stream.take(4096).read_to_end(&mut response).is_err() {
        return false;
    }
    String::from_utf8_lossy(&response).starts_with("HTTP/1.1 200")
}

// ----- spawn --------------------------------------------------------------
/// Start the bundled engine on `port`, writing its output to `log_path`.
///
/// `process_group(0)` is what makes the child the leader of a new group, so
/// [`Engine::shutdown`] can signal the whole tree. Without it, a job the
/// engine spawned outlives the app and keeps the data dir locked.
pub fn spawn(
    payload_dir: &Path,
    data_dir: &Path,
    log_path: &Path,
    port: u16,
) -> Result<Engine, EngineError> {
    let launcher = payload_dir.join(ENGINE_LAUNCHER);
    if !launcher.is_file() {
        return Err(EngineError::new(
            "This Open DJ build has no engine inside it.",
            format!(
                "Expected the bundled engine at {}. The .app was assembled \
                 without its payload; reinstall from a complete dmg.",
                launcher.display()
            ),
        ));
    }
    std::fs::create_dir_all(data_dir).map_err(|err| {
        EngineError::new(
            "Open DJ could not create its data folder.",
            format!("{}: {err}", data_dir.display()),
        )
    })?;
    if let Some(parent) = log_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let log = std::fs::File::create(log_path).map_err(|err| {
        EngineError::new(
            "Open DJ could not open its engine log.",
            format!("{}: {err}", log_path.display()),
        )
    })?;
    let log_err = log.try_clone().map_err(|err| {
        EngineError::new("Open DJ could not duplicate its engine log handle.", err.to_string())
    })?;

    let mut command = Command::new(&launcher);
    command
        .arg("--data-dir")
        .arg(data_dir)
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::from(log))
        .stderr(Stdio::from(log_err))
        .process_group(0);
    for name in STRIPPED_ENV {
        command.env_remove(name);
    }
    let child = command.spawn().map_err(|err| {
        EngineError::new(
            "Open DJ could not start its engine.",
            format!("Launching {} failed: {err}", launcher.display()),
        )
    })?;
    Ok(Engine {
        child,
        port,
        data_dir: data_dir.to_path_buf(),
        log_path: log_path.to_path_buf(),
    })
}

/// The tail of the engine log, for an error dialog that says something.
pub fn log_tail(log_path: &Path, lines: usize) -> String {
    let Ok(text) = std::fs::read_to_string(log_path) else {
        return String::new();
    };
    let collected: Vec<&str> = text.lines().collect();
    let start = collected.len().saturating_sub(lines);
    collected[start..].join("\n")
}
