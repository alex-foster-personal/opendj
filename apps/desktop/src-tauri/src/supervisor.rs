//! Post-boot engine supervision: reap, restart, fatal UI, shell health.
//!
//! Each poll uses `try_wait` (via [`engine::Engine::try_reap`]) to detect exit
//! and reap zombies before restart.

use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU32, Ordering};
use std::sync::{Arc, Mutex, MutexGuard, TryLockError};
use std::thread;
use std::time::{Duration, Instant};

use tauri::{AppHandle, Manager};
use url::Url;

use crate::engine;
use crate::engine_log::{disk_free_bytes, ENGINE_LOG_MIN_FREE_BYTES};
use crate::launch;
use crate::shell_health::{ShellHealthServer, ShellHealthSnapshot, utc_timestamp_iso};

pub const POLL_INTERVAL: Duration = Duration::from_secs(5);
pub const RECOVERY_TIMEOUT: Duration = Duration::from_secs(30);
/// Consecutive failed health checks on an engine whose pid is still alive
/// before it is declared unresponsive and restarted. One miss is not a death:
/// a busy engine can take longer than the 750ms socket timeout to answer, and
/// on Fri 2 Oct 2026 a single miss on demon-llama put up the fatal page over
/// an engine that was still serving and then wedged the quit (see
/// `reap_child`). Six polls is about 30s of silence.
pub const UNRESPONSIVE_AFTER_MISSES: u32 = 6;
/// How long `EngineSupervisor::shutdown` waits for the supervisor lock before
/// stopping the engine without it. The lock can be held through a restart
/// (up to `engine::BOOT_TIMEOUT`), and a quit must never wait on that.
pub const SHUTDOWN_LOCK_WAIT: Duration = Duration::from_secs(3);
const WINDOW_LABEL: &str = "main";

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum SupervisorPhase {
    Running,
    Dead,
    Reaping,
    Restarting,
    AwaitingDiskSpace,
    Fatal,
    AwaitingRelaunch,
}

pub enum Supervised {
    Spawned(engine::Engine),
    Adopted {
        pid: u32,
        host: String,
        port: u16,
    },
}

impl Supervised {
    pub fn origin(&self) -> String {
        match self {
            Self::Spawned(running) => running.origin(),
            Self::Adopted { host, port, .. } => launch::origin_for_adopt(host, *port),
        }
    }

    pub fn port(&self) -> u16 {
        match self {
            Self::Spawned(running) => running.port(),
            Self::Adopted { port, .. } => *port,
        }
    }

    pub fn pid(&self) -> Option<u32> {
        match self {
            Self::Spawned(running) => Some(running.pid()),
            Self::Adopted { pid, .. } => Some(*pid),
        }
    }

    pub fn shutdown(&mut self) {
        match self {
            Self::Spawned(running) => running.shutdown(),
            Self::Adopted { pid, .. } => launch::stop_holder_pid(*pid),
        }
    }
}

impl Drop for Supervised {
    fn drop(&mut self) {
        self.shutdown();
    }
}

pub struct SupervisorPaths {
    pub payload: PathBuf,
    pub data_dir: PathBuf,
    pub log_path: PathBuf,
    pub product_name: String,
}

struct RuntimeState {
    supervised: Option<Supervised>,
    phase: SupervisorPhase,
    dead_at: Option<Instant>,
    exit_code: Option<i32>,
    lock_pid: Option<u32>,
    lock_port: Option<u16>,
    auto_restart_attempted: bool,
    /// Consecutive failed health checks while the engine pid is alive.
    health_misses: u32,
    paths: SupervisorPaths,
}

/// Engine handle for exit paths and runtime supervision.
pub struct EngineSupervisor {
    inner: Arc<Mutex<RuntimeState>>,
    shell_health: ShellHealthServer,
    /// Set once the shell is quitting: no further restarts, and the poll
    /// thread stops.
    stopping: AtomicBool,
    /// The engine pid (its pgid when spawned), readable WITHOUT the lock so
    /// a quit can always stop the engine even while a tick holds it. 0 means
    /// none.
    engine_pid: AtomicU32,
}

