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

use odj_audio::engine::ErrorCode;
use odj_audio::offline::{render_plan_files_with, session_loader, RenderOptions, Solo};
use odj_audio::plan::{parse_plan, Action, Plan};
use odj_audio::{protocol, serve, wav};
use serde_json::json;
use same_file::Handle;
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

/// One spelling per file, for comparing paths that may not exist yet: the
/// absolute path walked one component at a time as the filesystem walks it,
/// following every symlink met on the way (a dangling one too, since
/// `File::create` follows it and creates its target), with `.` skipped and
/// `..` applied to what the walk has resolved so far. Components that do not
/// exist are kept as spelled.
fn resolved(p: &Path) -> PathBuf {
    use std::path::Component;
    let abs = std::path::absolute(p).unwrap_or_else(|_| p.to_path_buf());
    let mut todo: Vec<PathBuf> = abs.components().rev().map(|c| PathBuf::from(c.as_os_str())).collect();
    let mut out = PathBuf::new();
    // Past this many links the filesystem gives up too (ELOOP); keep the
    // spelling rather than loop.
    let mut links = 0;
    while let Some(part) = todo.pop() {
        match part.components().next() {
            Some(Component::Prefix(_) | Component::RootDir) => out.push(&part),
            Some(Component::ParentDir) => {
                out.pop();
            }
            Some(Component::Normal(name)) => {
                let next = out.join(name);
                match std::fs::read_link(&next) {
                    Ok(target) if links < 40 => {
                        links += 1;
                        // A relative target is read from the link's directory,
                        // which is `out`; an absolute one restarts at its root.
                        todo.extend(target.components().rev().map(|c| PathBuf::from(c.as_os_str())));
                    }
                    _ => out = next,
                }
            }
            Some(Component::CurDir) | None => {}
        }
    }
    out
}

/// Whether two paths name one file: the same resolved spelling, or, where
/// both exist, the same file on disk (a hard link has its own spelling but
/// is the file it links to, so writing it truncates that file; a spelling
/// in another case or Unicode form is the same file on a volume that folds
/// it). On Unix that is device and inode, read without opening either
/// file; elsewhere `same_file` opens both and compares volume serial and
/// file index (Windows).
///
/// Two outputs that do not exist yet have no identity to compare, so this
/// cannot tell whether the volume folds their spellings into one file;
/// `claim_outputs` asks the volume itself before anything is written.
fn same_file(a: &Path, b: &Path) -> bool {
    if resolved(a) == resolved(b) {
        return true;
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if let (Ok(ma), Ok(mb)) = (std::fs::metadata(a), std::fs::metadata(b)) {
            return (ma.dev(), ma.ino()) == (mb.dev(), mb.ino());
        }
    }
    #[cfg(not(unix))]
    if a.exists() && b.exists() {
        return same_file::is_same_file(a, b).unwrap_or(false);
    }
    false
}

/// The files a render writes, each with what it is: the mix, then one per
/// deck the plan loads when `decks_out` is given.
fn writes_of(plan: &Plan, out: &Path, decks_out: Option<&Path>) -> Vec<(PathBuf, String)> {
    let mut writes = vec![(out.to_path_buf(), "--out".to_string())];
    for ev in &plan.events {
        if let Action::Cmd(protocol::Command::Load(spec)) = &ev.action {
            // A deck loaded more than once still writes one file.
            let what = format!("deck {}'s output", spec.deck);
            if let Some(dir) = decks_out.filter(|_| !writes.iter().any(|(_, w)| w == &what)) {
                writes.push((dir.join(format!("deck{}.wav", spec.deck)), what));
            }
        }
    }
    writes
}

