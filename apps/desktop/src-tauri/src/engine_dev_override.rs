//! Dev and preview override for which engine payload directory the shell spawns.
//!
//! Preview builds can persist a checkout path under the app data directory.
//! When that directory is gone, the shell must stop before spawn, show the path
//! and the setting name, and offer a reset back to the bundled payload.

use std::path::{Path, PathBuf};

use serde::Deserialize;

use crate::engine;

/// JSON file in the shell data directory (`engine_dev_override.json`).
pub const OVERRIDE_FILE: &str = "engine-dev-override.json";

/// Key inside the JSON file and the label shown on the fatal bootstrap page.
pub const ENGINE_SOURCE_CONFIG_KEY: &str = "engine_source_directory";

/// Unpackaged/dev seam matching the Electron shell (`OPENDJ_PAYLOAD_DIR`).
pub const PAYLOAD_DIR_ENV: &str = "OPENDJ_PAYLOAD_DIR";

const RESET_BUNDLED_PAYLOAD_LABEL: &str = "Reset to bundled payload";

#[derive(Debug, Deserialize)]
struct OverrideFile {
    engine_source_directory: String,
}

/// The configured engine source directory, if any override is set.
pub fn configured_engine_source_directory(data_dir: &Path) -> Option<PathBuf> {
    if let Some(path) = payload_dir_from_env() {
        return Some(path);
    }
    read_override_file(data_dir).ok().flatten()
}

#[cfg(debug_assertions)]
fn payload_dir_from_env() -> Option<PathBuf> {
    let raw = std::env::var(PAYLOAD_DIR_ENV).ok()?;
    let trimmed = raw.trim();
    if trimmed.is_empty() {
        return None;
    }
    Some(PathBuf::from(trimmed))
}

#[cfg(not(debug_assertions))]
fn payload_dir_from_env() -> Option<PathBuf> {
    None
}

/// Which config key named the active override (for fatal copy).
pub fn active_engine_source_config_key(_data_dir: &Path) -> &'static str {
    if payload_dir_from_env().is_some() {
        PAYLOAD_DIR_ENV
    } else {
        ENGINE_SOURCE_CONFIG_KEY
    }
}

fn read_override_file(data_dir: &Path) -> Result<Option<PathBuf>, String> {
    let path = data_dir.join(OVERRIDE_FILE);
    if !path.is_file() {
        return Ok(None);
    }
    let raw = std::fs::read_to_string(&path)
        .map_err(|err| format!("reading {} failed: {err}", path.display()))?;
    let parsed: OverrideFile = serde_json::from_str(&raw).map_err(|err| {
        format!(
            "{} is not valid JSON for the engine source override: {err}",
            path.display()
        )
    })?;
    let directory = parsed.engine_source_directory.trim();
    if directory.is_empty() {
        return Err(format!(
            "{} sets {ENGINE_SOURCE_CONFIG_KEY} but the value is empty",
            path.display()
        ));
    }
    Ok(Some(PathBuf::from(directory)))
}

/// Remove a persisted override so the next launch uses the bundled payload.
pub fn clear_configured_engine_source(data_dir: &Path) -> Result<(), String> {
    let path = data_dir.join(OVERRIDE_FILE);
    if path.is_file() {
        std::fs::remove_file(&path)
            .map_err(|err| format!("removing {} failed: {err}", path.display()))?;
    }
    Ok(())
}

/// Bootstrap query for the missing-engine-source fatal page.
pub fn missing_engine_source_bootstrap_query(
    missing: &engine::MissingEngineSource,
    health_port: u16,
) -> String {
    let path = percent_encode_path(&missing.path);
    let key = percent_encode_path(Path::new(missing.config_key));
    format!(
        "index.html?missing-engine-source=1&path={path}&key={key}&health={health_port}"
    )
}

fn percent_encode_path(path: &Path) -> String {
    let mut out = String::new();
    for byte in path.to_string_lossy().bytes() {
        match byte {
            b'A'..=b'Z' | b'a'..=b'z' | b'0'..=b'9' | b'-' | b'.' | b'_' | b'~' | b'/' => {
                out.push(byte as char);
            }
            _ => out.push_str(&format!("%{byte:02X}")),
        }
    }
    out
}