impl EngineSupervisor {
    pub fn new(
        supervised: Supervised,
        paths: SupervisorPaths,
        shell_health: ShellHealthServer,
    ) -> Self {
        let engine_pid = AtomicU32::new(supervised.pid().unwrap_or(0));
        Self {
            inner: Arc::new(Mutex::new(RuntimeState {
                supervised: Some(supervised),
                phase: SupervisorPhase::Running,
                dead_at: None,
                exit_code: None,
                lock_pid: None,
                lock_port: None,
                auto_restart_attempted: false,
                health_misses: 0,
                paths,
            })),
            shell_health,
            stopping: AtomicBool::new(false),
            engine_pid,
        }
    }

    /// Stop the engine on quit. Bounded: it never waits on the supervisor
    /// lock for longer than `SHUTDOWN_LOCK_WAIT`.
    ///
    /// The unbounded `lock()` this replaces is how a quit hung on Fri 2 Oct
    /// 2026: the poll thread held the lock in a blocking wait on a live
    /// engine, `RunEvent::Exit` blocked behind it on the main thread, and
    /// from then on the app ignored every quit, Apple Event or click.
    pub fn shutdown(&self) {
        self.stopping.store(true, Ordering::SeqCst);
        match lock_within(&self.inner, SHUTDOWN_LOCK_WAIT) {
            Some(mut guard) => {
                if let Some(mut running) = guard.supervised.take() {
                    running.shutdown();
                }
                self.engine_pid.store(0, Ordering::SeqCst);
            }
            None => {
                let pid = self.engine_pid.swap(0, Ordering::SeqCst);
                engine::append_shell_log(
                    "WARN",
                    &format!(
                        "supervisor busy for {}ms at quit; stopping engine pgid {pid} without it",
                        SHUTDOWN_LOCK_WAIT.as_millis()
                    ),
                );
                if pid != 0 {
                    force_stop_group(pid);
                }
            }
        }
    }

    fn is_stopping(&self) -> bool {
        self.stopping.load(Ordering::SeqCst)
    }

    fn record_engine_pid(&self, pid: Option<u32>) {
        self.engine_pid.store(pid.unwrap_or(0), Ordering::SeqCst);
    }

    pub fn shell_health_port(&self) -> u16 {
        self.shell_health.port()
    }

    fn request_relaunch(&self, app: &AppHandle) {
        engine::append_shell_log("INFO", "user chose relaunch");
        let _ = attempt_restart(app, self, true);
    }
}

/// Start the background poll thread and return the shared supervisor handle.
pub fn spawn_runtime_supervisor(
    app: AppHandle,
    supervised: Supervised,
    paths: SupervisorPaths,
    data_dir: &Path,
) -> Result<Arc<EngineSupervisor>, engine::EngineError> {
    let shell_health = ShellHealthServer::start(data_dir).map_err(|detail| {
        engine::EngineError::new(
            "Open DJ could not start its shell health server.",
            detail,
        )
    })?;
    let supervisor = Arc::new(EngineSupervisor::new(supervised, paths, shell_health));
    let inner = Arc::clone(&supervisor);
    thread::spawn(move || runtime_loop(app, inner));
    Ok(supervisor)
}

/// Alias used by source-scan tests and main.rs wiring.
pub fn start_runtime_supervisor(
    app: AppHandle,
    supervised: Supervised,
    paths: SupervisorPaths,
    data_dir: &Path,
) -> Result<Arc<EngineSupervisor>, engine::EngineError> {
    spawn_runtime_supervisor(app, supervised, paths, data_dir)
}

/// Lock `mutex`, giving up after `wait`. A poisoned lock is still usable
/// here: the state it guards is the engine handle, and stopping the engine is
/// exactly what a quit needs even after a panic elsewhere.
fn lock_within<T>(mutex: &Mutex<T>, wait: Duration) -> Option<MutexGuard<'_, T>> {
    let deadline = Instant::now() + wait;
    loop {
        match mutex.try_lock() {
            Ok(guard) => return Some(guard),
            Err(TryLockError::Poisoned(poisoned)) => return Some(poisoned.into_inner()),
            Err(TryLockError::WouldBlock) => {
                if Instant::now() >= deadline {
                    return None;
                }
                thread::sleep(Duration::from_millis(20));
            }
        }
    }
}