/// Open every output before any is written, creating the ones (and the
/// directories) that do not exist yet, and refuse the render if two of them
/// are one file. The volume itself answers, so spellings that differ only
/// in case or Unicode form count as one file exactly where the volume folds
/// them, and nowhere else. On refusal everything created here is removed
/// again, so a refused render leaves nothing behind. The identity compared
/// is the open handle's (device and inode on Unix, volume serial and file
/// index on Windows), through `same_file`.
fn claim_outputs(writes: &[(PathBuf, String)]) -> Result<Vec<Handle>, String> {
    let mut made_files: Vec<PathBuf> = Vec::new();
    let mut made_dirs: Vec<PathBuf> = Vec::new();
    let undo = |files: &[PathBuf], dirs: &[PathBuf]| {
        for f in files.iter().rev() {
            let _ = std::fs::remove_file(f);
        }
        for d in dirs.iter().rev() {
            let _ = std::fs::remove_dir(d);
        }
    };
    let mut opened: Vec<Handle> = Vec::new();
    for (p, what) in writes {
        let claimed = (|| {
            if let Some(parent) = p.parent().filter(|d| !d.as_os_str().is_empty()) {
                let mut missing: Vec<&Path> = parent.ancestors().take_while(|a| !a.as_os_str().is_empty() && !a.exists()).collect();
                missing.reverse();
                for d in missing {
                    // `missing/..` exists once `missing` is made.
                    if d.exists() {
                        continue;
                    }
                    std::fs::create_dir(d).map_err(|e| format!("cannot create {}: {e}", d.display()))?;
                    made_dirs.push(d.to_path_buf());
                }
            }
            let existed = std::fs::metadata(p).is_ok();
            let f = std::fs::OpenOptions::new()
                .write(true)
                .create(true)
                .truncate(false)
                .open(p)
                .map_err(|e| format!("cannot create {}: {e}", p.display()))?;
            if !existed {
                // Through a symlink the file made is its target.
                made_files.push(std::fs::canonicalize(p).unwrap_or_else(|_| p.clone()));
            }
            // Device and inode on Unix, volume serial and file index on
            // Windows: what the volume opened, however it was spelled.
            let h = Handle::from_file(f).map_err(|e| format!("cannot identify {}: {e}", p.display()))?;
            if let Some(j) = opened.iter().position(|g| *g == h) {
                return Err(format!("{what} and {} are both {}; give them different paths", writes[j].1, p.display()));
            }
            Ok(h)
        })();
        match claimed {
            Ok(h) => opened.push(h),
            Err(e) => {
                drop(opened);
                undo(&made_files, &made_dirs);
                return Err(e);
            }
        }
    }
    Ok(opened)
}

