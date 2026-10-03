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
//! 5. Engine output is never silently lost. The log is proved writable
//!    before the engine is spawned, an interrupted read is reissued rather
//!    than read as the end of the stream, and a pump that does die stops the
//!    engine instead of leaving it running with nothing recording what it
//!    does. A log that quietly stops is indistinguishable from an engine
//!    that had nothing to say, which is the one report this shell must not
//!    be able to make.

use std::io::{Read, Write};
use std::net::{SocketAddr, TcpListener, TcpStream};
use std::os::unix::process::CommandExt;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::{Arc, Mutex, OnceLock};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::engine_log::{append_rotated, rotate_log_if_needed, ENGINE_LOG_MAX_BYTES};

/// The payload directory inside the bundle, relative to Contents/Resources.
pub const PAYLOAD_DIR: &str = "payload";

/// The one entry point the shell knows. Everything else about the payload
/// (interpreter, dependency layout, module names) is the payload's business.
pub const ENGINE_LAUNCHER: &str = "bin/opendj-engine";

/// How long a cold engine boot may take before the shell calls it failed.
/// Measured boots on this Mac are ~1.5s; 30s is a wide margin over a first
/// launch on a slower machine while still being a bound rather than a wait.
pub const BOOT_TIMEOUT: Duration = Duration::from_secs(30);

/// How long a stopping engine gets between SIGTERM and SIGKILL.
///
/// Long enough for the engine's own shutdown to finish: uvicorn's graceful
/// window (`GRACEFUL_SHUTDOWN_S`, apps/engine_core/__main__.py), the refresh
/// job's CLIs being stopped (`STOP_ALL_MAX_S`,
/// apps/webui/server/routes/ingest_cli_procs.py), and the job runner settling
/// jobs still forking (`_SPAWN_SETTLE_S`, apps/engine_core/jobs/runner.py),
/// cancelling the forks that outlast it (`_FORK_ABORT_WAIT_S`, same file)
/// then reaping its worker groups (`WORKER_TERMINATE_GRACE_S`,
/// apps/engine_core/jobs/reap.py), worst case one after another. tests/scripts/test_desktop_quit_budget.py holds
/// that sum under this number. Job
/// workers lead their own sessions, so this group's SIGKILL never reaches
/// them: a SIGKILL that lands before the runner has reaped them leaves them
/// running after the app is gone (three analysis workers on demon-llama,
/// Fri 2 Oct 2026, which then blocked the DMG installer). It is a bound,
/// not a wait: an engine that exits sooner is reaped sooner.
pub const SHUTDOWN_GRACE: Duration = Duration::from_secs(25);

const HEALTH_PATH: &str = "/api/v1/health";
const POLL_INTERVAL: Duration = Duration::from_millis(150);
const SOCKET_TIMEOUT: Duration = Duration::from_millis(750);

static SHELL_LOG_PATH: OnceLock<PathBuf> = OnceLock::new();

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
    pub(crate) fn new(headline: impl Into<String>, detail: impl Into<String>) -> Self {
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
    log_sink: Arc<LogSink>,
}

impl Engine {
    pub fn origin(&self) -> String {
        format!("http://127.0.0.1:{}", self.port)
    }

    pub fn port(&self) -> u16 {
        self.port
    }

    pub fn pid(&self) -> u32 {
        self.child.id()
    }

    pub fn is_log_failed(&self) -> bool {
        self.log_failure().is_some()
    }

    /// Non-blocking reap. Returns the exit status when the child has exited.
    pub fn try_reap(&mut self) -> Option<std::process::ExitStatus> {
        self.child.try_wait().ok().flatten()
    }

    /// A test-only engine around an arbitrary child, so supervision can be
    /// exercised against a real process without a payload.
    #[cfg(test)]
    pub(crate) fn for_test(child: Child, data_dir: &Path) -> Self {
        let log_path = data_dir.join("engine.log");
        Self {
            child,
            port: 1,
            data_dir: data_dir.to_path_buf(),
            log_path: log_path.clone(),
            log_sink: Arc::new(LogSink::new(log_path)),
        }
    }

    /// Wait for the engine to answer its own health route.
    ///
    /// Polling health rather than trusting the spawn is the whole point: a
    /// process that started and then refused its data dir is exactly the
    /// failure a naive shell renders as an empty window.
    /// Why the log pump stopped, if it did.
    ///
    /// `None` is the healthy case AND the only case in which this shell may
    /// say the engine started: see rule 5.
    pub fn log_failure(&self) -> Option<String> {
        self.log_sink.failure()
    }