/// SIGTERM a process group, wait up to 5s, then SIGKILL it. Never waits
/// after the SIGKILL: this runs on the quit path without the child handle,
/// so a spawned engine's zombie cannot be reaped here and the process is
/// about to exit anyway.
fn force_stop_group(pid: u32) {
    let pgid = pid as i32;
    engine::append_shell_log("shutdown", &format!("stopping engine pgid {pgid}: SIGTERM"));
    // SAFETY: killpg/kill on the engine pid this shell spawned or adopted;
    // a zero pid never reaches here.
    unsafe {
        if libc::killpg(pgid, libc::SIGTERM) != 0 {
            libc::kill(pgid, libc::SIGTERM);
        }
    }
    let deadline = Instant::now() + engine::SHUTDOWN_GRACE;
    while Instant::now() < deadline {
        if !launch::pid_can_act(pid) {
            engine::append_shell_log("shutdown", &format!("engine pgid {pgid} stopped after SIGTERM"));
            return;
        }
        thread::sleep(Duration::from_millis(50));
    }
    engine::append_shell_log("shutdown", &format!("engine pgid {pgid} still running; SIGKILL"));
    unsafe {
        if libc::killpg(pgid, libc::SIGKILL) != 0 {
            libc::kill(pgid, libc::SIGKILL);
        }
    }
}

fn runtime_loop(app: AppHandle, supervisor: Arc<EngineSupervisor>) {
    loop {
        if supervisor.is_stopping() {
            engine::append_shell_log("INFO", "runtime supervisor stopped polling for quit");
            return;
        }
        tick(&app, supervisor.as_ref());
        if supervisor.shell_health.take_relaunch_request() {
            supervisor.request_relaunch(&app);
        }
        thread::sleep(POLL_INTERVAL);
    }
}

fn tick(app: &AppHandle, supervisor: &EngineSupervisor) {
    let mut guard = supervisor.inner.lock().expect("supervisor mutex");
    if supervisor.is_stopping() {
        return;
    }
    match guard.phase {
        SupervisorPhase::Running => {
            if let Some(dead) = detect_death(&mut guard) {
                guard.phase = SupervisorPhase::Dead;
                guard.dead_at = Some(Instant::now());
                guard.exit_code = dead.exit_code;
                guard.lock_pid = dead.lock_pid;
                guard.lock_port = dead.lock_port;
                engine::append_shell_log(
                    "WARN",
                    &format!(
                        "{} pid={} port={}",
                        dead.exit_reason(),
                        dead.lock_pid.unwrap_or(0),
                        dead.lock_port.unwrap_or(0)
                    ),
                );
                update_surfaces(app, supervisor, &guard);
                reap_child(&mut guard);
                guard.phase = SupervisorPhase::Restarting;
                guard.auto_restart_attempted = false;
                update_surfaces(app, supervisor, &guard);
            } else {
                publish_running(app, supervisor, &guard);
            }
        }
        SupervisorPhase::Restarting => {
            let dead_at = guard.dead_at.unwrap_or_else(Instant::now);
            if !guard.auto_restart_attempted {
                guard.auto_restart_attempted = true;
                let app_clone = app.clone();
                let supervisor_ref = supervisor;
                drop(guard);
                if attempt_restart(&app_clone, supervisor_ref, false) {
                    return;
                }
                guard = supervisor.inner.lock().expect("supervisor mutex");
                if is_low_disk(&guard.paths.data_dir) {
                    enter_awaiting_disk(app, supervisor, &mut guard);
                    return;
                }
            }
            if dead_at.elapsed() >= RECOVERY_TIMEOUT && guard.phase == SupervisorPhase::Restarting
            {
                if is_low_disk(&guard.paths.data_dir) {
                    enter_awaiting_disk(app, supervisor, &mut guard);
                } else {
                    guard.phase = SupervisorPhase::Fatal;
                    engine::append_shell_log("ERROR", "engine fatal: restart failed within 30s");
                    update_surfaces(app, supervisor, &guard);
                    show_fatal_dialog(app, &guard);
                }
            } else if guard.phase == SupervisorPhase::Restarting {
                publish_restarting(app, supervisor, &guard);
            }
        }
        SupervisorPhase::AwaitingDiskSpace => {
            if disk_space_recovered(&guard.paths.data_dir) {
                let app_clone = app.clone();
                drop(guard);
                if attempt_restart(&app_clone, supervisor, false) {
                    engine::append_shell_log("INFO", "engine restarted after low disk recovered");
                    return;
                }
                guard = supervisor.inner.lock().expect("supervisor mutex");
                guard.phase = SupervisorPhase::AwaitingDiskSpace;
            }
            publish_awaiting_disk(app, supervisor, &guard);
        }
        SupervisorPhase::Fatal | SupervisorPhase::AwaitingRelaunch | SupervisorPhase::Dead => {
            publish_dead(app, supervisor, &guard);
        }
        SupervisorPhase::Reaping => {}
    }
}

