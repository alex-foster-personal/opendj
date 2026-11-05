//! RESTART TO UPDATE from a local build (INSTALL-40).
//!
//! An agent flags a locally built, updater-signed `<App>.app.tar.gz` as ready
//! (`opendj update ready`, engine `POST /api/v1/app-update/ready`). The engine
//! then serves a Tauri updater manifest for it on its own loopback origin. This
//! command points `tauri-plugin-updater` at that manifest and lets the PLUGIN
//! do what it does for a published release: download the archive, verify its
//! minisign signature against the pubkey compiled into this binary
//! (`plugins.updater.pubkey`), and swap the bundle. Nothing here verifies a
//! signature itself, so verification stays inseparable from installation, the
//! rule docs/auto-update.md sets for the release path.
//!
//! What this adds around the plugin, and why:
//! - the manifest must come from loopback and the engine's own route, so a
//!   page cannot point the installer at another host;
//! - the version comparator accepts any version, because a local build keeps
//!   the installed semver; WHICH build installs is pinned by the archive and
//!   its signature;
//! - the running bundle must be a real `.app` (a `cargo tauri dev` binary
//!   would have the plugin replace `target/debug`), checked BEFORE install;
//! - the plugin deletes the previous bundle once the new one is in place, so a
//!   clone of it is kept at `<app cache dir>/previous-app/<App>.app` first.
//!   A failed backup refuses the install.
//!
//! The relaunch is the webview's `relaunch()` (plugin-process), the same call
//! the release apply path makes after `downloadAndInstall`.

use std::path::{Path, PathBuf};
use std::process::Command;

use serde::Serialize;
use tauri::{AppHandle, Manager, Runtime};
use tauri_plugin_updater::{Update, UpdaterExt};
use url::Url;

use crate::engine;

/// The only path the shell accepts a local update manifest from.
pub const MANIFEST_ROUTE: &str = "/api/v1/app-update/manifest.json";
/// Directory under the app cache dir that holds the previous bundle.
pub const BACKUP_DIR_NAME: &str = "previous-app";

#[derive(Debug, Serialize)]
pub struct LocalUpdateInstalled {
    pub version: String,
    pub backup: String,
}

/// The manifest URL, if it is the engine's own route on a loopback origin.
pub fn loopback_manifest_url(raw: &str) -> Result<Url, String> {
    let url = Url::parse(raw).map_err(|err| format!("{raw:?} is not a URL: {err}"))?;
    if url.scheme() != "http" {
        return Err(format!(
            "{raw}: a local update manifest is served by the engine over http on loopback, not {}",
            url.scheme()
        ));
    }
    let loopback = match url.host() {
        Some(url::Host::Ipv4(ip)) => ip.is_loopback(),
        Some(url::Host::Ipv6(ip)) => ip.is_loopback(),
        Some(url::Host::Domain(name)) => name == "localhost",
        None => false,
    };
    if !loopback {
        return Err(format!(
            "{raw}: the update manifest must come from this machine (loopback)"
        ));
    }
    if url.path() != MANIFEST_ROUTE || url.query().is_some() {
        return Err(format!("{raw}: expected the engine's {MANIFEST_ROUTE}"));
    }
    Ok(url)
}

/// `/X/Open DJ.app` for `/X/Open DJ.app/Contents/MacOS/<binary>`, else why not.
pub fn bundle_of_executable(executable: &Path) -> Result<PathBuf, String> {
    let macos = executable.parent();
    let contents = macos.and_then(Path::parent);
    let bundle = contents.and_then(Path::parent);
    match (macos, contents, bundle) {
        (Some(macos), Some(contents), Some(bundle))
            if macos.file_name().is_some_and(|n| n == "MacOS")
                && contents.file_name().is_some_and(|n| n == "Contents")
                && bundle.extension().is_some_and(|e| e == "app") =>
        {
            Ok(bundle.to_path_buf())
        }
        _ => Err(format!(
            "{} is not inside an .app bundle (a dev build?); refusing to install over it",
            executable.display()
        )),
    }
}

/// Read the manifest and download the archive through the plugin, which
/// verifies the signature against the compiled-in pubkey before returning.
pub async fn fetch_verified<R: Runtime>(
    app: &AppHandle<R>,
    manifest: Url,
) -> Result<(Update, Vec<u8>), String> {
    let updater = app
        .updater_builder()
        .endpoints(vec![manifest.clone()])
        .map_err(|err| format!("the updater refused the manifest URL {manifest}: {err}"))?
        .version_comparator(|_running, _offered| true)
        .build()
        .map_err(|err| format!("the updater could not be built: {err}"))?;
    let update = updater
        .check()
        .await
        .map_err(|err| format!("reading the update manifest at {manifest} failed: {err}"))?
        .ok_or_else(|| format!("{manifest} offered no update"))?;
    let bytes = update
        .download(|_, _| {}, || {})
        .await
        .map_err(|err| format!("the update was refused, nothing was installed: {err}"))?;
    Ok((update, bytes))
}

