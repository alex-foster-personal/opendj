//! Day-based engine log rotation shared by the shell and tests.

use std::io::Write;
use std::path::{Path, PathBuf};
use std::time::{Duration, SystemTime, UNIX_EPOCH};

pub const ENGINE_LOG_MAX_BYTES: u64 = 5 * 1024 * 1024;
pub const ENGINE_LOG_RETENTION_DAYS: u64 = 7;
pub const ENGINE_LOG_MAX_TOTAL_ARCHIVE_BYTES: u64 = 2 * 1024 * 1024 * 1024;

fn archive_timestamp() -> String {
    let total_secs = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .expect("system clock is before Unix epoch")
        .as_secs();
    let (year, month, day, hour, minute, second) = utc_parts(total_secs);
    format!(
        "{year:04}{month:02}{day:02}T{hour:02}{minute:02}{second:02}Z"
    )
}

fn utc_parts(total_secs: u64) -> (u64, u64, u64, u64, u64, u64) {
    let days = total_secs / 86_400;
    let time = total_secs % 86_400;
    let hour = time / 3_600;
    let minute = (time % 3_600) / 60;
    let second = time % 60;
    let (year, month, day) = civil_from_days(days);
    (year, month, day, hour, minute, second)
}

fn civil_from_days(days: u64) -> (u64, u64, u64) {
    let z = days + 719_468;
    let era = z / 146_097;
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1_460 + doe / 36_524 - doe / 146_096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    let year = y + if month <= 2 { 1 } else { 0 };
    (year, month, day)
}

fn archive_paths(log_path: &Path) -> Vec<PathBuf> {
    let parent = log_path.parent().unwrap_or_else(|| Path::new("."));
    let base = log_path
        .file_name()
        .map(|name| name.to_string_lossy().to_string())
        .unwrap_or_default();
    let mut archives = Vec::new();
    if let Ok(entries) = std::fs::read_dir(parent) {
        for entry in entries.flatten() {
            let path = entry.path();
            if path == log_path {
                continue;
            }
            let file_name = path
                .file_name()
                .map(|name| name.to_string_lossy().to_string())
                .unwrap_or_default();
            if file_name.starts_with(&format!("{base}.")) {
                archives.push(path);
            }
        }
    }
    archives
}

fn prune_archives(log_path: &Path) -> std::io::Result<()> {
    let cutoff = SystemTime::now()
        .checked_sub(Duration::from_secs(ENGINE_LOG_RETENTION_DAYS * 24 * 60 * 60))
        .unwrap_or(UNIX_EPOCH);
    for archive in archive_paths(log_path) {
        let modified = std::fs::metadata(&archive)?.modified()?;
        if modified < cutoff {
            std::fs::remove_file(&archive)?;
        }
    }
    let mut archives = archive_paths(log_path);
    let mut total: u64 = archives
        .iter()
        .filter_map(|path| std::fs::metadata(path).map(|meta| meta.len()).ok())
        .sum();
    while total > ENGINE_LOG_MAX_TOTAL_ARCHIVE_BYTES && !archives.is_empty() {
        let oldest = archives
            .iter()
            .min_by_key(|path| {
                std::fs::metadata(path)
                    .and_then(|meta| meta.modified())
                    .unwrap_or(UNIX_EPOCH)
            })
            .cloned()
            .expect("archives non-empty");
        let size = std::fs::metadata(&oldest)?.len();
        std::fs::remove_file(&oldest)?;
        total -= size;
        archives = archive_paths(log_path);
    }
    Ok(())
}

/// Rotate the live log when it reaches ``max_bytes`` or when forced with ``0``.
pub fn rotate_log_if_needed(log_path: &Path, max_bytes: u64) -> std::io::Result<()> {
    let metadata = match std::fs::metadata(log_path) {
        Ok(metadata) => metadata,
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => return Ok(()),
        Err(error) => return Err(error),
    };
    if metadata.len() < max_bytes {
        return Ok(());
    }
    let archive = log_path.with_file_name(format!(
        "{}.{}",
        log_path
            .file_name()
            .map(|name| name.to_string_lossy())
            .unwrap_or_default(),
        archive_timestamp()
    ));
    std::fs::rename(log_path, archive)?;
    prune_archives(log_path)
}