struct DeathInfo {
    exit_code: Option<i32>,
    /// The terminating signal, when the OS reported one. Only obtainable when
    /// this shell is the engine's real parent (the `Spawned` variant, reaped
    /// via `waitpid`); an `Adopted` engine's death is inferred from health and
    /// carries no signal.
    signal: Option<i32>,
    lock_pid: Option<u32>,
    lock_port: Option<u16>,
    /// The pid was still alive: the engine stopped answering health, it did
    /// not exit. Logged as such, never as an exit code it never had.
    unresponsive: bool,
}

impl DeathInfo {
    /// `engine-exited(signal N)` / `engine-exited(code N)`, matching the
    /// naming the shell log uses for every other exit trigger, so a reader
    /// can tell a killed engine from one that exited on its own.
    /// `engine-unresponsive(...)` when the process was still alive.
    fn exit_reason(&self) -> String {
        if self.unresponsive {
            return format!(
                "engine-unresponsive(pid alive, {UNRESPONSIVE_AFTER_MISSES} health checks missed)"
            );
        }
        match (self.signal, self.exit_code) {
            (Some(signal), _) => format!("engine-exited(signal {signal})"),
            (None, Some(code)) => format!("engine-exited(code {code})"),
            (None, None) => "engine-exited(unknown)".to_string(),
        }
    }
}

fn detect_death(guard: &mut RuntimeState) -> Option<DeathInfo> {
    let lock_path = launch::lock_path(&guard.paths.data_dir);
    let lock_holder = launch::read_lock_fields(&lock_path);
    let (pid, port) = match guard.supervised.as_mut() {
        Some(Supervised::Spawned(engine)) => {
            if let Some(status) = engine.try_reap() {
                use std::os::unix::process::ExitStatusExt;
                return Some(DeathInfo {
                    exit_code: status.code(),
                    signal: status.signal(),
                    lock_pid: lock_holder.map(|(pid, _)| pid).or(Some(engine.pid())),
                    lock_port: lock_holder.map(|(_, port)| port).or(Some(engine.port())),
                    unresponsive: false,
                });
            }
            if engine.is_log_failed() {
                return Some(DeathInfo {
                    exit_code: Some(1),
                    signal: None,
                    lock_pid: lock_holder.map(|(pid, _)| pid).or(Some(engine.pid())),
                    lock_port: lock_holder.map(|(_, port)| port).or(Some(engine.port())),
                    unresponsive: false,
                });
            }
            (engine.pid(), engine.port())
        }
        Some(Supervised::Adopted { pid, port, .. }) => (*pid, *port),
        None => return None,
    };
    if !launch::pid_can_act(pid) {
        guard.health_misses = 0;
        return Some(DeathInfo {
            exit_code: Some(1),
            signal: None,
            lock_pid: Some(pid),
            lock_port: Some(port),
            unresponsive: false,
        });
    }
    if engine::health_ok(port) {
        guard.health_misses = 0;
        return None;
    }
    guard.health_misses += 1;
    if !unresponsive_after(guard.health_misses) {
        engine::append_shell_log(
            "WARN",
            &format!(
                "engine health check missed ({}/{UNRESPONSIVE_AFTER_MISSES}) pid={pid} port={port}; pid alive, not restarting yet",
                guard.health_misses
            ),
        );
        return None;
    }
    guard.health_misses = 0;
    Some(DeathInfo {
        exit_code: None,
        signal: None,
        lock_pid: lock_holder.map(|(pid, _)| pid).or(Some(pid)),
        lock_port: Some(port),
        unresponsive: true,
    })
}

/// Whether `misses` consecutive failed health checks on a live engine are
/// enough to call it unresponsive.
fn unresponsive_after(misses: u32) -> bool {
    misses >= UNRESPONSIVE_AFTER_MISSES
}

/// Reap a dead child, or stop one that is still running.
///
/// Never a bare blocking wait: an unresponsive engine is still alive, and a
/// `wait()` on it, made while holding the supervisor lock, blocked until
/// something else killed it. That is the Fri 2 Oct 2026 quit hang.
/// `Engine::shutdown` is bounded (SIGTERM, grace, SIGKILL, then a wait that
/// a SIGKILLed child always satisfies) and reaps either way.
fn reap_child(guard: &mut RuntimeState) {
    guard.phase = SupervisorPhase::Reaping;
    if let Some(Supervised::Spawned(engine)) = guard.supervised.as_mut() {
        if engine.try_reap().is_none() {
            engine::append_shell_log("WARN", "engine pid still alive at reap; stopping it");
            engine.shutdown();
        }
    }
    engine::append_shell_log("INFO", "engine child reaped");
}

