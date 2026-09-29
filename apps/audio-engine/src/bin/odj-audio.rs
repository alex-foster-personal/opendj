//! odj-audio command line.
//!
//!   odj-audio render --plan PLAN.json --out OUT.wav [--decks-out DIR]
//!   odj-audio serve [--clock fake|wall|device] [--sample-rate 48000] [--block 256] [--record OUT.wav]
//!   odj-audio version
//!
//! `render` prints one JSON summary line: the plan it rendered, the output's
//! sha256, when each event fired, which decks are heard when (the timeline
//! and its overlaps), and each deck's tempo. `--decks-out` also writes each
//! loaded deck's own audio as `deckN.wav`. `serve` speaks protocol v1 on
//! stdin/stdout; see `src/protocol.rs`.

use std::fs::File;
use std::io::{self, BufWriter};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use odj_audio::offline::{file_loader, render_plan_files_with, RenderOptions, Solo};
use odj_audio::plan::{parse_plan, Action, Plan};
use odj_audio::{protocol, serve, wav};
use serde_json::json;
use sha2::{Digest, Sha256};

const USAGE: &str = "usage:
  odj-audio render --plan PLAN.json --out OUT.wav [--decks-out DIR]
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

/// One spelling per file, for comparing paths that may not exist yet: its
/// nearest existing ancestor resolved by the filesystem (symlinks and all),
/// then the components below it that do not exist yet, with `.` and `..`
/// applied by hand. Nothing below that ancestor exists, so none of it can be
/// a symlink for `..` to see through.
fn resolved(p: &Path) -> PathBuf {
    let abs = std::path::absolute(p).unwrap_or_else(|_| p.to_path_buf());
    let Some((base, mut out)) = abs.ancestors().find_map(|a| a.canonicalize().ok().map(|c| (a, c))) else {
        return abs;
    };
    for part in abs.strip_prefix(base).map(Path::components).into_iter().flatten() {
        match part {
            std::path::Component::CurDir => {}
            std::path::Component::ParentDir => {
                out.pop();
            }
            other => out.push(other),
        }
    }
    out
}

/// Refuse a render that would write one file twice, or over a file it
/// reads, before anything is rendered or written: `--out DIR/deck1.wav` with
/// `--decks-out DIR` would replace the mix with deck 1 while the summary
/// still reported the mix, and an output over the plan or a track would
/// destroy the input.
fn check_paths(plan_path: &Path, base: &Path, plan: &Plan, out: &Path, decks_out: Option<&Path>) -> Result<(), String> {
    let mut writes = vec![(out.to_path_buf(), "--out".to_string())];
    let mut reads = vec![plan_path.to_path_buf()];
    for ev in &plan.events {
        if let Action::Cmd(protocol::Command::Load(spec)) = &ev.action {
            reads.push(base.join(&spec.path));
            // A deck loaded more than once still writes one file.
            let what = format!("deck {}'s output", spec.deck);
            if let Some(dir) = decks_out.filter(|_| !writes.iter().any(|(_, w)| w == &what)) {
                writes.push((dir.join(format!("deck{}.wav", spec.deck)), what));
            }
        }
    }
    for (i, (w, what)) in writes.iter().enumerate() {
        let rw = resolved(w);
        if let Some((_, other)) = writes[..i].iter().find(|(o, _)| resolved(o) == rw) {
            return Err(format!("{what} and {other} are both {}; give them different paths", w.display()));
        }
        if let Some(r) = reads.iter().find(|r| resolved(r) == rw) {
            return Err(format!("{what} {} would overwrite {}, which this render reads", w.display(), r.display()));
        }
    }
    Ok(())
}

fn render(mut args: Args) -> Result<(), String> {
    let plan_path = PathBuf::from(args.take("--plan")?.ok_or("render needs --plan")?);
    let out_path = PathBuf::from(args.take("--out")?.ok_or("render needs --out")?);
    let decks_out = args.take("--decks-out")?.map(PathBuf::from);
    args.done()?;
    let text = std::fs::read_to_string(&plan_path).map_err(|e| format!("cannot read {}: {e}", plan_path.display()))?;
    let value: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("{} is not JSON: {e}", plan_path.display()))?;
    let plan = parse_plan(&value).map_err(|e| format!("{}: {}", e.code.as_str(), e.message))?;
    let base = plan_path.parent().map(Path::to_path_buf).unwrap_or_default();
    check_paths(&plan_path, &base, &plan, &out_path, decks_out.as_deref())?;
    let opts = RenderOptions { deck_outputs: decks_out.is_some() };
    let out = render_plan_files_with(&plan, &base, opts).map_err(|e| format!("{}: {}", e.code.as_str(), e.message))?;
    write_wav(&out_path, out.sample_rate, &out.pcm)?;
    let mut deck_files = Vec::new();
    if let Some(dir) = &decks_out {
        std::fs::create_dir_all(dir).map_err(|e| format!("cannot create {}: {e}", dir.display()))?;
        for d in &out.decks {
            let p = dir.join(format!("deck{}.wav", d.deck));
            write_wav(&p, out.sample_rate, &d.pcm)?;
            deck_files.push(json!({"deck": d.deck, "out": p.display().to_string(), "sha256": d.sha256}));
        }
    }
    let ms = |f: u64| f as f64 * 1000.0 / out.sample_rate as f64;
    let decks_of = |mask: u8| -> Vec<u8> { (0..8).filter(|i| mask & (1 << i) != 0).map(|i| i + 1).collect() };
    let solo = |s: &Option<Solo>| s.as_ref().map(|s| json!({"deck": s.deck, "frames": s.frames, "ms": ms(s.frames)}));
    let fired: Vec<_> = out.fired.iter().map(|f| json!({"event": f.event, "frame": f.frame})).collect();
    let timeline: Vec<_> = out
        .timeline
        .iter()
        .map(|s| json!({"start": s.start, "end": s.end, "decks": decks_of(s.decks)}))
        .collect();
    let overlaps: Vec<_> = out
        .overlaps
        .iter()
        .map(|o| {
            json!({
                "start": o.start,
                "end": o.end,
                "ms": ms(o.end - o.start),
                "decks": decks_of(o.decks),
                "solo_before": solo(&o.solo_before),
                "solo_after": solo(&o.solo_after),
            })
        })
        .collect();
    let tempo: Vec<_> = out
        .tempo
        .iter()
        .map(|t| json!({"deck": t.deck, "frame": t.frame, "tempo": t.tempo, "bpm": t.bpm}))
        .collect();
    let plan_sha256: String = Sha256::digest(text.as_bytes()).iter().map(|b| format!("{b:02x}")).collect();
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
        // v1 plays varispeed only: a tempo change moves pitch with it.
        "master_tempo": false,
        "tempo": tempo,
        "timeline": timeline,
        "overlaps": overlaps,
        "deck_outputs": deck_files,
        "plan_sha256": plan_sha256,
        "plan": value,
    });
    println!("{summary}");
    Ok(())
}

fn write_wav(path: &Path, sr: u32, pcm: &[f32]) -> Result<(), String> {
    let file = File::create(path).map_err(|e| format!("cannot create {}: {e}", path.display()))?;
    wav::write_f32(&mut BufWriter::new(file), sr, pcm).map_err(|e| e.to_string())
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
            // A live session has no render budget: tracks load whole.
            let mut files = file_loader(std::env::current_dir().map_err(|e| e.to_string())?);
            let loader = move |spec: &odj_audio::protocol::LoadSpec| files(spec, u64::MAX);
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
