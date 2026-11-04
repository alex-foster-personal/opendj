//! odj-live: live key, tuning and BPM frames as JSON lines.
//!
//!   odj-live --file track.wav [--hz 10] [--summary]
//!   odj-live --stdin-f32 --rate 48000 [--hz 10] [--summary]
//!
//! Prints one `{"type":"frame",...}` object per line as frames are made,
//! then one `{"type":"summary",...}` line at the end of the file or of
//! stdin. `--summary` prints the summary line only. `--stdin-f32` reads raw
//! little-endian f32 mono samples at `--rate` Hz until EOF.
//!
//! Memory: `--stdin-f32` may run all day, so its summary keeps only the last
//! 30 minutes (`STREAM_HISTORY_S`) of chroma and onsets: past that, the
//! summary's `window_start_s` says where the window starts and its key, BPM,
//! mean chroma, segments and `duration_s` describe that window. Frames are
//! unaffected. `--file` summarizes the whole file.
//!
//! Exit status: 0 on success, 2 for a usage error, 1 when the audio cannot
//! be read or decoded (the reason on stderr).

use std::io::{self, BufWriter, Read, Write};
use std::path::PathBuf;
use std::process::ExitCode;

use clap::Parser;
use odj_live_core::{analyze_file_with, Analyzer, Frame, Summary, check_hz, check_rate};

const USAGE: &str = "usage: odj-live --file PATH [--hz 10] [--summary]\n       odj-live --stdin-f32 --rate HZ [--hz 10] [--summary]";

enum Source {
    File(PathBuf),
    Stdin { rate: u32 },
}

struct Args {
    source: Source,
    hz: f64,
    summary_only: bool,
}

#[derive(Parser)]
#[command(name = "odj-live", disable_help_flag = true, disable_version_flag = true)]
struct Cli {
    #[arg(long, value_name = "PATH")]
    file: Option<PathBuf>,
    #[arg(long)]
    stdin_f32: bool,
    #[arg(long, value_name = "HZ")]
    rate: Option<u32>,
    #[arg(long, default_value_t = 10.0)]
    hz: f64,
    #[arg(long)]
    summary: bool,
    #[arg(short = 'h', long)]
    help: bool,
}

fn args_from_cli(cli: Cli) -> Result<Args, String> {
    if cli.help {
        return Err(String::new());
    }
    // Range errors are usage errors (exit 2), not audio errors found later.
    check_hz(cli.hz).map_err(|e| format!("--hz: {e}"))?;
    if let Some(r) = cli.rate {
        check_rate(r).map_err(|e| format!("--rate: {e}"))?;
    }
    let source = match (cli.file, cli.stdin_f32, cli.rate) {
        (Some(_), true, _) => return Err("--file and --stdin-f32 are exclusive".into()),
        (Some(p), false, None) => Source::File(p),
        (Some(_), false, Some(_)) => return Err("--rate is for --stdin-f32; a file states its own rate".into()),
        (None, true, Some(r)) => Source::Stdin { rate: r },
        (None, true, None) => return Err("--stdin-f32 needs --rate".into()),
        (None, false, _) => return Err("one of --file or --stdin-f32 is required".into()),
    };
    Ok(Args {
        source,
        hz: cli.hz,
        summary_only: cli.summary,
    })
}

fn write_line<T: serde::Serialize, W: Write>(out: &mut W, value: &T) -> io::Result<()> {
    serde_json::to_writer(&mut *out, value).map_err(io::Error::other)?;
    out.write_all(b"\n")
}

fn run(args: Args) -> Result<(), String> {
    let stdout = io::stdout();
    let mut out = BufWriter::new(stdout.lock());
    let io_err = |e: io::Error| format!("cannot write to stdout: {e}");
    match args.source {
        Source::File(path) => {
            let mut err = None;
            let mut summary: Option<Summary> = None;
            analyze_file_with(
                &path,
                args.hz,
                |f: Frame| {
                    if !args.summary_only && err.is_none() {
                        err = write_line(&mut out, &f).err();
                    }
                },
                |s| summary = Some(s),
            )?;
            if let Some(e) = err {
                return Err(io_err(e));
            }
            write_line(&mut out, &summary.expect("a summary follows the frames")).map_err(io_err)?;
        }
        Source::Stdin { rate } => {
            let mut analyzer = Analyzer::new_stream(rate, args.hz)?;
            let mut input = io::stdin().lock();
            let mut buf = vec![0u8; 16384];
            let mut carry: Vec<u8> = Vec::with_capacity(4);
            let mut samples: Vec<f32> = Vec::with_capacity(4096);
            loop {
                let n = match input.read(&mut buf) {
                    Ok(0) => break,
                    Ok(n) => n,
                    Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
                    Err(e) => return Err(format!("cannot read stdin: {e}")),
                };
                carry.extend_from_slice(&buf[..n]);
                let whole = carry.len() / 4 * 4;
                samples.clear();
                samples.extend(carry[..whole].chunks_exact(4).map(|b| f32::from_le_bytes([b[0], b[1], b[2], b[3]])));
                carry.drain(..whole);
                analyzer.push(&samples)?;
                let mut wrote = false;
                while let Some(f) = analyzer.next_frame() {
                    if !args.summary_only {
                        write_line(&mut out, &f).map_err(io_err)?;
                        wrote = true;
                    }
                }
                // Live use: a frame is worth nothing if it waits in a buffer.
                if wrote {
                    out.flush().map_err(io_err)?;
                }
            }
            if !carry.is_empty() {
                return Err(format!(
                    "stdin ended {} byte(s) into a sample; raw f32 is 4 bytes per sample",
                    carry.len()
                ));
            }
            let summary = analyzer.finish()?;
            while let Some(f) = analyzer.next_frame() {
                if !args.summary_only {
                    write_line(&mut out, &f).map_err(io_err)?;
                }
            }
            write_line(&mut out, &summary).map_err(io_err)?;
        }
    }
    out.flush().map_err(io_err)
}

fn main() -> ExitCode {
    let cli = match Cli::try_parse() {
        Ok(c) => c,
        Err(e) => {
            eprintln!("odj-live: {e}");
            eprintln!("{USAGE}");
            return ExitCode::from(2);
        }
    };
    let args = match args_from_cli(cli) {
        Ok(a) => a,
        Err(e) => {
            if !e.is_empty() {
                eprintln!("odj-live: {e}");
            }
            eprintln!("{USAGE}");
            return if e.is_empty() { ExitCode::SUCCESS } else { ExitCode::from(2) };
        }
    };
    match run(args) {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => {
            eprintln!("odj-live: {e}");
            ExitCode::from(1)
        }
    }
}