/// Clone the running bundle to `<backup_root>/<App>.app`, replacing the last one.
pub fn backup_bundle(bundle: &Path, backup_root: &Path) -> Result<PathBuf, String> {
    let name = bundle
        .file_name()
        .ok_or_else(|| format!("{} has no file name", bundle.display()))?;
    std::fs::create_dir_all(backup_root)
        .map_err(|err| format!("creating {} failed: {err}", backup_root.display()))?;
    let destination = backup_root.join(name);
    if destination.exists() {
        std::fs::remove_dir_all(&destination).map_err(|err| {
            format!(
                "removing the older backup {} failed: {err}",
                destination.display()
            )
        })?;
    }
    // -c clones on APFS (no extra space until the install replaces the
    // original); -R keeps the bundle's symlinks as symlinks; -p keeps modes.
    // `.status()` blocks until cp exits, so nothing outlives this call.
    let status = Command::new("/bin/cp")
        .args(["-c", "-R", "-p"])
        .arg(bundle)
        .arg(&destination)
        .status()
        .map_err(|err| format!("running /bin/cp for the backup failed: {err}"))?;
    if !status.success() {
        return Err(format!(
            "backing up {} to {} failed ({status}); refusing to install without a backup",
            bundle.display(),
            destination.display()
        ));
    }
    Ok(destination)
}

