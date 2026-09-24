//! odj-audio command line.
//!
//!   odj-audio render --plan PLAN.json --out OUT.wav
//!   odj-audio serve [--clock fake|wall|device] [--sample-rate 48000] [--block 256] [--record OUT.wav]
//!   odj-audio version
//!
//! `render` prints one JSON summary line. `serve` speaks protocol v1 on
//! stdin/stdout; see `src/protocol.rs`.

use std::fs::File;
use std::io::{self, BufWriter};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use odj_audio::offline::{file_loader, render_plan_files};
use odj_audio::plan::parse_plan;
use odj_audio::{protocol, serve, wav};
use serde_json::json;

const USAGE: &str = "usage:
  odj-audio render --plan PLAN.json --out OUT.wav
  odj-audio serve [--clock fake|wall|device] [--sample-rate HZ] [--block FRAMES] [--record OUT.wav]
  odj-audio version";

struct Args {
    rest: Vec<String>,
}

impl Args {
    fn take(&mut self, flag: &str) -> Result<Option<String>, String> {
        match self.rest.iter().position(|a| a == flag) {
            None => Ok(None),
            Some(i) => {
                if i + 1 >= self.rest.len() {
                    return Err(format!("{flag} needs a value"));
                }
                let v = self.rest.remove(i + 1);
                self.rest.remove(i);
                Ok(Some(v))
            }
        }
    }

    fn done(&self) -> Result<(), String> {
        match self.rest.first() {
            Some(a) => Err(format!("unexpected argument {a}")),
            None => Ok(()),
        }
    }
}

fn fail(msg: impl std::fmt::Display) -> ExitCode {
    eprintln!("odj-audio: {msg}");
    ExitCode::from(2)
}

fn render(mut args: Args) -> Result<(), String> {
    let plan_path = PathBuf::from(args.take("--plan")?.ok_or("render needs --plan")?);
    let out_path = PathBuf::from(args.take("--out")?.ok_or("render needs --out")?);
    args.done()?;
    let text = std::fs::read_to_string(&plan_path).map_err(|e| format!("cannot read {}: {e}", plan_path.display()))?;
    let value: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("{} is not JSON: {e}", plan_path.display()))?;
    let plan = parse_plan(&value).map_err(|e| format!("{}: {}", e.code.as_str(), e.message))?;
    let base = plan_path.parent().map(Path::to_path_buf).unwrap_or_default();
    let out = render_plan_files(&plan, &base).map_err(|e| format!("{}: {}", e.code.as_str(), e.message))?;
    let file = File::create(&out_path).map_err(|e| format!("cannot create {}: {e}", out_path.display()))?;
    let mut w = BufWriter::new(file);
    wav::write_f32(&mut w, out.sample_rate, &out.pcm).map_err(|e| e.to_string())?;
    let fired: Vec<_> = out.fired.iter().map(|f| json!({"event": f.event, "frame": f.frame})).collect();
    let summary = json!({
        "type": "render",
        "out": out_path.display().to_string(),
        "sample_rate": out.sample_rate,
        "frames": out.frames,
        "duration_ms": out.frames as f64 * 1000.0 / out.sample_rate as f64,
        "sha256": out.sha256,
        "decode_wall_s": out.decode_wall_s,
        "render_wall_s": out.render_wall_s,
        "realtime_factor": out.realtime_factor(),
        "fired": fired,
    });
    println!("{summary}");
    Ok(())
}

fn serve_cmd(mut args: Args) -> Result<(), String> {
    let clock = args.take("--clock")?.unwrap_or_else(|| "fake".into());
    let sr: Option<u32> = match args.take("--sample-rate")? {
        None => None,
        Some(s) => Some(
            s.parse()
                .ok()
                .filter(|r| (8000..=384000).contains(r))
                .ok_or("--sample-rate must be 8000..384000")?,
        ),
    };
    let block: usize = match args.take("--block")? {
        None => 256,
        Some(s) => s
            .parse()
            .ok()
            .filter(|b| (16..=odj_audio::engine::MAX_BLOCK).contains(b))
            .ok_or(format!("--block must be 16..{}", odj_audio::engine::MAX_BLOCK))?,
    };
    let record = args.take("--record")?.map(PathBuf::from);
    args.done()?;
    if record.is_some() && clock != "fake" {
        return Err("--record works on the fake clock only".into());
    }
    match clock.as_str() {
        "fake" => {
            let sr = sr.unwrap_or(48000);
            let mut rec = Vec::new();
            let loader = file_loader(std::env::current_dir().map_err(|e| e.to_string())?);
            let sink = if record.is_some() { Some(&mut rec) } else { None };
            serve::serve_fake(io::stdin().lock(), io::stdout().lock(), sr, loader, sink).map_err(|e| e.to_string())?;
            if let Some(p) = record {
                let file = File::create(&p).map_err(|e| format!("cannot create {}: {e}", p.display()))?;
                wav::write_f32(&mut BufWriter::new(file), sr, &rec).map_err(|e| e.to_string())?;
            }
            Ok(())
        }
        "wall" => serve::serve_threaded(sr.unwrap_or(48000), "wall", serve::run_wall(block)).map_err(|e| e.to_string()),
        "device" => device(sr),
        other => Err(format!("unknown clock {other}; use fake, wall or device")),
    }
}

#[cfg(feature = "device")]
fn device(sr: Option<u32>) -> Result<(), String> {
    let (rate, _channels) = odj_audio::device::default_output_format()?;
    if let Some(want) = sr {
        if want != rate {
            return Err(format!("the output device runs at {rate} Hz; --sample-rate {want} does not match"));
        }
    }
    serve::serve_threaded(rate, "device", odj_audio::device::run_device()).map_err(|e| e.to_string())
}

#[cfg(not(feature = "device"))]
fn device(_sr: Option<u32>) -> Result<(), String> {
    Err("this build has no device output; rebuild with --features device".into())
}

fn main() -> ExitCode {
    let mut argv: Vec<String> = std::env::args().skip(1).collect();
    if argv.is_empty() {
        return fail(USAGE);
    }
    let sub = argv.remove(0);
    let args = Args { rest: argv };
    let r = match sub.as_str() {
        "render" => render(args),
        "serve" => serve_cmd(args),
        "version" => {
            let v = json!({"engine": concat!("odj-audio ", env!("CARGO_PKG_VERSION")), "protocol": protocol::PROTOCOL_VERSION});
            println!("{v}");
            Ok(())
        }
        "-h" | "--help" | "help" => {
            println!("{USAGE}");
            Ok(())
        }
        other => Err(format!("unknown command {other}\n{USAGE}")),
    };
    match r {
        Ok(()) => ExitCode::SUCCESS,
        Err(e) => fail(e),
    }
}