fn attempt_restart(app: &AppHandle, supervisor: &EngineSupervisor, _user_requested: bool) -> bool {
    let mut guard = supervisor.inner.lock().expect("supervisor mutex");
    let exit_code = guard.exit_code.unwrap_or(-1);
    let payload = guard.paths.payload.clone();
    let data_dir = guard.paths.data_dir.clone();
    let log_path = guard.paths.log_path.clone();
    let product_name = guard.paths.product_name.clone();
    if supervisor.is_stopping() {
        return false;
    }
    if let Some(mut old) = guard.supervised.take() {
        old.shutdown();
    }
    supervisor.record_engine_pid(None);
    // A quit can start during the stop above, which may take the full grace.
    // It found no engine to stop, so starting one now would outlive the shell.
    if supervisor.is_stopping() {
        engine::append_shell_log("INFO", "quit began during restart; not respawning");
        return false;
    }
    if let Err(err) = write_parent_file(&data_dir) {
        engine::append_shell_log("ERROR", &format!("restart failed: {err}"));
        guard.phase = SupervisorPhase::Fatal;
        update_surfaces(app, supervisor, &guard);
        return false;
    }
    let lock_path = launch::lock_path(&data_dir);
    match launch::inspect_lock(&lock_path, engine::health_ok) {
        launch::LaunchPlan::Adopt { pid, host, port } => {
            guard.supervised = Some(Supervised::Adopted { pid, host, port });
            supervisor.record_engine_pid(Some(pid));
            guard.phase = SupervisorPhase::Running;
            guard.dead_at = None;
            guard.auto_restart_attempted = false;
            guard.health_misses = 0;
            log_restart_success(exit_code);
            let origin = guard.supervised.as_ref().expect("adopted").origin();
            update_surfaces(app, supervisor, &guard);
            navigate_to_origin(app, &origin, &product_name);
            return true;
        }
        launch::LaunchPlan::StopOrQuit { pid, detail } => {
            engine::append_shell_log(
                "ERROR",
                &format!("engine fatal: lock held by pid {pid}: {detail}"),
            );
            guard.phase = SupervisorPhase::Fatal;
            update_surfaces(app, supervisor, &guard);
            return false;
        }
        launch::LaunchPlan::Spawn => {}
    }
    let port = match engine::free_loopback_port() {
        Ok(port) => port,
        Err(err) => {
            engine::append_shell_log("ERROR", &format!("restart failed: {err}"));
            guard.phase = SupervisorPhase::Fatal;
            update_surfaces(app, supervisor, &guard);
            return false;
        }
    };
    let mut running = match engine::spawn(&payload, &data_dir, &log_path, port) {
        Ok(engine) => {
            // Recorded before the health wait, so a quit during the boot
            // still has a pid to stop.
            supervisor.record_engine_pid(Some(engine.pid()));
            engine
        }
        Err(err) => {
            engine::append_shell_log("ERROR", &format!("restart failed: {err}"));
            guard.phase = SupervisorPhase::Fatal;
            update_surfaces(app, supervisor, &guard);
            return false;
        }
    };
    // `shutdown` sets the latch before it takes the recorded pid, and the pid
    // was recorded before this load, so either the quit saw this engine's pid
    // or this load sees the latch. Without the check, a quit that won the
    // race took pid 0 and the new engine outlived the shell.
    if supervisor.is_stopping() {
        engine::append_shell_log("INFO", "quit began during restart; stopping the new engine");
        running.shutdown();
        supervisor.record_engine_pid(None);
        return false;
    }
    match running.wait_until_healthy(engine::BOOT_TIMEOUT) {
        Ok(()) => {
            guard.supervised = Some(Supervised::Spawned(running));
            guard.phase = SupervisorPhase::Running;
            guard.dead_at = None;
            guard.auto_restart_attempted = false;
            guard.health_misses = 0;
            log_restart_success(exit_code);
            let origin = guard.supervised.as_ref().expect("spawned").origin();
            update_surfaces(app, supervisor, &guard);
            navigate_to_origin(app, &origin, &product_name);
            true
        }
        Err(err) => {
            running.shutdown();
            supervisor.record_engine_pid(None);
            engine::append_shell_log("ERROR", &format!("restart failed: {err}"));
            guard.phase = SupervisorPhase::Fatal;
            update_surfaces(app, supervisor, &guard);
            false
        }
    }
}