#[tauri::command]
pub async fn install_local_update(
    app: AppHandle,
    manifest_url: String,
) -> Result<LocalUpdateInstalled, String> {
    let manifest = loopback_manifest_url(&manifest_url)?;
    let (update, bytes) = fetch_verified(&app, manifest).await?;
    let executable = std::env::current_exe()
        .map_err(|err| format!("could not resolve the running executable: {err}"))?;
    let bundle = bundle_of_executable(&executable)?;
    let backup_root = app
        .path()
        .app_cache_dir()
        .map_err(|err| format!("could not resolve the app cache dir for the backup: {err}"))?
        .join(BACKUP_DIR_NAME);
    let backup = backup_bundle(&bundle, &backup_root)?;
    update.install(&bytes).map_err(|err| {
        format!(
            "installing over {} failed: {err}; the previous build is kept at {}",
            bundle.display(),
            backup.display()
        )
    })?;
    engine::append_shell_log(
        "local-update",
        &format!(
            "installed {} over {}; previous bundle kept at {}",
            update.version,
            bundle.display(),
            backup.display()
        ),
    );
    Ok(LocalUpdateInstalled {
        version: update.version.clone(),
        backup: backup.display().to_string(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Read, Write};
    use std::net::TcpListener;
    use std::thread::spawn as start_thread;

    // A throwaway keypair made for this test with `cargo tauri signer generate`
    // (no password); the private half was discarded. FIXTURE_SIG signs exactly
    // FIXTURE_BYTES.
    const TEST_PUBKEY: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IEZBMkM1QTA1Mzk0QjJBRDAKUldUUUtrczVCVm9zK2hnUTNjNVMwK0ZidFMzSUVHU2lPSExrSzkxSXYwcklnS3BBZmtWWHhYdEYK";
    const FIXTURE_BYTES: &[u8] = b"open-dj local update fixture\n";
    const FIXTURE_SIG: &str = "dW50cnVzdGVkIGNvbW1lbnQ6IHNpZ25hdHVyZSBmcm9tIHRhdXJpIHNlY3JldCBrZXkKUlVUUUtrczVCVm9zK2taa013M3d5NDJwbHAxRVc2WTZrUllpNlhHR3lBN3BndGRDUEw0ZGhnLzNxUlR1OXFYS2NCOUdjdzJYa3VaQnc0ODg3cGVaSzVVekFBQjdXbk5TWkFRPQp0cnVzdGVkIGNvbW1lbnQ6IHRpbWVzdGFtcDoxNzkzNjc1NjI1CWZpbGU6Zml4dHVyZS5iaW4KTUVKYVIycDZqNWdaMHFLenA0YXF2aGhKUGxZRnlaMzlCZE9udE1hblV0VEIwV0V6TVJVU0NTckp4OG1Hc1lXT0hQVU16Q3QvMUVUWk1OU1VQdGllQ3c9PQo=";

    #[test]
    fn manifest_url_accepts_only_the_engine_route_on_loopback() {
        assert!(
            loopback_manifest_url("http://127.0.0.1:51817/api/v1/app-update/manifest.json").is_ok()
        );
        assert!(
            loopback_manifest_url("http://localhost:7700/api/v1/app-update/manifest.json").is_ok()
        );
        for refused in [
            "http://example.com/api/v1/app-update/manifest.json",
            "https://127.0.0.1:1/api/v1/app-update/manifest.json",
            "http://127.0.0.1:1/api/v1/update/check",
            "http://127.0.0.1:1/api/v1/app-update/manifest.json?x=1",
            "file:///tmp/manifest.json",
            "not a url",
        ] {
            assert!(
                loopback_manifest_url(refused).is_err(),
                "accepted {refused}"
            );
        }
    }

    #[test]
    fn install_target_must_be_an_app_bundle() {
        assert_eq!(
            bundle_of_executable(Path::new(
                "/Applications/Open DJ.app/Contents/MacOS/opendj-desktop"
            ))
            .unwrap(),
            PathBuf::from("/Applications/Open DJ.app")
        );
        assert!(bundle_of_executable(Path::new("/repo/target/debug/opendj-desktop")).is_err());
        assert!(bundle_of_executable(Path::new("/x/NotApp/Contents/MacOS/bin")).is_err());
    }

    #[cfg(target_os = "macos")]
    #[test]
    fn backup_clones_the_bundle_and_replaces_the_previous_backup() {
        let root = std::env::temp_dir().join(format!("odj-backup-test-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&root);
        let bundle = root.join("Apps/Open DJ.app");
        std::fs::create_dir_all(bundle.join("Contents/MacOS")).unwrap();
        std::fs::write(bundle.join("Contents/MacOS/bin"), b"v1").unwrap();
        let backups = root.join("cache/previous-app");
        let first = backup_bundle(&bundle, &backups).unwrap();
        assert_eq!(
            std::fs::read(first.join("Contents/MacOS/bin")).unwrap(),
            b"v1"
        );
        std::fs::write(bundle.join("Contents/MacOS/bin"), b"v2").unwrap();
        let second = backup_bundle(&bundle, &backups).unwrap();
        assert_eq!(
            std::fs::read(second.join("Contents/MacOS/bin")).unwrap(),
            b"v2"
        );
        std::fs::remove_dir_all(&root).unwrap();
    }

    /// Serve one manifest and one archive body on a loopback port.
    fn serve(archive: &'static [u8]) -> Url {
        let listener = TcpListener::bind("127.0.0.1:0").unwrap();
        let port = listener.local_addr().unwrap().port();
        start_thread(move || {
            for stream in listener.incoming().take(2) {
                let mut stream = stream.unwrap();
                let mut request = [0u8; 4096];
                let read = stream.read(&mut request).unwrap();
                let head = String::from_utf8_lossy(&request[..read]).to_string();
                let body: Vec<u8> = if head.starts_with("GET /api/v1/app-update/manifest.json") {
                    let mut platforms = serde_json::Map::new();
                    platforms.insert(
                        tauri_plugin_updater::target().expect("updater target"),
                        serde_json::json!({
                            "signature": FIXTURE_SIG,
                            "url": format!("http://127.0.0.1:{port}/api/v1/app-update/artifact"),
                        }),
                    );
                    serde_json::json!({ "version": "0.0.1", "platforms": platforms })
                        .to_string()
                        .into_bytes()
                } else {
                    archive.to_vec()
                };
                let header = format!(
                    "HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
                    body.len()
                );
                stream.write_all(header.as_bytes()).unwrap();
                stream.write_all(&body).unwrap();
            }
        });
        loopback_manifest_url(&format!("http://127.0.0.1:{port}{MANIFEST_ROUTE}")).unwrap()
    }

    fn mock_app_with_test_key() -> tauri::App<tauri::test::MockRuntime> {
        let mut context = tauri::test::mock_context(tauri::test::noop_assets());
        context.config_mut().plugins.0.insert(
            "updater".into(),
            serde_json::json!({
                "pubkey": TEST_PUBKEY,
                "endpoints": ["https://example.invalid/latest.json"],
                "dangerousInsecureTransportProtocol": true
            }),
        );
        tauri::test::mock_builder()
            .plugin(tauri_plugin_updater::Builder::new().build())
            .build(context)
            .expect("mock app")
    }

    #[test]
    fn a_correctly_signed_archive_verifies() {
        let app = mock_app_with_test_key();
        let manifest = serve(FIXTURE_BYTES);
        let (_update, bytes) =
            tauri::async_runtime::block_on(fetch_verified(app.handle(), manifest))
                .expect("verified");
        assert_eq!(bytes, FIXTURE_BYTES);
    }

    #[test]
    fn a_tampered_archive_is_refused_before_anything_is_installed() {
        let app = mock_app_with_test_key();
        let manifest = serve(b"open-dj local update fixture, tampered\n");
        let error = tauri::async_runtime::block_on(fetch_verified(app.handle(), manifest))
            .err()
            .expect("a tampered archive must be refused");
        assert!(error.contains("refused, nothing was installed"), "{error}");
        assert!(error.to_lowercase().contains("signature"), "{error}");
    }
}