    pub fn wait_until_healthy(&mut self, timeout: Duration) -> Result<(), EngineError> {
        let deadline = Instant::now() + timeout;
        loop {
            // BEFORE the exit check, because a pump failure stops the engine:
            // asked in the other order this reports a bare exit status and
            // buries the reason underneath it.
            if let Some(detail) = self.log_failure() {
                return Err(EngineError::new(
                    "Open DJ lost the engine log.",
                    format!(
                        "{detail}\n\
                         The engine was stopped rather than left running with \
                         nothing recording it.\n\
                         Data directory: {}\n\
                         Engine log: {}",
                        self.data_dir.display(),
                        self.log_path.display()
                    ),
                ));
            }
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
    ///
    /// Bounded on every path: SIGTERM, SHUTDOWN_GRACE, then SIGKILL. Callers that
    /// must not hang (the supervisor's poll thread above all) rely on that.
    pub fn shutdown(&mut self) {
        let pid = self.child.id() as i32;
        if self.child.try_wait().ok().flatten().is_some() {
            // The leader is gone but its group may not be: a `kill -9` of the
            // engine alone leaves its analysis workers running, reparented to
            // launchd, for as long as their batch takes.
            sweep_orphaned_group(pid);
            return;
        }
        append_shell_log("shutdown", &format!("stopping engine pgid {pid}: SIGTERM"));
        // SAFETY: killpg on a pgid this process created. A negative or zero
        // pid is impossible here because Child::id() is the spawned pid.
        unsafe {
            libc::killpg(pid, libc::SIGTERM);
        }
        let sigterm_sent = Instant::now();
        let deadline = sigterm_sent + SHUTDOWN_GRACE;
        loop {
            if Instant::now() >= deadline {
                break;
            }
            match self.child.try_wait() {
                Ok(Some(_)) => {
                    append_shell_log(
                        "shutdown",
                        &format!(
                            "engine pgid {pid} exited {}ms after SIGTERM",
                            sigterm_sent.elapsed().as_millis()
                        ),
                    );
                    sweep_orphaned_group(pid);
                    return;
                }
                Ok(None) => std::thread::sleep(Duration::from_millis(50)),
                Err(err) => {
                    append_shell_log(
                        "WARN",
                        &format!("could not poll engine pgid {pid} after SIGTERM: {err}"),
                    );
                    break;
                }
            }
        }
        append_shell_log(
            "shutdown",
            &format!(
                "engine pgid {pid} did not exit within {}ms of SIGTERM; escalating to SIGKILL",
                sigterm_sent.elapsed().as_millis()
            ),
        );
        unsafe {
            libc::killpg(pid, libc::SIGKILL);
        }
        let _ = self.child.wait();
        append_shell_log(
            "shutdown",
            &format!("engine pgid {pid} reaped after SIGKILL"),
        );
    }
}

impl Drop for Engine {
    fn drop(&mut self) {
        self.shutdown();
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
pub fn health_ok(port: u16) -> bool {
    let address = SocketAddr::from(([127, 0, 0, 1], port));
    let Ok(mut stream) = TcpStream::connect_timeout(&address, SOCKET_TIMEOUT) else {
        return false;
    };
    let _ = stream.set_nodelay(true);
    if stream.set_read_timeout(Some(SOCKET_TIMEOUT)).is_err() {
        return false;
    }
    let _ = stream.set_write_timeout(Some(SOCKET_TIMEOUT));
    let request = format!(
        "GET {HEALTH_PATH} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\n\
         Connection: close\r\nAccept: application/json\r\n\r\n"
    );
    if stream.write_all(request.as_bytes()).is_err() {
        return false;
    }
    let mut response = Vec::new();
    let mut byte = [0_u8; 1];
    loop {
        if response.len() >= 4096 {
            break;
        }
        match stream.read(&mut byte) {
            Ok(0) => break,
            Ok(_) => {
                response.push(byte[0]);
                if byte[0] == b'\n' {
                    break;
                }
            }
            Err(_) => break,
        }
    }
    String::from_utf8_lossy(&response).starts_with("HTTP/1.1 200")
}

// ----- build profile ------------------------------------------------------

/// The feature-flag profile name a sandboxed build runs under.
///
/// Mirrors `apps/feature_flags/profiles.py`. The engine validates the name and
/// refuses an unknown one with the list of real profiles, so a typo here dies
/// at boot rather than silently shipping the full build.
pub const APPSTORE_PROFILE: &str = "appstore";

/// macOS sets this inside an App Sandbox container.
const SANDBOX_CONTAINER_ENV: &str = "APP_SANDBOX_CONTAINER_ID";

/// Where macOS redirects a sandboxed process's home.
const SANDBOX_HOME_MARKER: &str = "/Library/Containers/";

/// Which `--build-profile` this boot passes, or None for the full build.
///
/// DETECTED AT RUNTIME, NOT COMPILED IN (SAND-04). The obvious alternative is
/// a cargo feature set by the store lane, but that has to be remembered at
/// package time and is wrong whenever anyone forgets: a store bundle built
/// from the dmg lane would offer USB export, which cannot work in a sandbox,
/// and would fail as an EMPTY DRIVE LIST rather than as a refusal. Being
/// inside a container is a fact about the process, so the shell asks the
/// process. That is also self-correcting in both directions -- a store build
/// is sandboxed by definition and a dmg build never is -- which is why no
/// store-only build flag is needed anywhere.
///
/// Two independent signals, because either alone can be defeated: the env var
/// is absent on some spawn paths that still inherit the container, and a
/// developer can point HOME at a container-shaped path without being
/// sandboxed. A false positive costs an explicit refusal the user can read; a
/// false negative costs the silent empty list. That asymmetry is why either
/// signal alone is enough to trip it.
///
/// Pure in its inputs so it is testable without mutating process environment,
/// which is global and would race the other tests in this binary.
///
/// `host_is_macos` is passed explicitly rather than read from `cfg!` inside
/// this function, mirroring `apps/shared/sandbox.py`'s `platform` parameter:
/// the App Sandbox is macOS-only, so a hardcoded `cfg!(target_os = "macos")`
/// check here would make every call short-circuit to `None` on the Linux CI
/// runner that actually runs this test suite, leaving the signal logic
/// untested where it runs.
pub fn build_profile_for(
    container_id: Option<&str>,
    home: Option<&str>,
    host_is_macos: bool,
) -> Option<&'static str> {
    if !host_is_macos {
        return None;
    }
    let container_set = container_id.is_some_and(|value| !value.trim().is_empty());
    let home_in_container = home.is_some_and(|value| value.contains(SANDBOX_HOME_MARKER));
    if container_set || home_in_container {
        Some(APPSTORE_PROFILE)
    } else {
        None
    }
}

/// [`build_profile_for`] against this process's real environment.
fn build_profile() -> Option<&'static str> {
    let container = std::env::var(SANDBOX_CONTAINER_ENV).ok();
    let home = std::env::var("HOME").ok();
    build_profile_for(
        container.as_deref(),
        home.as_deref(),
        cfg!(target_os = "macos"),
    )
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
        std::fs::create_dir_all(parent).map_err(|err| {
            EngineError::new(
                "Open DJ could not create its log folder.",
                format!("{}: {err}", parent.display()),
            )
        })?;
    }
    rotate_log_if_needed(log_path, ENGINE_LOG_MAX_BYTES).map_err(|err| {
        EngineError::new(
            "Open DJ could not rotate its engine log.",
            format!("{}: {err}", log_path.display()),
        )
    })?;
    // BEFORE the spawn, on purpose. A log path that is a directory, a folder
    // the user cannot write, or a full disk are launch-time facts. Learning
    // them from a pump thread means the engine is already running and the
    // user has already been told it started.
    verify_log_writable(log_path)?;
    let mut command = Command::new(&launcher);
    command
        .arg("--data-dir")
        .arg(data_dir)
        .arg("--host")
        .arg("127.0.0.1")
        .arg("--port")
        .arg(port.to_string())
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env(
            "OPENDJ_ENGINE_WARN_LOG",
            log_path.with_file_name("engine-warn.log"),
        )
        .env("OPENDJ_ENGINE_LOG_BOOT_ID", log_boot_id())
        .env("OPENDJ_PARENT_PID", std::process::id().to_string())
        .process_group(0);
    // A sandboxed shell means an App Store build, and the engine must run the
    // profile that turns off what the sandbox forbids. Passed as an argument
    // rather than an env var so it survives STRIPPED_ENV below and shows up in
    // the engine's own boot line, where a wrong profile is visible.
    if let Some(profile) = build_profile() {
        command.arg("--build-profile").arg(profile);
    }
    for name in STRIPPED_ENV {
        command.env_remove(name);
    }
    let mut child = command.spawn().map_err(|err| {
        EngineError::new(
            "Open DJ could not start its engine.",
            format!("Launching {} failed: {err}", launcher.display()),
        )
    })?;
    let sink = Arc::new(LogSink::new(log_path.to_path_buf()));
    // The child is its own group leader (`process_group(0)` above), so this
    // pid is also the pgid a failing pump signals.
    let pgid = child.id() as i32;
    spawn_log_reader(
        child.stdout.take().expect("engine stdout was piped"),
        "stdout",
        Arc::clone(&sink),
        pgid,
    );
    spawn_log_reader(
        child.stderr.take().expect("engine stderr was piped"),
        "stderr",
        Arc::clone(&sink),
        pgid,
    );
    Ok(Engine {
        child,
        port,
        data_dir: data_dir.to_path_buf(),
        log_path: log_path.to_path_buf(),
        log_sink: sink,
    })
}

/// The engine's log file, and the one place a pump failure is recorded.
///
/// The mutex is an append lock rather than a path lock: stdout and stderr are
/// pumped by two threads into one file that either of them may rotate, so
/// unguarded appends can interleave a write with a rename. `failure` is the
/// channel back to supervision, and it exists because an engine that outlives
/// its log pump reports a healthy launch and then loses every line it writes.
struct LogSink {
    path: PathBuf,
    append_lock: Mutex<()>,
    failure: Mutex<Option<String>>,
}

impl LogSink {
    fn new(path: PathBuf) -> Self {
        Self {
            path,
            append_lock: Mutex::new(()),
            failure: Mutex::new(None),
        }
    }

