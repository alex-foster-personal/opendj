//! odj-audio command line.
//!
//!   odj-audio render --plan PLAN.json --out OUT.wav [--decks-out DIR]
//!   odj-audio serve [--clock fake|wall|device] [--sample-rate 48000] [--block 256] [--record OUT.wav]
//!                   [--ws 127.0.0.1:0]
//!                   [--midi] [--midi-map MAPS.json]
//!   odj-audio waveform --in AUDIO --out PEAKS [--low-hz 200] [--high-hz 4000] [--sections 2] [--columns-per-s 150]
//!   odj-audio fingerprint FILE... [--length SECONDS]
//!   odj-audio decode --in SOURCE --out OUT.wav
//!   odj-audio decode PATH [--rate HZ] [--mono] [--format f32le|s16le]
//!   odj-audio probe PATH
//!   odj-audio input-devices
//!   odj-audio record --dir DIR (--device NAME | --device-index N) [--segment-seconds 300]
//!   odj-audio version
//!
//! `waveform` writes the track's tri-band peak columns to PEAKS as raw bytes,
//! three per column (low, mid, high), and prints one JSON line: the column
//! count, the rate it filtered at and the file's channel count. See
//! `src/waveform.rs`.
//!
//! `render` prints one JSON summary line: the plan it rendered, the output's
//! sha256, when each event fired, which decks are heard when (the timeline
//! and its overlaps), and each deck's tempo. `--decks-out` also writes each
//! loaded deck's own audio as `deckN.wav`. `serve` speaks protocol v1 on
//! stdin/stdout; see `src/protocol.rs`. With `--ws`
//! (wall and device clocks) it also listens on a loopback WebSocket for the
//! renderer and agents, each presenting `ODJ_AUDIO_WS_TOKEN`; see `src/ws.rs`.
//! `--midi` (build feature `midi`, wall
//! and device clocks) has the engine read the controllers its device maps
//! match; `--midi-map` adds onboarded maps that win over the built-in ones.
//! `midi_inject` feeds recorded MIDI bytes on any clock, with or without it.
//! `decode` writes SOURCE (any format a deck loads: MP3, AAC, FLAC, ...) as a
//! 32-bit float WAV at its own rate and channel count, streamed a packet at a
//! time, and prints one JSON line naming them and the frame count. An MP4's
//! edit list is applied (encoder priming trimmed) so it starts where ffmpeg
//! and a deck start it. It never replaces a file: OUT must not exist. This is how the stems and vocals
//! workers read compressed audio in the installed app, which ships no ffmpeg
//! (`docs/decisions/*-odj-audio-decode-for-workers.md`).
//! `input-devices` (build feature `device`) prints the audio inputs as one
//! JSON line, and `record` records one of them into DIR as rolling 16-bit WAV
//! segments named by their UTC start (`src/record.rs`), printing a JSON line
//! when it is recording (`{"recording":...}`, after `{"waiting":
//! "microphone_permission"}` while macOS's first-run prompt is up) and another
//! when it stops. It stops, closing
//! the last segment, when stdin reaches end of file or reads `stop`. This is
//! how REC records a set in the installed app, which ships no ffmpeg
//! (`docs/decisions/*-set-recording-without-ffmpeg.md`).

use std::io::{self, BufWriter};
use std::path::{Path, PathBuf};
use std::process::ExitCode;

use odj_audio::decode::SourceId;
use odj_audio::engine::ErrorCode;
use odj_audio::offline::{render_plan_files_with, session_loader_at, RenderOptions, Solo};
use odj_audio::plan::{parse_plan, Action, Plan};
use odj_audio::midi::{MapSet, Router};
use odj_audio::{protocol, serve, wav};
use serde_json::json;
use same_file::Handle;
use sha2::{Digest, Sha256};

