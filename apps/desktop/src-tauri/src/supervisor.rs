//! Post-boot engine supervision: reap, restart, fatal UI, shell health.
//!
//! Each poll uses `try_wait` (via [`engine::Engine::try_reap`]) to detect exit
//! and reap zombies before restart.

use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
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
/// Consecutive failed health polls before a LIVE engine is declared hung.
///
/// One failed probe is not a death. Under memory pressure (an 8 GB Mac deep
/// in swap while an analysis drain runs) a healthy engine can miss a 750ms
/// probe, and declaring it dead on the first miss is what showed a fatal
/// screen over an engine that went on serving for hours. Six polls at
/// [`POLL_INTERVAL`] is 30s of silence, the same budget as a restart.
pub const UNRESPONSIVE_POLLS: u32 = 6;
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
    unhealthy_polls: u32,
    paths: SupervisorPaths,
}

/// Engine handle for exit paths and runtime supervision.
pub struct EngineSupervisor {
    inner: Arc<Mutex<RuntimeState>>,
    shell_health: ShellHealthServer,
}

impl EngineSupervisor {
    pub fn new(
        supervised: Supervised,
        paths: SupervisorPaths,
        shell_health: ShellHealthServer,
    ) -> Self {
        Self {
            inner: Arc::new(Mutex::new(RuntimeState {
                supervised: Some(supervised),
                phase: SupervisorPhase::Running,
                dead_at: None,
                exit_code: None,
                lock_pid: None,
                lock_port: None,
                auto_restart_attempted: false,
                unhealthy_polls: 0,
                paths,
            })),
            shell_health,
        }
    }