    fn append(&self, bytes: &[u8]) -> std::io::Result<()> {
        let _guard = self.append_lock.lock().expect("engine log mutex poisoned");
        append_rotated(&self.path, bytes)
    }

    /// Record why the pump stopped. The first reason wins: whichever stream
    /// failed first is the cause, and the second is usually its consequence.
    fn record_failure(&self, detail: String) {
        append_shell_log("ERROR", &detail);
        let mut slot = self
            .failure
            .lock()
            .expect("engine log failure mutex poisoned");
        if slot.is_none() {
            *slot = Some(detail);
        }
    }

    fn failure(&self) -> Option<String> {
        self.failure
            .lock()
            .expect("engine log failure mutex poisoned")
            .clone()
    }
}

/// Copy one engine stream into the log until it ends, or say why it stopped.
///
/// `Interrupted` is what the previous `while let Ok(read)` loop got wrong: a
/// signal delivered mid-read is the OS asking for the read to be reissued,
/// not the end of the stream, so reading it as the end silently stops
/// capturing a perfectly healthy engine. Every other read error, and every
/// append or rotation error, is terminal and is RETURNED rather than printed
/// and swallowed, because the caller is what turns it into a stopped engine.
fn pump_stream<R: Read>(mut stream: R, stream_name: &str, sink: &LogSink) -> Result<(), String> {
    let mut buffer = [0; 8192];
    loop {
        match stream.read(&mut buffer) {
            Ok(0) => return Ok(()),
            Ok(read) => sink.append(&buffer[..read]).map_err(|error| {
                format!(
                    "could not write engine {stream_name} to {}: {error}",
                    sink.path.display()
                )
            })?,
            Err(error) if error.kind() == std::io::ErrorKind::Interrupted => continue,
            Err(error) => return Err(format!("could not read engine {stream_name}: {error}")),
        }
    }
}

/// Pump one stream on its own thread, and stop the engine if the pump dies.
///
/// Stopping is the point. The shell used to print one line and return, which
/// left an engine running with no log, on behalf of a user who had been told
/// it started fine. The group kill is the one [`Engine::shutdown`] uses, so
/// the engine's own children go with it, and the recorded reason is what
/// [`Engine::wait_until_healthy`] reports instead of a bare exit status.
fn spawn_log_reader<R: Read + Send + 'static>(
    stream: R,
    stream_name: &'static str,
    sink: Arc<LogSink>,
    pgid: i32,
) {
    std::thread::spawn(move || {
        if let Err(detail) = pump_stream(stream, stream_name, &sink) {
            sink.record_failure(detail);
            stop_process_group(pgid);
        }
    });
}