fn open_append(log_path: &Path) -> std::io::Result<std::fs::File> {
    std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path)
}

/// Append bytes, rotating first when the live file would exceed the cap.
pub fn append_rotated(log_path: &Path, bytes: &[u8]) -> std::io::Result<()> {
    let existing_size = match std::fs::metadata(log_path) {
        Ok(metadata) => metadata.len(),
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => 0,
        Err(error) => return Err(error),
    };
    if existing_size > 0 && existing_size + bytes.len() as u64 > ENGINE_LOG_MAX_BYTES {
        rotate_log_if_needed(log_path, 0)?;
    }
    open_append(log_path)?.write_all(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::SystemTime;

    fn scratch_dir(label: &str) -> PathBuf {
        std::env::temp_dir().join(format!(
            "opendj-engine-log-{}-{}",
            label,
            SystemTime::now()
                .duration_since(UNIX_EPOCH)
                .unwrap()
                .as_nanos()
        ))
    }

    fn set_mtime(path: &Path, when: SystemTime) {
        let file = std::fs::OpenOptions::new()
            .write(true)
            .open(path)
            .unwrap();
        file.set_modified(when).unwrap();
    }

    #[test]
    fn rotates_full_log_into_timestamped_archive() {
        let directory = scratch_dir("rotate");
        std::fs::create_dir_all(&directory).unwrap();
        let log_path = directory.join("engine.log");
        std::fs::write(&log_path, "current").unwrap();

        rotate_log_if_needed(&log_path, 1).unwrap();

        assert!(!log_path.exists());
        let archives = archive_paths(&directory.join("engine.log"));
        assert_eq!(archives.len(), 1);
        assert_eq!(std::fs::read_to_string(&archives[0]).unwrap(), "current");
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn removes_archives_older_than_retention() {
        let directory = scratch_dir("prune-old");
        std::fs::create_dir_all(&directory).unwrap();
        let log_path = directory.join("engine.log");
        let old_archive = directory.join("engine.log.20260101T000000Z");
        let recent_archive = directory.join("engine.log.20260901T000000Z");
        std::fs::write(&old_archive, "old").unwrap();
        std::fs::write(&recent_archive, "recent").unwrap();
        let eight_days = Duration::from_secs(8 * 24 * 60 * 60);
        set_mtime(&old_archive, SystemTime::now() - eight_days);
        set_mtime(&recent_archive, SystemTime::now() - Duration::from_secs(60 * 60));

        prune_archives(&log_path).unwrap();

        assert!(!old_archive.exists());
        assert!(recent_archive.exists());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn keeps_more_than_five_recent_archives() {
        let directory = scratch_dir("keep-many");
        std::fs::create_dir_all(&directory).unwrap();
        let log_path = directory.join("engine.log");
        for index in 0..6 {
            let archive = directory.join(format!("engine.log.2026090{index}T000000Z"));
            std::fs::write(&archive, format!("archive-{index}")).unwrap();
            set_mtime(&archive, SystemTime::now() - Duration::from_secs(60 * (index + 1)));
        }
        std::fs::write(&log_path, "live").unwrap();

        rotate_log_if_needed(&log_path, 1).unwrap();

        let archives = archive_paths(&log_path);
        assert!(archives.len() >= 6, "expected at least six archives, got {}", archives.len());
        std::fs::remove_dir_all(directory).unwrap();
    }

    #[test]
    fn removes_numbered_legacy_archive_when_old() {
        let directory = scratch_dir("legacy-numbered");
        std::fs::create_dir_all(&directory).unwrap();
        let log_path = directory.join("engine.log");
        let legacy = directory.join("engine.log.5");
        std::fs::write(&legacy, "legacy").unwrap();
        set_mtime(
            &legacy,
            SystemTime::now() - Duration::from_secs(8 * 24 * 60 * 60),
        );

        prune_archives(&log_path).unwrap();

        assert!(!legacy.exists());
        std::fs::remove_dir_all(directory).unwrap();
    }
}