/// Refuse a render that would write one file twice, or over a file it
/// reads, before anything is rendered or written: `--out DIR/deck1.wav` with
/// `--decks-out DIR` would replace the mix with deck 1 while the summary
/// still reported the mix, and an output over the plan or a track would
/// destroy the input.
fn check_paths(plan_path: &Path, base: &Path, plan: &Plan, out: &Path, decks_out: Option<&Path>) -> Result<(), String> {
    let writes = writes_of(plan, out, decks_out);
    let mut reads = vec![plan_path.to_path_buf()];
    for ev in &plan.events {
        if let Action::Cmd(protocol::Command::Load(spec)) = &ev.action {
            reads.push(base.join(&spec.path));
        }
    }
    for (i, (w, what)) in writes.iter().enumerate() {
        if let Some((_, other)) = writes[..i].iter().find(|(o, _)| same_file(o, w)) {
            return Err(format!("{what} and {other} are both {}; give them different paths", w.display()));
        }
        if let Some(r) = reads.iter().find(|r| same_file(r, w)) {
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
    let writes = writes_of(&plan, &out_path, decks_out.as_deref());
    let mut files: Vec<(PathBuf, Handle)> = writes.iter().map(|(p, _)| p.clone()).zip(claim_outputs(&writes)?).collect();
    let mut take = |p: &Path| -> Result<Handle, String> {
        let i = files.iter().position(|(q, _)| q == p).ok_or_else(|| format!("{} was not claimed", p.display()))?;
        Ok(files.swap_remove(i).1)
    };
    write_wav(take(&out_path)?, &out_path, out.sample_rate, &out.pcm)?;
    let mut deck_files = Vec::new();
    if let Some(dir) = &decks_out {
        for d in &out.decks {
            let p = dir.join(format!("deck{}.wav", d.deck));
            write_wav(take(&p)?, &p, out.sample_rate, &d.pcm)?;
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

/// Write a claimed output: it was opened without truncating, so a file the
/// render replaces is emptied here, only once every output is known apart.
fn write_wav(handle: Handle, path: &Path, sr: u32, pcm: &[f32]) -> Result<(), String> {
    let file = handle.as_file();
    file.set_len(0).map_err(|e| format!("cannot write {}: {e}", path.display()))?;
    wav::write_f32(&mut BufWriter::new(file), sr, pcm).map_err(|e| format!("cannot write {}: {e}", path.display()))
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
            // A live session has no render budget: tracks load whole, and a
            // track no deck holds is let go.
            let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
            let mut files = session_loader(cwd.clone());
            let record_to = record.clone();
            let loader = move |spec: &odj_audio::protocol::LoadSpec| {
                // The recording is written over its path when the session
                // ends, so a track read from that file would be destroyed.
                if let Some(r) = &record_to {
                    if same_file(&cwd.join(&spec.path), r) {
                        return Err(protocol::ProtoError::new(
                            ErrorCode::Invalid,
                            format!("{} is the --record file, which this session overwrites when it ends", spec.path),
                        ));
                    }
                }
                files(spec)
            };
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

#[cfg(test)]
mod tests {
    use super::*;

    fn dir(name: &str) -> PathBuf {
        let d = std::env::temp_dir().join(format!("odj-claim-{name}-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&d);
        std::fs::create_dir_all(&d).unwrap();
        d
    }

    fn w(p: PathBuf, what: &str) -> (PathBuf, String) {
        (p, what.to_string())
    }

    // Two spellings the names cannot tell apart but the volume can: a
    // directory alias stands in for a volume folding case or Unicode form,
    // which a Linux test volume does not. The volume's answer refuses them,
    // and what the claim made is gone again.
    #[cfg(unix)]
    #[test]
    fn outputs_the_volume_holds_as_one_file_are_refused_and_nothing_is_left() {
        let d = dir("alias");
        std::fs::create_dir_all(d.join("real")).unwrap();
        std::os::unix::fs::symlink(d.join("real"), d.join("link")).unwrap();
        std::fs::write(d.join("keep.wav"), b"old").unwrap();
        let writes = [
            w(d.join("keep.wav"), "--keep"),
            w(d.join("new").join("sub").join("a.wav"), "--out"),
            w(d.join("real").join("x.wav"), "deck 1's output"),
            w(d.join("link").join("x.wav"), "deck 2's output"),
        ];
        let err = claim_outputs(&writes).unwrap_err();
        assert!(err.contains("deck 2's output and deck 1's output are both"), "{err}");
        assert!(!d.join("real").join("x.wav").exists(), "a file the claim made was left");
        assert!(!d.join("new").exists(), "a directory the claim made was left");
        // Control: a file that was there before is neither removed nor emptied.
        assert_eq!(std::fs::read(d.join("keep.wav")).unwrap(), b"old");
        let _ = std::fs::remove_dir_all(&d);
    }

    // Control: distinct outputs are all claimed, one handle each, in order,
    // and an existing file keeps its bytes until it is written.
    #[test]
    fn distinct_outputs_are_all_claimed() {
        let d = dir("distinct");
        std::fs::write(d.join("keep.wav"), b"old").unwrap();
        let writes = [w(d.join("keep.wav"), "--out"), w(d.join("n").join("deck1.wav"), "deck 1's output"), w(d.join("n").join("deck2.wav"), "deck 2's output")];
        let files = claim_outputs(&writes).unwrap();
        assert_eq!(files.len(), 3);
        assert_eq!(std::fs::read(d.join("keep.wav")).unwrap(), b"old");
        assert!(d.join("n").join("deck1.wav").exists() && d.join("n").join("deck2.wav").exists());
        let _ = std::fs::remove_dir_all(&d);
    }
}