/// SIGTERM a process group this shell created at spawn.
fn stop_process_group(pgid: i32) {
    // SAFETY: killpg on a pgid this process created. It is the child's own
    // pid, because `process_group(0)` made the child a group leader, so this
    // can never reach the shell's own group.
    unsafe {
        libc::killpg(pgid, libc::SIGTERM);
    }
}

/// SIGKILL whatever is left in a group whose leader has already exited.
///
/// Called right after the leader is reaped. A pgid is not reissued while any
/// process still belongs to that group, so a surviving member keeps this
/// signal aimed at the dead engine's own workers; an empty group is ESRCH.
fn sweep_orphaned_group(pgid: i32) {
    // SAFETY: killpg on the pgid this shell created at spawn (see above).
    let swept = unsafe { libc::killpg(pgid, libc::SIGKILL) } == 0;
    if swept {
        append_shell_log(
            "shutdown",
            &format!("engine pgid {pgid}: SIGKILLed processes left behind by the exited engine"),
        );
    }
}

/// Prove the engine log can be appended to, before anything depends on it.
fn verify_log_writable(log_path: &Path) -> Result<(), EngineError> {
    open_append(log_path).map(drop).map_err(|err| {
        EngineError::new(
            "Open DJ could not write its engine log.",
            format!("{}: {err}", log_path.display()),
        )
    })
}