/// Text the bootstrap page renders for a missing override (Rust unit snapshot).
pub fn missing_engine_source_fatal_page_text(
    missing: &engine::MissingEngineSource,
) -> String {
    format!(
        "Open DJ cannot start\n\n\
         The configured engine source directory does not exist:\n{}\n\n\
         Setting: {}\n\n\
         {RESET_BUNDLED_PAYLOAD_LABEL} clears the override and restarts with the \
         bundled engine payload.",
        missing.path.display(),
        missing.config_key,
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicUsize, Ordering};

    static TMP_COUNTER: AtomicUsize = AtomicUsize::new(0);

    fn temp_data_dir() -> PathBuf {
        let n = TMP_COUNTER.fetch_add(1, Ordering::Relaxed);
        let dir = std::env::temp_dir().join(format!(
            "opendj-engine-dev-override-{}-{n}",
            std::process::id()
        ));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).expect("mkdir");
        dir
    }

    fn write_override(data_dir: &Path, directory: &str) {
        let body = format!(
            r#"{{"engine_source_directory":{}}}"#,
            serde_json::to_string(directory).expect("encode")
        );
        std::fs::write(data_dir.join(OVERRIDE_FILE), body).expect("write override");
    }

    #[test]
    fn nonexistent_override_returns_error_before_spawn() {
        let bundled = temp_data_dir().join("bundled");
        std::fs::create_dir_all(&bundled).expect("mkdir bundled");
        let missing = bundled.join("definitely-not-there-wt");
        let err = engine::resolve_engine_payload_dir(
            &bundled,
            Some(missing.as_path()),
            ENGINE_SOURCE_CONFIG_KEY,
        )
        .expect_err("missing override must refuse before spawn");
        assert_eq!(err.config_key, ENGINE_SOURCE_CONFIG_KEY);
        assert_eq!(err.path, missing);
    }

    #[test]
    fn missing_engine_source_fatal_page_names_path_and_key() {
        let missing = engine::MissingEngineSource {
            path: PathBuf::from("/tmp/music-dj-tools-wt-p0-audio"),
            config_key: ENGINE_SOURCE_CONFIG_KEY,
        };
        let text = missing_engine_source_fatal_page_text(&missing);
        assert!(
            text.contains("/tmp/music-dj-tools-wt-p0-audio"),
            "fatal page must name the path: {text}"
        );
        assert!(
            text.contains(ENGINE_SOURCE_CONFIG_KEY),
            "fatal page must name the config key: {text}"
        );
        assert!(
            text.contains(RESET_BUNDLED_PAYLOAD_LABEL),
            "fatal page must offer reset: {text}"
        );
    }

    #[test]
    fn reset_to_bundled_payload_clears_override_and_next_resolve_uses_bundled() {
        let data_dir = temp_data_dir();
        let bundled = data_dir.join("bundled-payload");
        std::fs::create_dir_all(&bundled).expect("mkdir bundled");
        write_override(&data_dir, "/gone/worktree");
        assert!(configured_engine_source_directory(&data_dir).is_some());
        clear_configured_engine_source(&data_dir).expect("clear");
        assert!(configured_engine_source_directory(&data_dir).is_none());
        let resolved = engine::resolve_engine_payload_dir(
            &bundled,
            configured_engine_source_directory(&data_dir).as_deref(),
            ENGINE_SOURCE_CONFIG_KEY,
        )
        .expect("resolve bundled");
        assert_eq!(resolved, bundled);
        let _ = std::fs::remove_dir_all(&data_dir);
    }

    #[test]
    fn no_override_startup_uses_bundled_payload_unchanged() {
        let data_dir = temp_data_dir();
        let bundled = data_dir.join("bundled");
        std::fs::create_dir_all(&bundled).expect("mkdir");
        let resolved = engine::resolve_engine_payload_dir(
            &bundled,
            configured_engine_source_directory(&data_dir).as_deref(),
            ENGINE_SOURCE_CONFIG_KEY,
        )
        .expect("resolve");
        assert_eq!(resolved, bundled);
        let _ = std::fs::remove_dir_all(&data_dir);
    }

    #[test]
    fn valid_override_directory_resolves_to_existing_payload_tree() {
        let data_dir = temp_data_dir();
        let checkout = data_dir.join("checkout");
        let payload = checkout.join("apps/desktop/src-tauri/payload");
        std::fs::create_dir_all(payload.join("bin")).expect("mkdir");
        std::fs::write(payload.join("bin").join("opendj-engine"), b"#!/bin/sh\n")
            .expect("launcher");
        write_override(&data_dir, checkout.to_string_lossy().as_ref());
        let bundled = data_dir.join("bundled");
        std::fs::create_dir_all(&bundled).expect("mkdir bundled");
        let resolved = engine::resolve_engine_payload_dir(
            &bundled,
            configured_engine_source_directory(&data_dir).as_deref(),
            ENGINE_SOURCE_CONFIG_KEY,
        )
        .expect("valid override");
        assert_eq!(resolved, payload);
        let _ = std::fs::remove_dir_all(&data_dir);
    }
}
