//! CoreAudio output-health helper for the Electron shell.
//!
//! `payload/bin/odj-output-probe probe|switch|list` prints one JSON document on
//! stdout and exits 0. The document is whatever `probe_output_health_json` or
//! `switch_output_json` already returns, including the honest `unknown` body
//! when CoreAudio refuses. A usage error exits non-zero and writes the reason
//! to stderr, without opening a device.
//!
//! The health, listing and speaker-link functions live in this crate's library,
//! which the Tauri shell links for the same work in process.

use odj_output_probe::{cue_sink, output_health, speaker_link};
use std::io::{self, Write};
use std::process::ExitCode;

const USAGE: &str = "usage: odj-output-probe <probe|switch|list|reconnect|inputs>\n";

fn main() -> ExitCode {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match dispatch(&args) {
        Ok(body) => {
            let mut out = io::stdout().lock();
            if writeln!(out, "{body}").is_err() {
                return ExitCode::from(1);
            }
            ExitCode::SUCCESS
        }
        Err(reason) => {
            let _ = io::stderr().write_all(reason.as_bytes());
            ExitCode::from(2)
        }
    }
}

/// `probe`, `switch`, `list`, `reconnect`, and `inputs` call the existing health or listing functions.
/// Every other argv is a usage error and does not touch CoreAudio.
fn dispatch(args: &[String]) -> Result<String, String> {
    match args {
        [cmd] if cmd == "probe" => Ok(output_health::probe_output_health_json()),
        [cmd] if cmd == "switch" => Ok(output_health::switch_output_json()),
        [cmd] if cmd == "reconnect" => Ok(speaker_link::reconnect_speaker_json()),
        [cmd] if cmd == "inputs" => Ok(speaker_link::input_devices_json()),
        [cmd] if cmd == "list" => {
            let devices = cue_sink::list_output_devices().map_err(|reason| format!("{reason}\n"))?;
            serde_json::to_string(&cue_sink::devices_json(&devices))
                .map_err(|reason| format!("{reason}\n"))
        }
        [cmd] => Err(format!(
            "unknown command '{cmd}'; expected probe, switch, list, reconnect, or inputs\n{USAGE}"
        )),
        _ => Err(USAGE.to_string()),
    }
}

#[cfg(test)]
mod tests {
    use super::{dispatch, USAGE};

    #[test]
    fn argv_usage_fails_without_opening_a_device() {
        let missing = dispatch(&[]).expect_err("no argument");
        assert!(missing.contains("usage:"), "{missing}");
        assert!(missing.contains("list"), "{missing}");

        let unknown = dispatch(&["nope".into()]).expect_err("unknown command");
        assert!(unknown.contains("unknown command"), "{unknown}");
        assert!(unknown.contains("usage:"), "{unknown}");
        assert!(unknown.contains("probe, switch, list, reconnect, or inputs"), "{unknown}");

        let extra = dispatch(&["probe".into(), "again".into()]).expect_err("extra argument");
        assert!(extra.contains("usage:"), "{extra}");
    }

    #[test]
    fn usage_documents_list_alongside_probe_and_switch() {
        assert!(USAGE.contains("probe|switch|list|reconnect|inputs"), "{USAGE}");
    }
}