fn log_restart_success(exit_code: i32) {
    let line = format!(
        "{} engine restarted after exit code {}",
        utc_timestamp_iso(),
        exit_code
    );
    engine::append_shell_log("INFO", &line);
}

fn write_parent_file(data_dir: &Path) -> Result<(), String> {
    let path = data_dir.join(".engine.parent");
    std::fs::write(&path, format!("{}\n", std::process::id())).map_err(|err| {
        format!("{}: {err}", path.display())
    })?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).map_err(|err| {
            format!("{}: {err}", path.display())
        })?;
    }
    Ok(())
}

fn is_low_disk(data_dir: &Path) -> bool {
    disk_free_bytes(data_dir)
        .map(|free| free < ENGINE_LOG_MIN_FREE_BYTES)
        .unwrap_or(false)
}

fn disk_space_recovered(data_dir: &Path) -> bool {
    disk_free_bytes(data_dir)
        .map(|free| free >= ENGINE_LOG_MIN_FREE_BYTES)
        .unwrap_or(false)
}

fn enter_awaiting_disk(
    app: &AppHandle,
    supervisor: &EngineSupervisor,
    guard: &mut RuntimeState,
) {
    guard.phase = SupervisorPhase::AwaitingDiskSpace;
    engine::append_shell_log("WARN", "engine awaiting disk space recovery");
    update_surfaces(app, supervisor, guard);
}

fn publish_awaiting_disk(app: &AppHandle, supervisor: &EngineSupervisor, guard: &RuntimeState) {
    supervisor.shell_health.update(ShellHealthSnapshot {
        status: "waiting".into(),
        engine: "waiting-disk".into(),
        lock_pid: guard.lock_pid,
        lock_port: guard.lock_port,
        exit_code: guard.exit_code,
        reason: Some("low-disk".into()),
    });
    set_window_title(
        app,
        &format!(
            "{} - waiting for disk space",
            guard.paths.product_name
        ),
    );
}

fn publish_running(app: &AppHandle, supervisor: &EngineSupervisor, guard: &RuntimeState) {
    let (lock_pid, lock_port) = launch::read_lock_fields(&launch::lock_path(&guard.paths.data_dir))
        .or_else(|| guard.supervised.as_ref().map(|s| (s.pid().unwrap_or(0), s.port())))
        .unwrap_or((0, 0));
    supervisor.shell_health.update(ShellHealthSnapshot {
        status: "ok".into(),
        engine: "running".into(),
        lock_pid: Some(lock_pid),
        lock_port: Some(lock_port),
        exit_code: None,
        reason: None,
    });
    set_window_title(app, &guard.paths.product_name);
}

fn publish_restarting(app: &AppHandle, supervisor: &EngineSupervisor, guard: &RuntimeState) {
    supervisor.shell_health.update(ShellHealthSnapshot {
        status: "restarting".into(),
        engine: "restarting".into(),
        lock_pid: guard.lock_pid,
        lock_port: guard.lock_port,
        exit_code: guard.exit_code,
        reason: None,
    });
    set_window_title(app, &format!("{} - engine restarting", guard.paths.product_name));
}

fn publish_dead(app: &AppHandle, supervisor: &EngineSupervisor, guard: &RuntimeState) {
    supervisor.shell_health.update(ShellHealthSnapshot {
        status: "dead".into(),
        engine: "dead".into(),
        lock_pid: guard.lock_pid,
        lock_port: guard.lock_port,
        exit_code: guard.exit_code,
        reason: None,
    });
    set_window_title(app, &format!("{} - engine dead", guard.paths.product_name));
}

fn update_surfaces(app: &AppHandle, supervisor: &EngineSupervisor, guard: &RuntimeState) {
    match guard.phase {
        SupervisorPhase::Running => publish_running(app, supervisor, guard),
        SupervisorPhase::Restarting => publish_restarting(app, supervisor, guard),
        SupervisorPhase::AwaitingDiskSpace => publish_awaiting_disk(app, supervisor, guard),
        SupervisorPhase::Dead | SupervisorPhase::Fatal | SupervisorPhase::AwaitingRelaunch => {
            publish_dead(app, supervisor, guard);
            navigate_fatal_bootstrap(app, guard, supervisor.shell_health.port());
        }
        SupervisorPhase::Reaping => {}
    }
}