const USAGE: &str = "usage:
  odj-audio render --plan PLAN.json --out OUT.wav [--decks-out DIR]
  odj-audio serve [--clock fake|wall|device] [--sample-rate HZ] [--block FRAMES] [--record OUT.wav]
                  [--ws LOOPBACK_ADDR:PORT]   (token from ODJ_AUDIO_WS_TOKEN)
                  [--midi] [--midi-map MAPS.json]
  odj-audio waveform --in AUDIO --out PEAKS [--low-hz HZ] [--high-hz HZ] [--sections N] [--columns-per-s N]
  odj-audio fingerprint FILE... [--length SECONDS]   (0 = whole file; default 120, as fpcalc)
  odj-audio decode --in SOURCE --out OUT.wav   (OUT must not exist)
  odj-audio decode PATH [--rate HZ] [--mono] [--format f32le|s16le]
  odj-audio probe PATH
  odj-audio input-devices
  odj-audio record --dir DIR (--device NAME | --device-index N) [--segment-seconds SECONDS]
                   (stops on `stop` or end of file on stdin)
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

    fn flag(&mut self, flag: &str) -> bool {
        match self.rest.iter().position(|a| a == flag) {
            None => false,
            Some(i) => {
                self.rest.remove(i);
                true
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
        // False: every tempo change in this render moved pitch with it.
        "master_tempo": out.master_tempo,
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
    let ws_addr = args.take("--ws")?;
    let midi_map = args.take("--midi-map")?.map(PathBuf::from);
    let midi_ports = args.flag("--midi");
    args.done()?;
    if record.is_some() && clock != "fake" {
        return Err("--record works on the fake clock only".into());
    }
    let ws = match ws_addr {
        None => None,
        // The fake clock is a single-threaded simulator driven by one sender.
        Some(_) if clock == "fake" => return Err("--ws works on the wall and device clocks only".into()),
        Some(a) => {
            let addr: std::net::SocketAddr = a.parse().map_err(|_| format!("--ws needs HOST:PORT, got {a}"))?;
            let token = std::env::var("ODJ_AUDIO_WS_TOKEN").map_err(|_| "--ws needs ODJ_AUDIO_WS_TOKEN in the environment")?;
            odj_audio::ws::check_token(&token)?;
            let listener = odj_audio::ws::bind(addr).map_err(|e| format!("--ws {a}: {e}"))?;
            Some(serve::WsListen { listener, token })
        }
    };
    if midi_ports && clock == "fake" {
        return Err("--midi reads controllers in real time; on the fake clock send midi_inject lines instead".into());
    }
    let mut maps = MapSet::builtin()?;
    if let Some(p) = &midi_map {
        let text = std::fs::read_to_string(p).map_err(|e| format!("cannot read {}: {e}", p.display()))?;
        let v: serde_json::Value = serde_json::from_str(&text).map_err(|e| format!("{} is not JSON: {e}", p.display()))?;
        maps.install_json(&v, &p.display().to_string())?;
    }
    let midi = serve::MidiSetup { router: Router::new(maps), open: if midi_ports { Some(midi_opener()?) } else { None } };
    match clock.as_str() {
        "fake" => {
            let sr = sr.unwrap_or(48000);
            let mut rec = Vec::new();
            // A live session has no render budget: tracks load whole, and a
            // track no deck holds is let go.
            let cwd = std::env::current_dir().map_err(|e| e.to_string())?;
            let mut files = session_loader_at(cwd.clone(), Some(sr));
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
            serve::serve_fake(io::stdin().lock(), io::stdout().lock(), sr, loader, sink, midi.router).map_err(|e| e.to_string())?;
            if let Some(p) = record {
                let writes = [(p.clone(), "--record".to_string())];
                let h = claim_outputs(&writes, &loaded.borrow())?.remove(0);
                write_wav(h, &p, sr, &rec)?;
            }
            Ok(())
        }
        "wall" => serve::serve_threaded(sr.unwrap_or(48000), "wall", midi, serve::run_wall(block), ws).map_err(|e| e.to_string()),
        "device" => device(sr, midi, ws),
        other => Err(format!("unknown clock {other}; use fake, wall or device")),
    }
}

#[cfg(feature = "midi")]
fn midi_opener() -> Result<serve::MidiOpener, String> {
    Ok(odj_audio::midi_in::opener())
}

#[cfg(not(feature = "midi"))]
fn midi_opener() -> Result<serve::MidiOpener, String> {
    Err("this build has no MIDI input; rebuild with --features midi".into())
}

#[cfg(feature = "device")]
fn device(sr: Option<u32>, midi: serve::MidiSetup, ws: Option<serve::WsListen>) -> Result<(), String> {
    let probed = odj_audio::device::default_output()?;
    let rate = probed.rate;
    if let Some(want) = sr {
        if want != rate {
            return Err(format!("the output device runs at {rate} Hz; --sample-rate {want} does not match"));
        }
    }
    serve::serve_threaded(rate, "device", midi, odj_audio::device::run_device(probed), ws).map_err(|e| e.to_string())
}

/// `odj-audio decode` has two forms: `--in SOURCE --out OUT.wav` writes a new
/// float WAV file ([`decode_wav_cmd`], the stems and vocals workers), and a
/// bare `PATH` streams raw PCM to stdout ([`decode_pcm_cmd`], the analysis
/// lanes). `--in` picks the first.
fn decode_cmd(args: Args) -> Result<(), String> {
    if args.rest.iter().any(|a| a == "--in") {
        decode_wav_cmd(args)
    } else {
        decode_pcm_cmd(args)
    }
}

/// `odj-audio decode`: SOURCE to a new float WAV at OUT (see the module
/// docs). OUT is created, never replaced, so neither a typo nor OUT naming
/// SOURCE itself (or a link to it) can truncate a library file; a failed
/// decode removes the partial OUT it created.
fn decode_wav_cmd(mut args: Args) -> Result<(), String> {
    let src = PathBuf::from(args.take("--in")?.ok_or("decode needs --in")?);
    let out_path = PathBuf::from(args.take("--out")?.ok_or("decode needs --out")?);
    args.done()?;
    let file = std::fs::OpenOptions::new()
        .write(true)
        .create_new(true)
        .open(&out_path)
        .map_err(|e| format!("cannot create {}: {e} (decode never replaces a file)", out_path.display()))?;
    let mut w = BufWriter::new(file);
    let written = odj_audio::decode::decode_to_wav(&src, &mut w)
        .and_then(|r| {
            w.get_ref().sync_all().map_err(|e| {
                protocol::ProtoError::new(ErrorCode::Io, format!("cannot write {}: {e}", out_path.display()))
            })?;
            Ok(r)
        });
    let written = match written {
        Ok(r) => r,
        Err(e) => {
            drop(w);
            let _ = std::fs::remove_file(&out_path);
            return Err(format!("{}: {}", e.code.as_str(), e.message));
        }
    };
    let summary = json!({
        "type": "decode",
        "in": src.display().to_string(),
        "out": out_path.display().to_string(),
        "format": "wav_f32",
        "sample_rate": written.sample_rate,
        "channels": written.channels,
        "frames": written.frames,
        "duration_s": written.frames as f64 / written.sample_rate as f64,
        // MP4 edit list: encoder priming trimmed from the front, whole
        // packets dropped past its end, and whether it was applied.
        "trimmed_start_frames": written.trimmed_start,
        "dropped_end_frames": written.dropped_end,
        "edit_list": written.edit,
    });
    println!("{summary}");
    Ok(())
}

#[cfg(not(feature = "device"))]
fn device(_sr: Option<u32>, _midi: serve::MidiSetup, _ws: Option<serve::WsListen>) -> Result<(), String> {
    Err("this build has no device output; rebuild with --features device".into())
}

fn waveform_cmd(mut a: Args) -> Result<(), String> {
    let path = a.take("--in")?.ok_or("waveform needs --in AUDIO")?;
    let out = a.take("--out")?.ok_or("waveform needs --out PEAKS")?;
    let mut p = odj_audio::waveform::Profile::default();
    fn num<T: std::str::FromStr>(flag: &str, v: Option<String>, into: &mut T) -> Result<(), String> {
        if let Some(v) = v {
            *into = v.parse().map_err(|_| format!("{flag} {v} is not a number"))?;
        }
        Ok(())
    }
    num("--low-hz", a.take("--low-hz")?, &mut p.crossover_low_hz)?;
    num("--high-hz", a.take("--high-hz")?, &mut p.crossover_high_hz)?;
    num("--sections", a.take("--sections")?, &mut p.filter_sections)?;
    num("--columns-per-s", a.take("--columns-per-s")?, &mut p.columns_per_s)?;
    a.done()?;
    let peaks = odj_audio::waveform::peaks_file(Path::new(&path), &p).map_err(|e| e.message)?;
    let bytes: Vec<u8> = peaks.columns.iter().flatten().copied().collect();
    std::fs::write(&out, &bytes).map_err(|e| format!("cannot write {out}: {e}"))?;
    let v = json!({
        "type": "waveform",
        "columns": peaks.columns.len(),
        "sample_rate": peaks.sample_rate,
        "channels": peaks.channels,
    });
    println!("{v}");
    Ok(())
}

/// The one positional argument left after the flags are taken.
fn sole_path(args: &mut Args) -> Result<PathBuf, String> {
    if args.rest.len() != 1 || args.rest[0].starts_with("--") {
        return Err(format!("expected exactly one PATH\n{USAGE}"));
    }
    Ok(PathBuf::from(args.rest.remove(0)))
}

/// Decode a file to raw PCM on stdout, the way the Python analysis lanes
/// read `ffmpeg ... -f s16le -`: little-endian, interleaved, at the file's
/// own rate unless `--rate` resamples it (rubato, as the engine loads it).
/// `--mono` averages the two sides. One JSON line on stderr after the last
/// sample names the rate, channels and frames written, so a reader can tell
/// a complete decode from a truncated pipe.
fn decode_pcm_cmd(mut args: Args) -> Result<(), String> {
    use std::io::Write;
    let rate = args.take("--rate")?.map(|r| r.parse::<u32>().map_err(|_| format!("--rate {r} is not a whole number of Hz"))).transpose()?;
    if rate == Some(0) {
        return Err("--rate must be above 0".into());
    }
    let mono = args.flag("--mono");
    let format = args.take("--format")?.unwrap_or_else(|| "f32le".into());
    if format != "f32le" && format != "s16le" {
        return Err(format!("--format {format} is not f32le or s16le"));
    }
    let path = sole_path(&mut args)?;
    args.done()?;
    let d = match rate {
        Some(r) => odj_audio::decode::decode_at(&path, r),
        None => odj_audio::decode::decode_file(&path),
    }
    .map_err(|e| e.message)?;
    let channels = if mono { 1 } else { 2 };
    let frames = d.pcm.len() / 2;
    let mut out = BufWriter::with_capacity(1 << 20, io::stdout().lock());
    let s16 = format == "s16le";
    let put = |out: &mut BufWriter<io::StdoutLock<'_>>, v: f32| -> io::Result<()> {
        if s16 {
            // ffmpeg's float to s16: scale by 32768, round to nearest, clip.
            let s = (v * 32768.0).round().clamp(-32768.0, 32767.0) as i16;
            out.write_all(&s.to_le_bytes())
        } else {
            out.write_all(&v.to_le_bytes())
        }
    };
    let w = |e: io::Error| format!("cannot write PCM: {e}");
    for f in d.pcm.chunks_exact(2) {
        if mono {
            put(&mut out, (f[0] + f[1]) * 0.5).map_err(w)?;
        } else {
            put(&mut out, f[0]).map_err(w)?;
            put(&mut out, f[1]).map_err(w)?;
        }
    }
    out.flush().map_err(w)?;
    eprintln!("{}", json!({"sample_rate": d.sample_rate, "channels": channels, "frames": frames, "format": format}));
    Ok(())
}

/// Print one JSON line with the file's rate and length. `frames` comes from
/// the header when the container states it ("source": "header"), otherwise
/// from a full decode ("source": "decode"); never an estimate from bitrate.
fn probe_cmd(mut args: Args) -> Result<(), String> {
    let path = sole_path(&mut args)?;
    args.done()?;
    let p = odj_audio::decode::probe_file(&path).map_err(|e| e.message)?;
    let (rate, frames, source) = match (p.sample_rate, p.frames) {
        (Some(r), Some(n)) => (r, n, "header"),
        _ => {
            let d = odj_audio::decode::decode_file(&path).map_err(|e| e.message)?;
            (d.sample_rate, (d.pcm.len() / 2) as u64, "decode")
        }
    };
    let v = json!({
        "sample_rate": rate,
        "frames": frames,
        "duration_s": frames as f64 / rate as f64,
        "delay": p.delay,
        "padding": p.padding,
        "source": source,
    });
    println!("{v}");
    Ok(())
}

/// One JSON line per file: `{"path", "duration", "fingerprint"}` on success,
/// `{"path", "error"}` on failure, so one bad file never hides the rest. The
/// exit status is nonzero when any file failed.
fn fingerprint_cmd(mut args: Args) -> Result<(), String> {
    let length: u32 = match args.take("--length")? {
        Some(v) => v.parse().map_err(|_| format!("--length must be whole seconds, got {v}"))?,
        None => odj_audio::fingerprint::DEFAULT_LENGTH_S,
    };
    let files = std::mem::take(&mut args.rest);
    if files.is_empty() {
        return Err("fingerprint needs at least one FILE".into());
    }
    if let Some(flag) = files.iter().find(|f| f.starts_with("--")) {
        return Err(format!("unknown option {flag}"));
    }
    let mut failed = 0usize;
    for f in &files {
        let line = match odj_audio::fingerprint::fingerprint_file(Path::new(f), length) {
            Ok(fp) => json!({"path": f, "duration": fp.duration_s, "fingerprint": fp.fingerprint}),
            Err(e) => {
                failed += 1;
                json!({"path": f, "error": e.message})
            }
        };
        println!("{line}");
    }
    if failed > 0 {
        return Err(format!("{failed} of {} files could not be fingerprinted", files.len()));
    }
    Ok(())
}

/// Where `record` writes, how long each segment is, and which input. Parsed
/// (and its errors tested) without `device` too, where nothing reads it.
#[cfg_attr(not(feature = "device"), allow(dead_code))]
struct RecordArgs {
    dir: PathBuf,
    segment_seconds: u32,
    select: RecordSelect,
}

#[cfg_attr(not(feature = "device"), allow(dead_code))]
enum RecordSelect {
    Name(String),
    Index(usize),
}

fn parse_record(mut args: Args) -> Result<RecordArgs, String> {
    let dir = PathBuf::from(args.take("--dir")?.ok_or("record needs --dir")?);
    let name = args.take("--device")?;
    let index = args.take("--device-index")?;
    let segment_seconds = match args.take("--segment-seconds")? {
        None => 300,
        Some(s) => s.parse().map_err(|_| format!("--segment-seconds {s} is not a whole number"))?,
    };
    args.done()?;
    let select = match (name, index) {
        (Some(n), None) if !n.is_empty() => RecordSelect::Name(n),
        (None, Some(i)) => RecordSelect::Index(i.parse().map_err(|_| format!("--device-index {i} is not an index"))?),
        _ => return Err("record needs exactly one of --device NAME or --device-index N".into()),
    };
    Ok(RecordArgs { dir, segment_seconds, select })
}

#[cfg(feature = "device")]
fn input_devices_cmd(args: Args) -> Result<(), String> {
    args.done()?;
    let devices = odj_audio::capture::input_devices()?;
    println!("{}", json!({ "devices": devices }));
    Ok(())
}

#[cfg(not(feature = "device"))]
fn input_devices_cmd(args: Args) -> Result<(), String> {
    args.done()?;
    Err("this build has no audio input; rebuild with --features device".into())
}

/// Set `stop` when stdin reaches end of file or reads a `stop` line: the
/// parent closing the pipe (or dying) ends the recording cleanly.
#[cfg(feature = "device")]
fn stop_on_stdin(stop: std::sync::Arc<std::sync::atomic::AtomicBool>) {
    use std::io::BufRead;
    std::thread::spawn(move || {
        for line in io::stdin().lock().lines() {
            match line {
                Ok(l) if l.trim() == "stop" => break,
                Ok(_) => {}
                Err(_) => break,
            }
        }
        stop.store(true, std::sync::atomic::Ordering::Relaxed);
    });
}

#[cfg(feature = "device")]
fn record_cmd(args: Args) -> Result<(), String> {
    use odj_audio::capture::{record, Select};
    use std::io::Write;
    let a = parse_record(args)?;
    let select = match a.select {
        RecordSelect::Name(n) => Select::Name(n),
        RecordSelect::Index(i) => Select::Index(i),
    };
    let stop = std::sync::Arc::new(std::sync::atomic::AtomicBool::new(false));
    stop_on_stdin(stop.clone());
    let stopped = match record(
        &select,
        &a.dir,
        a.segment_seconds,
        stop,
        || {
            println!("{}", json!({ "waiting": "microphone_permission" }));
            let _ = io::stdout().flush();
        },
        |started| {
            println!("{}", json!({ "recording": started }));
            let _ = io::stdout().flush();
        },
    ) {
        Ok(stopped) => stopped,
        Err(e) => {
            // On stdout too, so whoever started the recording can show why
            // it ended (a microphone denied at the prompt, an unplugged input).
            println!("{}", json!({ "failed": e }));
            let _ = io::stdout().flush();
            return Err(e);
        }
    };
    if stopped.dropped_samples > 0 {
        eprintln!("odj-audio: dropped {} samples the writer could not keep up with", stopped.dropped_samples);
    }
    println!("{}", json!({ "stopped": stopped }));
    Ok(())
}

#[cfg(not(feature = "device"))]
fn record_cmd(args: Args) -> Result<(), String> {
    parse_record(args)?;
    Err("this build has no audio input; rebuild with --features device".into())
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
        "waveform" => waveform_cmd(args),
        "fingerprint" => fingerprint_cmd(args),
        "decode" => decode_cmd(args),
        "probe" => probe_cmd(args),
        "input-devices" => input_devices_cmd(args),
        "record" => record_cmd(args),
        "version" => {
            // `commands` lets a caller tell this build from an older one before it
            // runs a subcommand the older one lacks (a stale cargo build in a
            // reused CI workspace, say).
            let v = json!({
                "engine": concat!("odj-audio ", env!("CARGO_PKG_VERSION")),
                "protocol": protocol::PROTOCOL_VERSION,
                "commands": ["decode", "fingerprint", "probe", "render", "serve", "waveform", "input-devices", "record", "version"],
                "capture": cfg!(feature = "device"),
            });
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

    /// A fresh directory, deleted when the guard drops (on panic too). Bind
    /// the guard for the whole test: `let (_guard, d) = dir(..)`.
    fn dir(name: &str) -> (tempfile::TempDir, PathBuf) {
        let guard = tempfile::Builder::new().prefix(&format!("odj-audio-test-claim-{name}-")).tempdir().unwrap();
        let d = guard.path().to_path_buf();
        (guard, d)
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
        let (_guard, d) = dir("alias");
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
    }

    #[test]
    fn an_output_that_became_an_input_during_the_run_is_refused_and_left_whole() {
        // Codex on d959eae8: the reads were asked only before a render, so
        // an output relinked to the plan or a track while it ran was then
        // emptied. The claim asks again, against the output as opened.
        let (_guard, d) = dir("relinked");
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
    }

    #[test]
    fn an_output_linked_to_a_read_renamed_away_is_refused_and_left_whole() {
        // Codex on d07e4db1: only the input paths were asked again, so a
        // track renamed away and replaced at its path, with the output then
        // linked to the file that was read, passed the claim and was emptied.
        let (_guard, d) = dir("renamed");
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
    }

    #[test]
    fn a_render_holds_its_plan_and_tracks_as_opened() {
        let (_guard, d) = dir("render-reads");
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
    }

    #[test]
    fn a_refused_claim_leaves_an_output_another_run_created_first() {
        // Codex on b87a0f49: two runs racing on one absent output both saw
        // it absent, so the second counted the first's file as its own and
        // removed it when a later output of its own was refused.
        let (_guard, d) = dir("raced");
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
    }

    #[test]
    fn a_refused_claim_leaves_what_another_run_put_in_place_of_its_own() {
        // Codex on bb0d2322: creation was decided atomically, but the undo
        // removed by path, so an output this claim created, then replaced by
        // another run, was removed as if it were still this claim's.
        let (_guard, d) = dir("replaced");
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
    }

    // Control: distinct outputs are all claimed, one handle each, in order,
    // and an existing file keeps its bytes until it is written.
    #[test]
    fn distinct_outputs_are_all_claimed() {
        let (_guard, d) = dir("distinct");
        std::fs::write(d.join("keep.wav"), b"old").unwrap();
        let writes = [w(d.join("keep.wav"), "--out"), w(d.join("n").join("deck1.wav"), "deck 1's output"), w(d.join("n").join("deck2.wav"), "deck 2's output")];
        let files = claim_outputs(&writes, &Reads::default()).unwrap();
        assert_eq!(files.len(), 3);
        assert_eq!(std::fs::read(d.join("keep.wav")).unwrap(), b"old");
        assert!(d.join("n").join("deck1.wav").exists() && d.join("n").join("deck2.wav").exists());
    }

    fn args(v: &[&str]) -> Args {
        Args { rest: v.iter().map(|s| s.to_string()).collect() }
    }

    #[test]
    fn record_takes_exactly_one_input_and_a_whole_segment_length() {
        let a = parse_record(args(&["--dir", "/tmp/x", "--device", "BlackHole 2ch"])).unwrap();
        assert_eq!((a.dir, a.segment_seconds), (PathBuf::from("/tmp/x"), 300));
        assert!(matches!(a.select, RecordSelect::Name(ref n) if n == "BlackHole 2ch"));
        let a = parse_record(args(&["--device-index", "3", "--dir", "d", "--segment-seconds", "60"])).unwrap();
        assert!(matches!(a.select, RecordSelect::Index(3)));
        assert_eq!(a.segment_seconds, 60);
        for bad in [
            &["--dir", "d"][..],
            &["--dir", "d", "--device", "A", "--device-index", "1"],
            &["--dir", "d", "--device", ""],
            &["--dir", "d", "--device-index", "x"],
            &["--dir", "d", "--device", "A", "--segment-seconds", "1.5"],
            &["--device", "A"],
            &["--dir", "d", "--device", "A", "--loud"],
        ] {
            assert!(parse_record(args(bad)).is_err(), "{bad:?}");
        }
    }
}