fn open_append(log_path: &Path) -> std::io::Result<std::fs::File> {
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
}

fn log_boot_id() -> String {
    let millis = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_millis();
    format!("shell-{}-{millis}", std::process::id())
}

fn append_shell_log_to_path(log_path: &Path, level: &str, message: &str) {
    let line = format!("[shell {level}] {message}\n");
    if let Err(err) = append_rotated(log_path, line.as_bytes()) {
        if err.kind() == std::io::ErrorKind::NotFound {
            eprintln!("[{level}] {message}");
            return;
        }
        eprintln!("[shell log failed] {err}: [{level}] {message}");
    }
}

/// Append one shell-owned line to the shared engine log.
pub fn append_shell_log(level: &str, message: &str) {
    let Some(path) = SHELL_LOG_PATH.get() else {
        eprintln!("[{level}] {message}");
        return;
    };
    append_shell_log_to_path(path, level, message);
}

fn install_shell_log_path(lock: &OnceLock<PathBuf>, log_path: &Path) -> Result<(), EngineError> {
    if let Some(parent) = log_path.parent() {
        std::fs::create_dir_all(parent).map_err(|err| {
            EngineError::new(
                "Open DJ could not create its log folder.",
                format!("{}: {err}", parent.display()),
            )
        })?;
    }
    verify_log_writable(log_path)?;
    match lock.set(log_path.to_path_buf()) {
        Ok(()) => Ok(()),
        Err(requested) => {
            let retained = lock
                .get()
                .map(|path| path.display().to_string())
                .unwrap_or_else(|| "unknown".into());
            Err(EngineError::new(
                "Open DJ shell logging is already installed.",
                format!("retained {} requested {}", retained, requested.display()),
            ))
        }
    }
}