fn set_window_title(app: &AppHandle, title: &str) {
    let title = title.to_string();
    let handle = app.clone();
    let _ = handle.clone().run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window(WINDOW_LABEL) {
            let _ = window.set_title(&title);
        }
    });
}

fn navigate_fatal_bootstrap(app: &AppHandle, guard: &RuntimeState, health_port: u16) {
    let exit_code = guard.exit_code.unwrap_or(-1);
    let pid = guard.lock_pid.unwrap_or(0);
    let port = guard.lock_port.unwrap_or(0);
    let product = guard.paths.product_name.clone();
    let script = format!(
        "globalThis.__OPENDJ_ENGINE_SUPERVISOR__ = {{ engine: 'dead', exit_code: {exit_code}, lock_pid: {pid}, lock_port: {port}, health_port: {health_port} }};"
    );
    let query = format!("index.html?fatal=1&exit={exit_code}&pid={pid}&port={port}");
    let handle = app.clone();
    let _ = handle.clone().run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window(WINDOW_LABEL) {
            let _ = window.eval(&script);
            let target = Url::parse(&format!("tauri://localhost/{query}")).expect("fatal url");
            let _ = window.navigate(target);
            let dead_title = format!("{product} - engine dead");
            let _ = window.set_title(&dead_title);
        }
    });
}

fn navigate_to_origin(app: &AppHandle, origin: &str, product_name: &str) {
    let origin = origin.to_string();
    let product_name = product_name.to_string();
    let handle = app.clone();
    let _ = handle.clone().run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window(WINDOW_LABEL) {
            let script = format!(
                "globalThis.OPENDJ_ENGINE_ORIGIN = {};",
                serde_json::to_string(&origin).unwrap_or_else(|_| "\"\"".into())
            );
            let _ = window.eval(&script);
            let _ = window.set_title(&product_name);
            let target = format!("{origin}/performance").parse::<Url>().expect("origin url");
            let _ = window.navigate(target);
        }
    });
}

fn show_fatal_dialog(app: &AppHandle, guard: &RuntimeState) {
    let exit_code = guard.exit_code.unwrap_or(-1);
    let pid = guard.lock_pid.unwrap_or(0);
    let port = guard.lock_port.unwrap_or(0);
    let detail = format!(
        "The engine exited with code {exit_code} (pid {pid}, port {port}).\n\n\
         Relaunch starts a fresh engine, or quit the app."
    );
    let app_clone = app.clone();
    let _ = app.run_on_main_thread(move || {
        let choice = rfd::MessageDialog::new()
            .set_level(rfd::MessageLevel::Error)
            .set_title("Open DJ engine stopped")
            .set_description(&detail)
            .set_buttons(rfd::MessageButtons::OkCancelCustom(
                "Relaunch".to_string(),
                "Quit".to_string(),
            ))
            .show();
        match choice {
            rfd::MessageDialogResult::Ok => {
                engine::append_shell_log("INFO", "user chose relaunch from native dialog");
                if let Some(state) = app_clone.try_state::<RuntimeSupervisorState>() {
                    state.supervisor.request_relaunch(&app_clone);
                }
            }
            _ => {
                engine::append_shell_log("INFO", "engine fatal: restart declined");
                std::process::exit(1);
            }
        }
    });
}

/// Tauri-managed handle so native dialogs can trigger relaunch on the main thread.
pub struct RuntimeSupervisorState {
    pub supervisor: Arc<EngineSupervisor>,
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn supervisor_phase_dead_is_distinct_from_running() {
        assert_ne!(SupervisorPhase::Running, SupervisorPhase::Dead);
        assert_ne!(SupervisorPhase::Restarting, SupervisorPhase::Fatal);
    }

    #[test]
    fn recovery_timeout_matches_heartbeat_interval() {
        assert_eq!(RECOVERY_TIMEOUT, Duration::from_secs(30));
        assert!(POLL_INTERVAL <= RECOVERY_TIMEOUT);
    }

    // - if a signal-killed engine is logged only by exit code then `kill -9`
    //   and a plain `exit(1)` are indistinguishable in the shell log, which is
    //   exactly what issue #2801's acceptance test checks for -> broken
    // - if a signal takes priority over a stray exit code on the same
    //   `ExitStatus` then a real signal death could be misreported as a code
    //   exit -> broken

