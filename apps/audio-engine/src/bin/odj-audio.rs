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

use std::io::{self, BufWriter};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use odj_audio::decode::SourceId;
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
///
/// `reads` are asked again here, not only before the render: a path swapped
/// or relinked while a long render ran would otherwise be emptied. Each
/// output is compared as opened, so once claimed, what its path names later
/// no longer matters: it is written through its handle.
fn claim_outputs(writes: &[(PathBuf, String)], reads: &Reads) -> Result<Vec<Handle>, String> {
    let mut made_files: Vec<Made> = Vec::new();
    let mut made_dirs: Vec<Made> = Vec::new();
    // Each only while its path still names what this claim made: another
    // run may have removed it and put its own in its place since.
    let undo = |files: &[Made], dirs: &[Made]| {
        for (f, _) in files.iter().rev().filter(|m| still_ours(m)) {
            let _ = std::fs::remove_file(f);
        }
        for (d, _) in dirs.iter().rev().filter(|m| still_ours(m)) {
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
                    // `missing/..` exists once `missing` is made, and another
                    // process may have made it since: only a directory this
                    // claim created is its to remove.
                    #[cfg(test)]
                    BEFORE_CREATE.with(|h| h.get().map(|h| h(d)));
                    match std::fs::create_dir(d) {
                        Ok(()) => made_dirs.push((d.to_path_buf(), Handle::from_path(d).ok())),
                        Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists && d.is_dir() => {}
                        Err(e) => return Err(format!("cannot create {}: {e}", d.display())),
                    }
                }
            }
            let f = create_or_open(p, &mut made_files)?;
            // Device and inode on Unix, volume serial and file index on
            // Windows: what the volume opened, however it was spelled.
            let h = Handle::from_file(f).map_err(|e| format!("cannot identify {}: {e}", p.display()))?;
            if let Some(j) = opened.iter().position(|g| *g == h) {
                return Err(format!("{what} and {} are both {}; give them different paths", writes[j].1, p.display()));
            }
            if let Some(r) = reads.as_read(&h) {
                return Err(format!("{what} {} would overwrite {r}; nothing was written", p.display()));
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

/// Open `p` for writing, without truncating, creating it if it is absent.
/// Whether this call created it is the volume's answer (an exclusive
/// create), not a look beforehand: another run may create the same absent
/// output in between, and a file it made is not this claim's to remove.
/// A file created here is added to `made`, as the file itself: through a
/// symlink, its target, with its identity taken from the file created.
fn create_or_open(p: &Path, made: &mut Vec<Made>) -> Result<std::fs::File, String> {
    let cannot = |e: std::io::Error| format!("cannot create {}: {e}", p.display());
    let target = resolved(p);
    // A file removed between the two opens is absent again: try again, a
    // few times, as the volume would for a retried create.
    for _ in 0..8 {
        #[cfg(test)]
        BEFORE_CREATE.with(|h| h.get().map(|h| h(&target)));
        match std::fs::OpenOptions::new().write(true).create_new(true).open(&target) {
            Ok(f) => {
                made.push((target, f.try_clone().ok().and_then(|g| Handle::from_file(g).ok())));
                return Ok(f);
            }
            Err(e) if e.kind() == std::io::ErrorKind::AlreadyExists => {
                match std::fs::OpenOptions::new().write(true).open(&target) {
                    Ok(f) => return Ok(f),
                    Err(e) if e.kind() == std::io::ErrorKind::NotFound => continue,
                    Err(e) => return Err(cannot(e)),
                }
            }
            Err(e) => return Err(cannot(e)),
        }
    }
    Err(format!("cannot create {}: it keeps being created and removed", p.display()))
}

/// A file or directory a claim created, and what it was as created (none
/// when it could not be identified, and then it is never removed).
type Made = (PathBuf, Option<Handle>);

/// Whether the path still names what the claim created there.
fn still_ours((path, made): &Made) -> bool {
    made.as_ref().is_some_and(|m| Handle::from_path(path).is_ok_and(|h| h == *m))
}

#[cfg(test)]
thread_local! {
    /// Runs just before an output or its directory is created: another run
    /// getting there first.
    static BEFORE_CREATE: std::cell::Cell<Option<fn(&Path)>> = const { std::cell::Cell::new(None) };
}

/// What a run read, for its outputs to be kept off: each input's path, asked
/// again as the outputs are claimed, and each file as it was opened, which
/// stays what was read whatever its path names by then (renamed away, with
/// another file put in its place).
#[derive(Default)]
struct Reads {
    paths: Vec<PathBuf>,
    files: Vec<SourceId>,
}

impl Reads {
    /// What `h`, an output held open, would overwrite of what was read, if
    /// anything. A path that no longer opens cannot be emptied through `h`.
    fn as_read(&self, h: &Handle) -> Option<String> {
        if let Some(r) = self.paths.iter().find(|r| Handle::from_path(r).is_ok_and(|g| g == *h)) {
            return Some(format!("{}, which this run read", r.display()));
        }
        let id = SourceId::of(h.as_file()).ok()?;
        self.files
            .contains(&id)
            .then(|| "a file this run read, since renamed or replaced at its path".to_string())
    }
}

/// What a render reads: the plan and every track it loads.
fn reads_of(plan_path: &Path, base: &Path, plan: &Plan) -> Vec<PathBuf> {
    let mut reads = vec![plan_path.to_path_buf()];
    for ev in &plan.events {
        if let Action::Cmd(protocol::Command::Load(spec)) = &ev.action {
            reads.push(base.join(&spec.path));
        }
    }
    reads
}

/// What a render read, for `claim_outputs`: the plan and its tracks by path,
/// and the plan file and each decoded track as they were opened.
fn render_reads(plan_path: &Path, base: &Path, plan: &Plan, plan_file: Option<SourceId>, tracks: &[SourceId]) -> Reads {
    Reads { paths: reads_of(plan_path, base, plan), files: plan_file.into_iter().chain(tracks.iter().cloned()).collect() }
}

/// Refuse a render that would write one file twice, or over a file it
/// reads, before anything is rendered or written (and `claim_outputs` asks
/// again once the render is done): `--out DIR/deck1.wav` with
/// `--decks-out DIR` would replace the mix with deck 1 while the summary
/// still reported the mix, and an output over the plan or a track would
/// destroy the input.
fn check_paths(plan_path: &Path, base: &Path, plan: &Plan, out: &Path, decks_out: Option<&Path>) -> Result<(), String> {
    let writes = writes_of(plan, out, decks_out);
    let reads = reads_of(plan_path, base, plan);
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
    let (text, plan_file) = {
        use std::io::Read;
        let mut f = std::fs::File::open(&plan_path).map_err(|e| format!("cannot read {}: {e}", plan_path.display()))?;
        let mut text = String::new();
        f.read_to_string(&mut text).map_err(|e| format!("cannot read {}: {e}", plan_path.display()))?;
        (text, SourceId::of(&f).ok())
    };
    let value: serde_json::Value =
        serde_json::from_str(&text).map_err(|e| format!("{} is not JSON: {e}", plan_path.display()))?;
    let plan = parse_plan(&value).map_err(|e| format!("{}: {}", e.code.as_str(), e.message))?;
    let base = plan_path.parent().map(Path::to_path_buf).unwrap_or_default();
    check_paths(&plan_path, &base, &plan, &out_path, decks_out.as_deref())?;
    let opts = RenderOptions { deck_outputs: decks_out.is_some() };
    let out = render_plan_files_with(&plan, &base, opts).map_err(|e| format!("{}: {}", e.code.as_str(), e.message))?;
    let writes = writes_of(&plan, &out_path, decks_out.as_deref());
    let reads = render_reads(&plan_path, &base, &plan, plan_file, &out.reads);
    let mut files: Vec<(PathBuf, Handle)> = writes.iter().map(|(p, _)| p.clone()).zip(claim_outputs(&writes, &reads)?).collect();
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
            // Every track the session read, by path and as opened, kept off
            // when the recording is written: a path swapped or relinked during
            // the session would otherwise be emptied.
            let loaded = std::rc::Rc::new(std::cell::RefCell::new(Reads::default()));
            let read = loaded.clone();
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
                let track = files(spec)?;
                let mut r = read.borrow_mut();
                r.paths.push(cwd.join(&spec.path));
                if let Some(s) = track.source.as_ref().filter(|s| !r.files.contains(s)) {
                    r.files.push(s.clone());
                }
                Ok(track)
            };
            let sink = if record.is_some() { Some(&mut rec) } else { None };
            serve::serve_fake(io::stdin().lock(), io::stdout().lock(), sr, loader, sink).map_err(|e| e.to_string())?;
            if let Some(p) = record {
                let writes = [(p.clone(), "--record".to_string())];
                let h = claim_outputs(&writes, &loaded.borrow())?.remove(0);
                write_wav(h, &p, sr, &rec)?;
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
    serve::serve_threaded(rate, "device", odj_audio::device::run_device(rate)).map_err(|e| e.to_string())
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
        let err = claim_outputs(&writes, &Reads::default()).unwrap_err();
        assert!(err.contains("deck 2's output and deck 1's output are both"), "{err}");
        assert!(!d.join("real").join("x.wav").exists(), "a file the claim made was left");
        assert!(!d.join("new").exists(), "a directory the claim made was left");
        // Control: a file that was there before is neither removed nor emptied.
        assert_eq!(std::fs::read(d.join("keep.wav")).unwrap(), b"old");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn an_output_that_became_an_input_during_the_run_is_refused_and_left_whole() {
        // Codex on d959eae8: the reads were asked only before a render, so
        // an output relinked to the plan or a track while it ran was then
        // emptied. The claim asks again, against the output as opened.
        let d = dir("relinked");
        std::fs::write(d.join("track.wav"), b"track").unwrap();
        std::fs::write(d.join("plan.json"), b"plan").unwrap();
        let reads = Reads { paths: vec![d.join("plan.json"), d.join("track.wav"), d.join("gone.wav")], files: vec![] };
        // A hard link is the swap made while the render ran.
        std::fs::hard_link(d.join("track.wav"), d.join("out.wav")).unwrap();
        let err = claim_outputs(&[w(d.join("out.wav"), "--out")], &reads).unwrap_err();
        assert!(err.contains("would overwrite") && err.contains("track.wav"), "{err}");
        assert_eq!(std::fs::read(d.join("track.wav")).unwrap(), b"track");
        // And a deck output replaced by a link to the plan, behind a first
        // output the claim created: that one is gone again.
        #[cfg(unix)]
        {
            std::os::unix::fs::symlink(d.join("plan.json"), d.join("deck1.wav")).unwrap();
            let writes = [w(d.join("new.wav"), "--out"), w(d.join("deck1.wav"), "deck 1's output")];
            let err = claim_outputs(&writes, &reads).unwrap_err();
            assert!(err.contains("deck 1's output") && err.contains("plan.json"), "{err}");
            assert_eq!(std::fs::read(d.join("plan.json")).unwrap(), b"plan");
            assert!(!d.join("new.wav").exists(), "a file the claim made was left");
        }
        // Control: an output that is not a read is claimed, next to reads
        // that exist and one that no longer does.
        assert_eq!(claim_outputs(&[w(d.join("fresh.wav"), "--out")], &reads).unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn an_output_linked_to_a_read_renamed_away_is_refused_and_left_whole() {
        // Codex on d07e4db1: only the input paths were asked again, so a
        // track renamed away and replaced at its path, with the output then
        // linked to the file that was read, passed the claim and was emptied.
        let d = dir("renamed");
        std::fs::write(d.join("track.wav"), b"track").unwrap();
        let read = SourceId::of(&std::fs::File::open(d.join("track.wav")).unwrap()).unwrap();
        std::fs::rename(d.join("track.wav"), d.join("old.wav")).unwrap();
        std::fs::write(d.join("track.wav"), b"new").unwrap();
        std::fs::hard_link(d.join("old.wav"), d.join("out.wav")).unwrap();
        let reads = Reads { paths: vec![d.join("track.wav")], files: vec![read] };
        let err = claim_outputs(&[w(d.join("out.wav"), "--out")], &reads).unwrap_err();
        assert!(err.contains("would overwrite a file this run read, since renamed or replaced"), "{err}");
        assert_eq!(std::fs::read(d.join("old.wav")).unwrap(), b"track");
        // Control: the path alone names the new file, so it cannot say this;
        // and neither the new file nor a fresh output is taken for the read.
        let by_path = Reads { paths: reads.paths.clone(), files: vec![] };
        assert_eq!(claim_outputs(&[w(d.join("out.wav"), "--out")], &by_path).unwrap().len(), 1);
        assert_eq!(claim_outputs(&[w(d.join("fresh.wav"), "--out")], &reads).unwrap().len(), 1);
        std::fs::hard_link(d.join("track.wav"), d.join("now.wav")).unwrap();
        let only_id = Reads { paths: vec![], files: reads.files.clone() };
        assert_eq!(claim_outputs(&[w(d.join("now.wav"), "--out")], &only_id).unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_render_holds_its_plan_and_tracks_as_opened() {
        let d = dir("render-reads");
        std::fs::write(d.join("plan.json"), b"{}").unwrap();
        std::fs::write(d.join("a.wav"), b"a").unwrap();
        let id = |n: &str| SourceId::of(&std::fs::File::open(d.join(n)).unwrap()).unwrap();
        let plan = parse_plan(&json!({"sample_rate": 48000, "end": {"ms": 10}, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}}
        ]}))
        .unwrap();
        let r = render_reads(&d.join("plan.json"), &d, &plan, Some(id("plan.json")), &[id("a.wav")]);
        assert_eq!(r.paths, vec![d.join("plan.json"), d.join("a.wav")]);
        assert_eq!(r.files, vec![id("plan.json"), id("a.wav")]);
        // The plan renamed away and replaced: an output linked to the plan
        // that was read is refused, and one linked to its replacement is not.
        std::fs::rename(d.join("plan.json"), d.join("plan-old.json")).unwrap();
        std::fs::write(d.join("plan.json"), b"{}").unwrap();
        std::fs::hard_link(d.join("plan-old.json"), d.join("out.wav")).unwrap();
        let only_ids = Reads { paths: vec![], files: r.files.clone() };
        let err = claim_outputs(&[w(d.join("out.wav"), "--out")], &only_ids).unwrap_err();
        assert!(err.contains("since renamed or replaced"), "{err}");
        assert_eq!(std::fs::read(d.join("plan-old.json")).unwrap(), b"{}");
        std::fs::hard_link(d.join("plan.json"), d.join("out2.wav")).unwrap();
        assert_eq!(claim_outputs(&[w(d.join("out2.wav"), "--out")], &only_ids).unwrap().len(), 1);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_refused_claim_leaves_an_output_another_run_created_first() {
        // Codex on b87a0f49: two runs racing on one absent output both saw
        // it absent, so the second counted the first's file as its own and
        // removed it when a later output of its own was refused.
        let d = dir("raced");
        std::fs::write(d.join("plan.json"), b"plan").unwrap();
        let reads = Reads { paths: vec![d.join("plan.json")], files: vec![] };
        let writes = [w(d.join("mix.wav"), "--out"), w(d.join("plan.json"), "deck 1's output")];
        BEFORE_CREATE.with(|h| {
            h.set(Some(|p: &Path| {
                if p.ends_with("mix.wav") && !p.exists() {
                    std::fs::write(p, b"theirs").unwrap();
                }
            }))
        });
        let err = claim_outputs(&writes, &reads);
        BEFORE_CREATE.with(|h| h.set(None));
        assert!(err.unwrap_err().contains("plan.json"));
        assert_eq!(std::fs::read(d.join("mix.wav")).unwrap(), b"theirs", "the other run's file was removed");
        // Control: a file this claim did create is removed on the same refusal.
        std::fs::remove_file(d.join("mix.wav")).unwrap();
        assert!(claim_outputs(&writes, &reads).is_err());
        assert!(!d.join("mix.wav").exists(), "a file the claim made was left");
        // Likewise a directory another run made while this claim made its
        // way down to the output: it is not this claim's to remove.
        BEFORE_CREATE.with(|h| {
            h.set(Some(|p: &Path| {
                if p.ends_with("new") && !p.exists() {
                    std::fs::create_dir(p).unwrap();
                }
            }))
        });
        let writes = [w(d.join("new").join("mix.wav"), "--out"), w(d.join("plan.json"), "deck 1's output")];
        let err = claim_outputs(&writes, &reads);
        BEFORE_CREATE.with(|h| h.set(None));
        assert!(err.unwrap_err().contains("plan.json"), "a directory made first failed the claim");
        assert!(d.join("new").is_dir(), "the other run's directory was removed");
        assert!(!d.join("new").join("mix.wav").exists(), "a file the claim made was left");
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_refused_claim_leaves_what_another_run_put_in_place_of_its_own() {
        // Codex on bb0d2322: creation was decided atomically, but the undo
        // removed by path, so an output this claim created, then replaced by
        // another run, was removed as if it were still this claim's.
        let d = dir("replaced");
        std::fs::write(d.join("plan.json"), b"plan").unwrap();
        let reads = Reads { paths: vec![d.join("plan.json")], files: vec![] };
        let writes = [w(d.join("new").join("mix.wav"), "--out"), w(d.join("plan.json"), "deck 1's output")];
        // Between the claim creating new/mix.wav and refusing plan.json,
        // another run replaces both the file and the directory with its own.
        BEFORE_CREATE.with(|h| {
            h.set(Some(|p: &Path| {
                if p.ends_with("plan.json") {
                    let new = p.with_file_name("new");
                    std::fs::remove_file(new.join("mix.wav")).unwrap();
                    std::fs::remove_dir(&new).unwrap();
                    std::fs::create_dir(&new).unwrap();
                    std::fs::write(new.join("mix.wav"), b"theirs").unwrap();
                }
            }))
        });
        let err = claim_outputs(&writes, &reads);
        BEFORE_CREATE.with(|h| h.set(None));
        assert!(err.unwrap_err().contains("plan.json"));
        assert_eq!(std::fs::read(d.join("new").join("mix.wav")).unwrap(), b"theirs", "the other run's file was removed");
        // Its directory too, once empty: the claim did not make this one.
        std::fs::remove_file(d.join("new").join("mix.wav")).unwrap();
        BEFORE_CREATE.with(|h| {
            h.set(Some(|p: &Path| {
                if p.ends_with("plan.json") {
                    let new = p.with_file_name("new");
                    std::fs::remove_file(new.join("mix.wav")).unwrap();
                    std::fs::remove_dir(&new).unwrap();
                    std::fs::create_dir(&new).unwrap();
                }
            }))
        });
        std::fs::remove_dir(d.join("new")).unwrap();
        let err = claim_outputs(&writes, &reads);
        BEFORE_CREATE.with(|h| h.set(None));
        assert!(err.is_err());
        assert!(d.join("new").is_dir(), "the other run's directory was removed");
        // Control: what the claim made and nobody replaced is removed.
        std::fs::remove_dir(d.join("new")).unwrap();
        assert!(claim_outputs(&writes, &reads).is_err());
        assert!(!d.join("new").exists(), "what the claim made was left");
        let _ = std::fs::remove_dir_all(&d);
    }

    // Control: distinct outputs are all claimed, one handle each, in order,
    // and an existing file keeps its bytes until it is written.
    #[test]
    fn distinct_outputs_are_all_claimed() {
        let d = dir("distinct");
        std::fs::write(d.join("keep.wav"), b"old").unwrap();
        let writes = [w(d.join("keep.wav"), "--out"), w(d.join("n").join("deck1.wav"), "deck 1's output"), w(d.join("n").join("deck2.wav"), "deck 2's output")];
        let files = claim_outputs(&writes, &Reads::default()).unwrap();
        assert_eq!(files.len(), 3);
        assert_eq!(std::fs::read(d.join("keep.wav")).unwrap(), b"old");
        assert!(d.join("n").join("deck1.wav").exists() && d.join("n").join("deck2.wav").exists());
        let _ = std::fs::remove_dir_all(&d);
    }
}
