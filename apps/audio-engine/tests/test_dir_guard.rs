//! The scratch-directory guard every test here uses: a directory it made is
//! there and writable while the guard lives, and gone once it drops, after a
//! panic too. Before the guard, each `cargo test` run left about 35
//! directories (159 MB) in the runner's temp dir.

mod common;

use std::path::PathBuf;

use common::{temp_dir, TestDir};

/// Makes a guarded directory, puts a file in it, and hands back its path.
fn fill(tag: &str) -> (TestDir, PathBuf) {
    let d = temp_dir(tag);
    std::fs::create_dir_all(d.join("sub")).unwrap();
    std::fs::write(d.join("sub").join("a.wav"), b"pcm").unwrap();
    let path = d.clone();
    (d, path)
}

#[test]
fn a_live_guard_holds_a_writable_directory_a_reaper_can_match() {
    let (d, path) = fill("guard-live");
    assert!(path.is_dir(), "{} was not made", path.display());
    assert_eq!(
        std::fs::read(path.join("sub").join("a.wav")).unwrap(),
        b"pcm"
    );
    // It lives in the temp dir the environment names, under the prefix a
    // host reaper matches for what a killed job left behind.
    assert_eq!(path.parent(), Some(std::env::temp_dir().as_path()));
    let name = path.file_name().unwrap().to_str().unwrap();
    assert!(
        name.starts_with(&format!(
            "odj-audio-test-guard-live-{}-",
            std::process::id()
        )),
        "{name}"
    );
    // Control: two calls with one tag are two directories.
    let other = temp_dir("guard-live");
    assert_ne!(*other, path);
    drop(d);
}

#[test]
fn a_dropped_guard_deletes_its_directory_and_everything_in_it() {
    let (d, path) = fill("guard-drop");
    assert!(path.join("sub").join("a.wav").exists());
    drop(d);
    assert!(!path.exists(), "{} outlived its guard", path.display());
}

#[test]
fn a_guard_dropped_by_a_panic_deletes_its_directory() {
    let made = std::sync::Mutex::new(None);
    let outcome = std::panic::catch_unwind(|| {
        let (_d, path) = fill("guard-panic");
        *made.lock().unwrap() = Some(path);
        panic!("a test failed with its directory still in use");
    });
    assert!(outcome.is_err(), "the closure was meant to panic");
    let path = made
        .into_inner()
        .unwrap()
        .expect("the directory was never made");
    assert!(
        !path.exists(),
        "{} outlived a panicking test",
        path.display()
    );
}

/// Why the guard panics as it drops: its reason names the directory.
fn panic_reason(payload: Box<dyn std::any::Any + Send>) -> String {
    payload
        .downcast_ref::<String>()
        .cloned()
        .or_else(|| payload.downcast_ref::<&str>().map(|s| s.to_string()))
        .unwrap_or_default()
}

#[test]
fn a_guard_that_cannot_delete_its_directory_fails_the_test() {
    let (d, path) = fill("guard-undeletable");
    // Take the directory out from under the guard, so its own delete errors.
    std::fs::remove_dir_all(&path).unwrap();
    let outcome = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| drop(d)));
    let reason = panic_reason(outcome.expect_err("a failed delete passed silently"));
    assert!(
        reason.contains("could not delete test scratch dir")
            && reason.contains(&*path.to_string_lossy()),
        "{reason}"
    );
}

#[test]
fn a_failed_delete_during_a_panic_keeps_the_original_failure() {
    // A second panic while unwinding would abort the whole test binary.
    let outcome = std::panic::catch_unwind(|| {
        let (_d, path) = fill("guard-undeletable-panic");
        std::fs::remove_dir_all(&path).unwrap();
        panic!("the original failure");
    });
    assert_eq!(
        panic_reason(outcome.expect_err("the closure was meant to panic")),
        "the original failure"
    );
}