    fn death(exit_code: Option<i32>, signal: Option<i32>) -> DeathInfo {
        DeathInfo {
            exit_code,
            signal,
            lock_pid: None,
            lock_port: None,
            unresponsive: false,
        }
    }

    #[test]
    fn a_signal_kill_is_named_by_its_signal_number() {
        assert_eq!(death(None, Some(9)).exit_reason(), "engine-exited(signal 9)");
    }

    #[test]
    fn an_ordinary_exit_is_named_by_its_code() {
        assert_eq!(death(Some(1), None).exit_reason(), "engine-exited(code 1)");
    }

    #[test]
    fn a_signal_is_named_even_alongside_a_placeholder_exit_code() {
        // `ExitStatus::code()` is platform-defined when a process was signaled
        // (macOS reports None; some platforms could report something else),
        // so the signal must win over any accompanying code.
        assert_eq!(death(Some(1), Some(9)).exit_reason(), "engine-exited(signal 9)");
    }

    #[test]
    fn neither_signal_nor_code_says_so_rather_than_guessing() {
        assert_eq!(death(None, None).exit_reason(), "engine-exited(unknown)");
    }

    // - if one missed health check on a live pid counts as a death then a
    //   busy engine gets the fatal page while it is still serving -> broken
    //   (demon-llama, Fri 2 Oct 2026)
    // - if misses never add up to unresponsive then a hung engine is never
    //   restarted -> broken (the opposite overshoot)
    // - if an unresponsive engine is logged as an exit code then the log
    //   says it exited when it did not -> broken

    #[test]
    fn a_single_missed_health_check_is_not_unresponsive() {
        assert!(!unresponsive_after(1));
        assert!(!unresponsive_after(UNRESPONSIVE_AFTER_MISSES - 1));
    }

    #[test]
    fn enough_consecutive_misses_are_unresponsive() {
        assert!(unresponsive_after(UNRESPONSIVE_AFTER_MISSES));
        assert!(unresponsive_after(UNRESPONSIVE_AFTER_MISSES + 1));
    }

    #[test]
    fn an_unresponsive_engine_is_not_reported_as_an_exit() {
        let info = DeathInfo { unresponsive: true, ..death(None, None) };
        let reason = info.exit_reason();
        assert!(reason.starts_with("engine-unresponsive("), "{reason}");
        assert!(!reason.contains("exited"), "{reason}");
    }

    // - if a held lock blocks shutdown forever then quit hangs behind a busy
    //   supervisor tick -> broken (the Fri 2 Oct 2026 hang)
    // - if a free lock is given up on then a normal quit skips the clean
    //   path that reaps the child -> broken

    #[test]
    fn lock_within_gives_up_on_a_held_lock() {
        let mutex = Arc::new(Mutex::new(0_u8));
        let holder = Arc::clone(&mutex);
        let (held_tx, held_rx) = std::sync::mpsc::channel();
        let (release_tx, release_rx) = std::sync::mpsc::channel::<()>();
        let handle = thread::spawn(move || {
            let _guard = holder.lock().unwrap();
            held_tx.send(()).unwrap();
            let _ = release_rx.recv();
        });
        held_rx.recv().unwrap();
        let started = Instant::now();
        assert!(lock_within(&mutex, Duration::from_millis(200)).is_none());
        assert!(started.elapsed() >= Duration::from_millis(200));
        assert!(started.elapsed() < Duration::from_secs(2));
        release_tx.send(()).unwrap();
        handle.join().unwrap();
    }

    #[test]
    fn lock_within_takes_a_free_lock() {
        let mutex = Mutex::new(7_u8);
        assert_eq!(*lock_within(&mutex, Duration::from_millis(10)).unwrap(), 7);
    }

    #[test]
    fn force_stop_group_stops_a_process_group() {
        use std::os::unix::process::CommandExt;
        let mut child = std::process::Command::new("sleep")
            .arg("30")
            .process_group(0)
            .spawn()
            .expect("spawn sleep");
        let pid = child.id();
        let reaper = thread::spawn(move || child.wait());
        let started = Instant::now();
        force_stop_group(pid);
        let status = reaper.join().unwrap().expect("wait");
        assert!(!status.success());
        assert!(started.elapsed() < engine::SHUTDOWN_GRACE);
    }
}