/// Route shell stderr and panics into the same engine log the child uses.
pub fn install_shell_logging(log_path: &Path) -> Result<(), EngineError> {
    install_shell_log_path(&SHELL_LOG_PATH, log_path)?;
    std::panic::set_hook(Box::new(|info| {
        let payload = if let Some(message) = info.payload().downcast_ref::<&str>() {
            (*message).to_string()
        } else if let Some(message) = info.payload().downcast_ref::<String>() {
            message.clone()
        } else {
            "panic".into()
        };
        let location = info
            .location()
            .map(|loc| format!("{}:{}", loc.file(), loc.line()))
            .unwrap_or_else(|| "unknown".into());
        let detail = format!("panic at {location}: {payload}");
        append_shell_log("panic", &detail);
        eprintln!("[PANIC] {detail}");
    }));
    Ok(())
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

#[cfg(test)]
mod tests {
    use super::*;

    // ----- build profile ---------------------------------------------------
    //
    // Both directions, because a one-directional guard passes its own bug
    // report perfectly. Under-detecting ships USB export into a sandbox where
    // it returns an empty drive list; over-detecting turns the feature off in
    // the dmg, where it works fine. Each has a test.

    #[test]
    fn a_container_env_var_selects_the_appstore_profile() {
        assert_eq!(
            build_profile_for(Some("com.opendj.desktop"), Some("/Users/dj"), true),
            Some(APPSTORE_PROFILE)
        );
    }

    #[test]
    fn a_container_shaped_home_selects_the_appstore_profile() {
        // The second signal alone, with the env var absent: some spawn paths
        // inherit the container without setting it.
        assert_eq!(
            build_profile_for(
                None,
                Some("/Users/dj/Library/Containers/com.opendj.desktop/Data"),
                true
            ),
            Some(APPSTORE_PROFILE)
        );
    }

    #[test]
    fn an_unsandboxed_shell_passes_no_profile() {
        // The over-detection control. A dmg build must keep USB export, so
        // this asserts the ORIGINAL behavior still holds where it should.
        assert_eq!(build_profile_for(None, Some("/Users/dj"), true), None);
        assert_eq!(build_profile_for(None, None, true), None);
    }

    #[test]
    fn a_blank_container_id_is_not_a_container() {
        // An exported-but-empty variable is the shell's version of a zero
        // that is both a value and an error signature. Empty means absent.
        assert_eq!(build_profile_for(Some(""), Some("/Users/dj"), true), None);
        assert_eq!(
            build_profile_for(Some("   "), Some("/Users/dj"), true),
            None
        );
    }

    #[test]
    fn a_home_merely_containing_library_is_not_a_container() {
        // Substring matching is the trap here: ~/Library alone is every Mac.
        // Only the Containers segment means a sandbox.
        assert_eq!(
            build_profile_for(None, Some("/Users/dj/Library/Application Support"), true),
            None
        );
    }

    #[test]
    fn the_sandbox_is_macos_only() {
        // Mirrors apps/shared/sandbox.py's test_the_sandbox_is_macos_only: a
        // Linux or Windows host is never sandboxed, whatever the process
        // signals say. This is the case that a bare `cfg!(target_os =
        // "macos")` inside the helper made untestable on Linux CI, where it
        // always returned None before looking at either signal.
        assert_eq!(
            build_profile_for(
                Some("com.opendj.desktop"),
                Some("/Users/dj/Library/Containers/com.opendj.desktop/Data"),
                false
            ),
            None
        );
    }

    // - if an interrupted read ends the pump then one signal during a healthy
    //   engine's life silently stops all logging -> broken
    // - if a terminal read error is swallowed then the shell reports a
    //   healthy launch while capturing nothing -> broken
    // - if an unwritable log target is discovered by the pump rather than
    //   before the spawn then the engine is already running when the shell
    //   finds out -> broken
    // - if a pump failure does not stop the engine then the app runs on with
    //   nothing recording what it did -> broken
    // - if a recorded pump failure still reports a healthy boot then the
    //   whole durability contract is decorative -> broken

    /// A stream that hands out exactly the reads a test asks for.
    ///
    /// Real pipes cannot be made to return `Interrupted` on demand, and that
    /// is the case the pump has to get right, so the stream is the fixture.
    struct ScriptedStream {
        steps: Vec<std::io::Result<&'static [u8]>>,
        next: usize,
    }

    impl ScriptedStream {
        fn new(steps: Vec<std::io::Result<&'static [u8]>>) -> Self {
            Self { steps, next: 0 }
        }
    }

    impl Read for ScriptedStream {
        fn read(&mut self, buffer: &mut [u8]) -> std::io::Result<usize> {
            let step = self
                .steps
                .get(self.next)
                .expect("the pump read past the end of its script");
            self.next += 1;
            match step {
                Ok(bytes) => {
                    buffer[..bytes.len()].copy_from_slice(bytes);
                    Ok(bytes.len())
                }
                Err(error) => Err(std::io::Error::new(error.kind(), error.to_string())),
            }
        }
    }

    fn scratch_dir(name: &str) -> PathBuf {
        let directory = std::env::temp_dir().join(format!("opendj-{name}-{}", log_boot_id()));
        std::fs::create_dir_all(&directory).unwrap();
        directory
    }

    fn sleeping_child() -> Child {
        Command::new("/bin/sleep")
            .arg("30")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .unwrap()
    }

    /// A child that survives SIGTERM, so [`Engine::shutdown`] has no choice
    /// but to escalate to SIGKILL.
    ///
    /// A single tail `sleep 30` will not do: a POSIX shell execs into the
    /// last command of a script rather than forking it, which replaces the
    /// shell (and its trap) with a plain `sleep` that dies on SIGTERM like
    /// any other process. The loop keeps the shell -- and its ignored TERM --
    /// as the live, tracked process for the whole test.
    fn sigterm_ignoring_child() -> Child {
        let mut command = Command::new("/bin/sleep");
        command
            .arg("30")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0);
        // SIG_IGN, unlike a handler function, survives exec, so `sleep`
        // inherits "ignore SIGTERM" instead of reverting to the default that
        // would kill it. Set from the child side of fork, before exec, so
        // the test binary's own (unrelated) signal disposition is untouched.
        unsafe {
            command.pre_exec(|| {
                if libc::signal(libc::SIGTERM, libc::SIG_IGN) == libc::SIG_ERR {
                    return Err(std::io::Error::last_os_error());
                }
                Ok(())
            });
        }
        command.spawn().unwrap()
    }

    #[test]
    fn retries_interrupted_reads_instead_of_ending_the_pump() {
        let directory = scratch_dir("pump-interrupted");
        let sink = LogSink::new(directory.join("engine.log"));
        let stream = ScriptedStream::new(vec![
            Err(std::io::Error::from(std::io::ErrorKind::Interrupted)),
            Ok(b"first\n"),
            Err(std::io::Error::from(std::io::ErrorKind::Interrupted)),
            Ok(b"second\n"),
            Ok(b""),
        ]);

        pump_stream(stream, "stdout", &sink).unwrap();

        assert_eq!(
            std::fs::read_to_string(&sink.path).unwrap(),
            "first\nsecond\n"
        );
        assert!(sink.failure().is_none());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn reports_a_terminal_read_error_rather_than_swallowing_it() {
        let directory = scratch_dir("pump-read-error");
        let sink = LogSink::new(directory.join("engine.log"));
        let stream = ScriptedStream::new(vec![
            Ok(b"before\n"),
            Err(std::io::Error::from(std::io::ErrorKind::BrokenPipe)),
        ]);

        let failure = pump_stream(stream, "stderr", &sink).unwrap_err();

        assert!(
            failure.contains("could not read engine stderr"),
            "{failure}"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn refuses_an_unwritable_log_target_before_the_engine_exists() {
        let directory = scratch_dir("pump-unwritable");
        let log_path = directory.join("engine.log");
        // A directory where the log belongs: every append to it fails, the
        // way a read-only folder or a full disk does.
        std::fs::create_dir_all(&log_path).unwrap();

        let refusal = verify_log_writable(&log_path).unwrap_err();

        assert!(
            refusal.headline.contains("could not write its engine log"),
            "{refusal}"
        );
        let sink = LogSink::new(log_path);
        let stream = ScriptedStream::new(vec![Ok(b"line\n")]);
        let failure = pump_stream(stream, "stdout", &sink).unwrap_err();
        assert!(
            failure.contains("could not write engine stdout"),
            "{failure}"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn stopping_the_group_terminates_the_engine() {
        let mut child = sleeping_child();

        stop_process_group(child.id() as i32);

        let deadline = Instant::now() + Duration::from_secs(5);
        while Instant::now() < deadline {
            if child.try_wait().unwrap().is_some() {
                return;
            }
            std::thread::sleep(Duration::from_millis(25));
        }
        child.kill().unwrap();
        panic!("stop_process_group left the engine running");
    }

    // - if `Engine::shutdown` never escalates past SIGTERM then an engine
    //   that ignores it (or one wedged deep enough not to act on it) outlives
    //   the shell that thinks it stopped -> broken
    // - if shutdown returns before the SIGTERM grace period then a slow
    //   but honest shutdown looks identical to one that never got a chance to
    //   comply -> broken (this is the "SIGKILL escalation is logged
    //   distinctly from SIGTERM" contract issue #2801 asks for; the log
    //   content itself is exercised by DeathInfo::exit_reason and
    //   exit_requested_trigger's own tests, since the shell log's global
    //   OnceLock sink is not reliably assertable across parallel tests)

    #[test]
    fn shutdown_escalates_to_sigkill_when_sigterm_is_ignored() {
        let directory = scratch_dir("shutdown-escalate");
        let log_path = directory.join("engine.log");
        let sink = Arc::new(LogSink::new(log_path.clone()));
        let mut engine = Engine {
            child: sigterm_ignoring_child(),
            port: 1,
            data_dir: directory.clone(),
            log_path,
            log_sink: sink,
        };

        let started = Instant::now();
        engine.shutdown();
        let elapsed = started.elapsed();

        assert!(
            elapsed >= SHUTDOWN_GRACE,
            "shutdown must wait out the SIGTERM grace period before escalating, took {elapsed:?}"
        );
        assert!(
            elapsed < SHUTDOWN_GRACE + Duration::from_secs(5),
            "SIGKILL should terminate the child promptly once sent, took {elapsed:?}"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    // - if an engine that was `kill -9`ed alone leaves its own workers alive
    //   after the shell reaps it then every crash leaks an analysis pool onto
    //   a machine that may already be swapping -> broken

    fn group_is_gone(pgid: i32) -> bool {
        // -1 with ESRCH (empty) or, on macOS, EPERM (only zombies left).
        let probe = unsafe { libc::killpg(pgid, 0) };
        probe == -1
    }

    #[test]
    fn shutdown_of_an_already_dead_engine_kills_its_orphaned_workers() {
        let directory = scratch_dir("orphan-sweep");
        // The backgrounded sleep is the worker: same group, different pid.
        let child = Command::new("/bin/sh")
            .arg("-c")
            .arg("/bin/sleep 120 & exec /bin/sleep 120")
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .process_group(0)
            .spawn()
            .unwrap();
        let pgid = child.id() as i32;
        let mut engine = Engine::for_test(child, &directory);
        std::thread::sleep(Duration::from_millis(200));
        unsafe {
            libc::kill(pgid, libc::SIGKILL);
        }
        while engine.try_reap().is_none() {
            std::thread::sleep(Duration::from_millis(20));
        }
        assert!(
            !group_is_gone(pgid),
            "fixture: the worker must outlive its leader"
        );

        engine.shutdown();

        let deadline = Instant::now() + Duration::from_secs(3);
        while !group_is_gone(pgid) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(25));
        }
        let gone = group_is_gone(pgid);
        if !gone {
            unsafe {
                libc::killpg(pgid, libc::SIGKILL);
            }
        }
        std::fs::remove_dir_all(directory).unwrap();
        assert!(gone, "shutdown left the dead engine's worker running");
    }

    #[test]
    fn a_lost_log_fails_the_launch_instead_of_reporting_healthy() {
        let directory = scratch_dir("pump-launch");
        let log_path = directory.join("engine.log");
        let sink = Arc::new(LogSink::new(log_path.clone()));
        sink.record_failure("could not write engine stdout: disk full".into());
        let mut engine = Engine {
            child: sleeping_child(),
            // Nothing listens here, so a healthy verdict could only come from
            // skipping the check.
            port: 1,
            data_dir: directory.clone(),
            log_path,
            log_sink: sink,
        };

        let failure = engine.wait_until_healthy(BOOT_TIMEOUT).unwrap_err();

        assert!(
            failure.headline.contains("lost the engine log"),
            "{failure}"
        );
        assert!(failure.detail.contains("disk full"), "{failure}");
        engine.shutdown();
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn shell_logging_appends_prefixed_lines() {
        let directory = scratch_dir("shell-log");
        let log_path = directory.join("engine.log");
        append_shell_log_to_path(&log_path, "WARN", "monitor scale factor 0");
        let contents = std::fs::read_to_string(&log_path).unwrap();
        assert!(
            contents.contains("[shell WARN] monitor scale factor 0\n"),
            "expected prefixed line in log, got: {contents}"
        );
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn shell_logging_missing_path_does_not_panic() {
        let directory = scratch_dir("shell-log-missing");
        let log_path = directory.join("engine.log");
        append_shell_log_to_path(&log_path, "WARN", "before");
        std::fs::remove_dir_all(directory).unwrap();
        append_shell_log_to_path(&log_path, "panic", "probe");
    }

    #[test]
    fn shell_logging_rejects_second_install() {
        let first_dir = scratch_dir("shell-log-first");
        let first_path = first_dir.join("engine.log");
        let second_dir = scratch_dir("shell-log-second");
        let second_path = second_dir.join("engine.log");
        let lock = OnceLock::new();

        install_shell_log_path(&lock, &first_path).unwrap();
        let failure = install_shell_log_path(&lock, &second_path).unwrap_err();

        assert!(
            failure
                .headline
                .contains("shell logging is already installed"),
            "{failure}"
        );
        assert!(
            failure.detail.contains(&first_path.display().to_string()),
            "{failure}"
        );
        assert!(
            failure.detail.contains(&second_path.display().to_string()),
            "{failure}"
        );
        assert_eq!(lock.get(), Some(&first_path));

        std::fs::remove_dir_all(first_dir).unwrap();
        std::fs::remove_dir_all(second_dir).unwrap();
    }
}