    pub fn shutdown(&self) {
        if let Ok(mut guard) = self.inner.lock() {
            if let Some(mut running) = guard.supervised.take() {
                running.shutdown();
            }
        }
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

/// The one thread that acts on the engine.
///
/// Every step here must be bounded: the HTTP relaunch request is only a flag
/// this loop consumes, so anything that blocks `tick` (it once waited forever
/// on a live child it had wrongly declared dead) silently turns Relaunch into
/// a 202 that nothing ever acts on.
fn runtime_loop(app: AppHandle, supervisor: Arc<EngineSupervisor>) {
    loop {
        tick(&app, supervisor.as_ref());
        if supervisor.shell_health.take_relaunch_request() {
            let phase = supervisor.inner.lock().expect("supervisor mutex").phase;
            if relaunch_applies(phase) {
                engine::append_shell_log(
                    "INFO",
                    &format!("relaunch requested over shell health (phase {phase:?})"),
                );
                supervisor.request_relaunch(&app);
            } else {
                engine::append_shell_log(
                    "INFO",
                    &format!("relaunch request ignored: engine is {phase:?}"),
                );
            }
        }
        thread::sleep(POLL_INTERVAL);
    }
}

/// Whether a user's Relaunch should start a fresh engine in this phase.
///
/// Every post-death phase does. A running engine is left alone: a stale tab
/// still showing the fatal page must not be able to kill a healthy set.
fn relaunch_applies(phase: SupervisorPhase) -> bool {
    match phase {
        SupervisorPhase::Running | SupervisorPhase::Reaping => false,
        SupervisorPhase::Dead
        | SupervisorPhase::Restarting
        | SupervisorPhase::AwaitingDiskSpace
        | SupervisorPhase::Fatal
        | SupervisorPhase::AwaitingRelaunch => true,
    }
}

fn tick(app: &AppHandle, supervisor: &EngineSupervisor) {
    let mut guard = supervisor.inner.lock().expect("supervisor mutex");
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
    /// Set when the engine was alive but stopped answering and the shell
    /// stopped it: the polls it missed. Distinct from an exit the engine
    /// chose, so the log never reports a kill by the shell as `code 1`.
    unresponsive_polls: Option<u32>,
}

/// What one supervision poll concluded about the engine.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Liveness {
    Alive,
    Gone,
    Unresponsive,
}

/// Judge one poll, returning the verdict and the new failed-poll count.
///
/// A process that is still there is only declared hung after
/// [`UNRESPONSIVE_POLLS`] consecutive failed probes; one is noise.
fn judge_liveness(alive: bool, healthy: bool, unhealthy_polls: u32) -> (Liveness, u32) {
    if !alive {
        return (Liveness::Gone, 0);
    }
    if healthy {
        return (Liveness::Alive, 0);
    }
    let polls = unhealthy_polls + 1;
    if polls >= UNRESPONSIVE_POLLS {
        (Liveness::Unresponsive, polls)
    } else {
        (Liveness::Alive, polls)
    }
}

impl DeathInfo {
    /// `engine-exited(signal N)` / `engine-exited(code N)`, matching the
    /// naming the shell log uses for every other exit trigger, so a reader
    /// can tell a killed engine from one that exited on its own.
    fn exit_reason(&self) -> String {
        if let Some(polls) = self.unresponsive_polls {
            return format!(
                "engine-unresponsive({polls} failed health polls over {}s; stopped by the shell)",
                u64::from(polls) * POLL_INTERVAL.as_secs()
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
    use std::os::unix::process::ExitStatusExt;
    let lock_path = launch::lock_path(&guard.paths.data_dir);
    let lock_holder = launch::read_lock_fields(&lock_path);
    let prior_polls = guard.unhealthy_polls;
    match guard.supervised.as_mut() {
        Some(Supervised::Spawned(engine)) => {
            let (pid, port) = (engine.pid(), engine.port());
            let death = |exit_code, signal, unresponsive_polls| DeathInfo {
                exit_code,
                signal,
                lock_pid: lock_holder.map(|(pid, _)| pid).or(Some(pid)),
                lock_port: lock_holder.map(|(_, port)| port).or(Some(port)),
                unresponsive_polls,
            };
            if let Some(status) = engine.try_reap() {
                return Some(death(status.code(), status.signal(), None));
            }
            if engine.is_log_failed() {
                return Some(death(Some(1), None, None));
            }
            let healthy = engine::health_ok(port);
            let alive = healthy || launch::pid_can_act(pid);
            let (verdict, polls) = judge_liveness(alive, healthy, prior_polls);
            guard.unhealthy_polls = polls;
            match verdict {
                Liveness::Alive => {
                    if polls > 0 {
                        engine::append_shell_log(
                            "WARN",
                            &format!(
                                "engine pid {pid} missed health poll {polls}/{UNRESPONSIVE_POLLS} \
                                 (process alive, not declared dead)"
                            ),
                        );
                    }
                    None
                }
                Liveness::Gone => Some(death(Some(1), None, None)),
                Liveness::Unresponsive => {
                    let Some(Supervised::Spawned(engine)) = guard.supervised.as_mut() else {
                        unreachable!("matched Spawned above");
                    };
                    engine::append_shell_log(
                        "WARN",
                        &format!(
                            "engine pid {pid} alive but unresponsive for {polls} polls: stopping its process group"
                        ),
                    );
                    engine.shutdown();
                    let status = engine.try_reap();
                    Some(death(
                        status.and_then(|s| s.code()),
                        status.and_then(|s| s.signal()),
                        Some(polls),
                    ))
                }
            }
        }
        Some(Supervised::Adopted { pid, port, .. }) => {
            let (pid, port) = (*pid, *port);
            let healthy = engine::health_ok(port);
            let alive = healthy || launch::pid_can_act(pid);
            let (verdict, polls) = judge_liveness(alive, healthy, prior_polls);
            guard.unhealthy_polls = polls;
            let death = |unresponsive_polls| DeathInfo {
                exit_code: Some(1),
                signal: None,
                lock_pid: Some(pid),
                lock_port: Some(port),
                unresponsive_polls,
            };
            match verdict {
                Liveness::Alive => None,
                Liveness::Gone => Some(death(None)),
                Liveness::Unresponsive => {
                    engine::append_shell_log(
                        "WARN",
                        &format!(
                            "adopted engine pid {pid} alive but unresponsive for {polls} polls: stopping it"
                        ),
                    );
                    launch::stop_holder_pid(pid);
                    Some(death(Some(polls)))
                }
            }
        }
        None => None,
    }
}

fn reap_child(guard: &mut RuntimeState) {
    guard.phase = SupervisorPhase::Reaping;
    guard.unhealthy_polls = 0;
    if let Some(Supervised::Spawned(engine)) = guard.supervised.as_mut() {
        // `shutdown`, never an unbounded wait: it reaps an exited engine
        // (sweeping the workers it left in its group) and, should a death
        // ever be declared over a live process again, stops it within ~5s
        // instead of parking this thread on `wait()` forever.
        engine.shutdown();
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
    if let Some(mut old) = guard.supervised.take() {
        old.shutdown();
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
            guard.phase = SupervisorPhase::Running;
            guard.dead_at = None;
            guard.auto_restart_attempted = false;
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
        Ok(engine) => engine,
        Err(err) => {
            engine::append_shell_log("ERROR", &format!("restart failed: {err}"));
            guard.phase = SupervisorPhase::Fatal;
            update_surfaces(app, supervisor, &guard);
            return false;
        }
    };
    match running.wait_until_healthy(engine::BOOT_TIMEOUT) {
        Ok(()) => {
            guard.supervised = Some(Supervised::Spawned(running));
            guard.phase = SupervisorPhase::Running;
            guard.dead_at = None;
            guard.auto_restart_attempted = false;
            log_restart_success(exit_code);
            let origin = guard.supervised.as_ref().expect("spawned").origin();
            update_surfaces(app, supervisor, &guard);
            navigate_to_origin(app, &origin, &product_name);
            true
        }
        Err(err) => {
            running.shutdown();
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

/// The fatal screen's URL. Everything the page needs travels IN it: a global
/// eval'd before `navigate` belongs to the old document and is gone by the time
/// the fatal page runs, which left Relaunch with no health port every time.
fn fatal_query(exit_code: i32, pid: u32, port: u16, health_port: u16) -> String {
    format!("index.html?fatal=1&exit={exit_code}&pid={pid}&port={port}&health={health_port}")
}

fn navigate_fatal_bootstrap(app: &AppHandle, guard: &RuntimeState, health_port: u16) {
    let exit_code = guard.exit_code.unwrap_or(-1);
    let pid = guard.lock_pid.unwrap_or(0);
    let port = guard.lock_port.unwrap_or(0);
    let product = guard.paths.product_name.clone();
    let query = fatal_query(exit_code, pid, port, health_port);
    let handle = app.clone();
    let _ = handle.clone().run_on_main_thread(move || {
        if let Some(window) = handle.get_webview_window(WINDOW_LABEL) {
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
    use std::os::unix::process::CommandExt;

    #[test]
    fn fatal_url_carries_the_health_port() {
        let q = fatal_query(1, 4242, 8685, 51999);
        assert!(q.contains("&health=51999"), "{q}");
        assert!(q.starts_with("index.html?fatal=1&"), "{q}");
    }

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
            unresponsive_polls: None,
        }
    }

    // - if one failed health probe on a live engine is declared a death then
    //   a box in swap shows the fatal screen over an engine that is serving
    //   (demon-llama, Thu 1 Oct 2026: "engine-exited(code 1)" for pid 86496,
    //   still answering five minutes later) -> broken
    // - if reaping a declared-dead engine can block on a live child then the
    //   poll thread parks forever and Relaunch returns 202 into a void
    //   -> broken
    // - if a shell-initiated stop is logged as the engine's own exit then the
    //   log cannot say why the engine went away -> broken

    #[test]
    fn one_failed_probe_on_a_live_engine_is_not_a_death() {
        assert_eq!(judge_liveness(true, false, 0), (Liveness::Alive, 1));
    }

    #[test]
    fn a_live_engine_is_declared_hung_only_after_the_full_budget() {
        let mut polls = 0;
        for _ in 1..UNRESPONSIVE_POLLS {
            let (verdict, next) = judge_liveness(true, false, polls);
            assert_eq!(verdict, Liveness::Alive);
            polls = next;
        }
        assert_eq!(
            judge_liveness(true, false, polls),
            (Liveness::Unresponsive, UNRESPONSIVE_POLLS)
        );
    }

    #[test]
    fn a_healthy_probe_resets_the_count_and_a_vanished_pid_is_gone_at_once() {
        assert_eq!(judge_liveness(true, true, 5), (Liveness::Alive, 0));
        assert_eq!(judge_liveness(false, false, 0), (Liveness::Gone, 0));
    }

    #[test]
    fn a_shell_stop_is_named_as_unresponsive_not_as_an_exit_code() {
        let mut info = death(Some(1), None);
        info.unresponsive_polls = Some(6);
        assert_eq!(
            info.exit_reason(),
            "engine-unresponsive(6 failed health polls over 30s; stopped by the shell)"
        );
    }

    #[test]
    fn relaunch_applies_in_every_post_death_phase_and_never_to_a_running_engine() {
        for phase in [
            SupervisorPhase::Dead,
            SupervisorPhase::Fatal,
            SupervisorPhase::Restarting,
            SupervisorPhase::AwaitingRelaunch,
            SupervisorPhase::AwaitingDiskSpace,
        ] {
            assert!(relaunch_applies(phase), "{phase:?}");
        }
        assert!(!relaunch_applies(SupervisorPhase::Running));
    }

    #[test]
    fn reaping_a_declared_dead_engine_that_is_still_alive_is_bounded() {
        let directory =
            std::env::temp_dir().join(format!("opendj-reap-bounded-{}", std::process::id()));
        std::fs::create_dir_all(&directory).unwrap();
        let child = std::process::Command::new("/bin/sleep")
            .arg("120")
            .stdin(std::process::Stdio::null())
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .process_group(0)
            .spawn()
            .unwrap();
        let pid = child.id();
        let mut state = RuntimeState {
            supervised: Some(Supervised::Spawned(engine::Engine::for_test(
                child, &directory,
            ))),
            phase: SupervisorPhase::Dead,
            dead_at: None,
            exit_code: Some(1),
            lock_pid: None,
            lock_port: None,
            auto_restart_attempted: false,
            unhealthy_polls: 0,
            paths: SupervisorPaths {
                payload: directory.clone(),
                data_dir: directory.clone(),
                log_path: directory.join("engine.log"),
                product_name: "test".into(),
            },
        };
        let (done_tx, done_rx) = std::sync::mpsc::channel();
        thread::spawn(move || {
            reap_child(&mut state);
            let _ = done_tx.send(());
            // Keep the engine out of Drop's own shutdown on this thread.
            std::mem::forget(state);
        });

        let finished = done_rx.recv_timeout(Duration::from_secs(15)).is_ok();
        if !finished {
            unsafe {
                libc::killpg(pid as i32, libc::SIGKILL);
            }
        }
        let _ = std::fs::remove_dir_all(&directory);
        assert!(
            finished,
            "reap_child blocked on a live child: the poll thread would park forever"
        );
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
}
