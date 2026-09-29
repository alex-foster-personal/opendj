//! Render a plan on the fake clock: as fast as the CPU allows, bit for bit the
//! same every time.
//!
//! Blocks are split at every event frame, every ramp step and every timeline
//! step, so what fires when never depends on the block size.
//!
//! Besides the mix, a render reports what a transition scorer needs: which
//! decks are heard over time, where they overlap and how long each plays alone
//! either side, each deck's tempo, and optionally each deck's own audio.

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex, Weak};
use std::time::Instant;

use sha2::{Digest, Sha256};

use crate::deck::Track;
use crate::decode::{decode_open_at, reserve_within, SourceId};
use crate::engine::{DeckId, Engine, EngineCmd, ErrorCode, KnobTarget, MAX_DECKS};
use crate::plan::{Action, At, DeckPos, Over, Plan};
use crate::protocol::{Command, LoadSpec, ProtoError};

/// Ramps move their knob in steps this far apart; the engine's own parameter
/// smoothing glides between steps.
pub const RAMP_STEP_FRAMES: u64 = 32;
/// How close to a deck-relative target counts as reaching it, in source frames.
pub const POS_EPS_FRAMES: f64 = 1e-3;

/// Which decks are heard is sampled this often, and on every frame an event
/// fires, so segment edges are exact for commands and within 10 ms for ramps.
pub const TIMELINE_STEP_MS: u32 = 10;

#[derive(Clone, Debug, PartialEq)]
pub struct Fired {
    pub event: usize,
    pub frame: u64,
}

/// A span of output in which the same decks are heard (see `Deck::audible`).
#[derive(Clone, Debug, PartialEq)]
pub struct Segment {
    pub start: u64,
    pub end: u64,
    /// Bit `d - 1` is set when deck `d` is heard.
    pub decks: u8,
}

/// One deck heard alone, next to an overlap.
#[derive(Clone, Debug, PartialEq)]
pub struct Solo {
    pub deck: DeckId,
    pub frames: u64,
}

/// A run of segments with two or more decks heard, and the solo segment on
/// either side of it when there is one.
#[derive(Clone, Debug, PartialEq)]
pub struct Overlap {
    pub start: u64,
    pub end: u64,
    /// Every deck heard at some point in the overlap, as in `Segment::decks`.
    pub decks: u8,
    pub solo_before: Option<Solo>,
    pub solo_after: Option<Solo>,
}

/// A deck's tempo from `frame` on. `bpm` is the grid's local BPM (or the tag
/// BPM) times the tempo, read where the playhead was at that frame.
#[derive(Clone, Debug, PartialEq)]
pub struct TempoPoint {
    pub deck: DeckId,
    pub frame: u64,
    pub tempo: f64,
    pub bpm: Option<f64>,
}

/// One deck's own audio: after its channel strip, crossfader included, before
/// the master gain. The decks' outputs sum to the mix before master gain.
pub struct DeckOutput {
    pub deck: DeckId,
    /// Interleaved stereo, the same length as the mix.
    pub pcm: Vec<f32>,
    pub sha256: String,
}

#[derive(Clone, Copy, Debug, Default)]
pub struct RenderOptions {
    /// Also return each loaded deck's own audio (memory: one more mix-sized
    /// buffer per deck, so each deck output shortens the longest render the
    /// same amount of memory allows; see `render_ceiling`).
    pub deck_outputs: bool,
}

pub struct RenderOutput {
    pub sample_rate: u32,
    pub frames: u64,
    /// Interleaved stereo.
    pub pcm: Vec<f32>,
    pub fired: Vec<Fired>,
    pub sha256: String,
    pub decode_wall_s: f64,
    pub render_wall_s: f64,
    pub timeline: Vec<Segment>,
    pub overlaps: Vec<Overlap>,
    pub tempo: Vec<TempoPoint>,
    /// True when any deck had Master Tempo on at any point: then a tempo
    /// change on that deck did not move its pitch.
    pub master_tempo: bool,
    /// Empty unless `RenderOptions::deck_outputs`.
    pub decks: Vec<DeckOutput>,
    /// Every file the tracks were decoded from, as opened: what an output
    /// must not be, whatever their paths name by the time it is written.
    pub reads: Vec<SourceId>,
}

impl RenderOutput {
    /// Seconds of audio per second of wall time spent rendering (decode excluded).
    pub fn realtime_factor(&self) -> f64 {
        (self.frames as f64 / self.sample_rate as f64) / self.render_wall_s.max(1e-9)
    }
}

struct ActiveRamp {
    target: KnobTarget,
    from: f64,
    to: f64,
    start: u64,
    len: u64,
    next: u64,
    event: usize,
}

/// A command that sets a knob outright ends any ramp on that knob, so the
/// value it sets stays set, in plan order, as a newer ramp replaces an older
/// one; otherwise the ramp's next step would overwrite it and carry on toward
/// its old target.
fn yield_ramp(ramps: &mut Vec<ActiveRamp>, cmd: &EngineCmd) {
    if let Some(t) = KnobTarget::set_by(cmd) {
        ramps.retain(|r| r.target != t);
    }
}

/// One decoded file: its sample rate, its samples, and the file as opened.
type CachedFile = (u32, Arc<Vec<f32>>, Option<SourceId>);

/// Loads every track the plan names, decoding each distinct file once.
/// Relative paths resolve against `base`. The second argument is the most
/// frames a newly decoded file may hold; a file already decoded shares its
/// samples and costs nothing more, so it is never refused for room.
pub fn file_loader(base: PathBuf) -> impl FnMut(&LoadSpec, u64) -> Result<Arc<Track>, ProtoError> {
    file_loader_at(base, None)
}

/// As `file_loader`, resampling each file to `sample_rate` when given, so
/// the engine plays it at its own rate (the page resamples at decode too).
pub fn file_loader_at(
    base: PathBuf,
    sample_rate: Option<u32>,
) -> impl FnMut(&LoadSpec, u64) -> Result<Arc<Track>, ProtoError> {
    let mut cache: HashMap<FileKey, CachedFile> = HashMap::new();
    move |spec: &LoadSpec, room: u64| {
        let path = base.join(&spec.path);
        let (file, key) = open_keyed(&path)?;
        let (sr, pcm, source) = match cache.get(&key) {
            Some((sr, pcm, source)) => (*sr, pcm.clone(), source.clone()),
            None => {
                let d = decode_open_at(file, &path, sample_rate, room)?;
                let pcm = Arc::new(d.pcm);
                cache.insert(key, (d.sample_rate, pcm.clone(), d.source.clone()));
                (d.sample_rate, pcm, d.source)
            }
        };
        // Every load of one file shares the cached samples; nothing is copied.
        Ok(Arc::new(Track::new(sr, pcm, spec.beats.clone(), spec.bpm).with_source(source)))
    }
}

/// Which file a path names, so loads of one file share one decode. One file
/// reached by two names (`a.wav`, `sub/../a.wav`, a symlink, a hard link, a
/// case-folded spelling on a case-insensitive volume) is one file: on unix
/// that is its device and inode, on Windows its volume serial and file id
/// (as the output collision check compares files there too), and elsewhere,
/// or on a Windows volume that reports no file id, the path with every link
/// resolved.
///
/// It is taken from the open file that is then decoded, never from the path
/// again, so the samples stored under a key are that file's even when the
/// path is renamed or replaced in between.
///
/// Its length and modification time are part of it, so a file rewritten in
/// place, or a new file that is handed a deleted one's inode while a deck
/// still holds the old samples, decodes afresh instead of sharing them. On
/// unix and Windows so is its change time, which every write moves and no
/// tool can set back: a same-length rewrite whose modification time was kept
/// (`cp -p`, `rsync -t`, `touch -r`, a restored timestamp) is a new file too.
/// Two writes inside one tick of the volume's clock still look alike (a
/// volume that keeps no change time, as exFAT, gives none, and there a kept
/// modification time hides a same-length rewrite).
#[derive(Clone, Debug, PartialEq, Eq, Hash)]
struct FileKey {
    id: FileId,
    len: u64,
    modified: Option<std::time::SystemTime>,
    changed: Option<(i64, i64)>,
}

#[derive(Clone, Debug, PartialEq, Eq, Hash)]
enum FileId {
    #[cfg(unix)]
    Inode { dev: u64, ino: u64 },
    #[cfg(windows)]
    Volume { serial: u64, id: [u8; 16] },
    Path(PathBuf),
}

/// Open `path` for a load, keyed by the handle that will be decoded.
fn open_keyed(path: &Path) -> Result<(std::fs::File, FileKey), ProtoError> {
    let file = crate::decode::open(path)?;
    let key = FileKey::of_file(&file, path);
    #[cfg(test)]
    AFTER_OPEN.with(|h| h.get().map(|h| h(path)));
    Ok((file, key))
}

#[cfg(test)]
thread_local! {
    /// Runs once a load has opened and keyed its file, before it decodes it:
    /// the path renamed or replaced just then.
    static AFTER_OPEN: std::cell::Cell<Option<fn(&Path)>> = const { std::cell::Cell::new(None) };
}

impl FileKey {
    /// The key of `file`, opened from `path`.
    fn of_file(file: &std::fs::File, path: &Path) -> FileKey {
        let by_path = || FileId::Path(path.canonicalize().unwrap_or_else(|_| path.to_path_buf()));
        let m = file.metadata().ok();
        #[cfg(unix)]
        let (id, changed) = match &m {
            Some(m) => {
                use std::os::unix::fs::MetadataExt;
                (FileId::Inode { dev: m.dev(), ino: m.ino() }, Some((m.ctime(), m.ctime_nsec())))
            }
            None => (by_path(), None),
        };
        #[cfg(windows)]
        let (id, changed) = (file_id(file).unwrap_or_else(by_path), change_time(file));
        #[cfg(not(any(unix, windows)))]
        let (id, changed) = (by_path(), None);
        FileKey { id, len: m.as_ref().map_or(0, |m| m.len()), modified: m.and_then(|m| m.modified().ok()), changed }
    }

    #[cfg(test)]
    fn of(path: &Path) -> FileKey {
        FileKey::of_file(&std::fs::File::open(path).unwrap(), path)
    }
}

/// The volume serial and file id Windows gives an open file (128 bits, as
/// ReFS needs), or none where the volume reports none.
#[cfg(windows)]
fn file_id(file: &std::fs::File) -> Option<FileId> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{FileIdInfo, GetFileInformationByHandleEx, FILE_ID_INFO};
    let mut info = FILE_ID_INFO::default();
    // SAFETY: the handle is open for the call, and the buffer is a
    // FILE_ID_INFO of the size passed, as FileIdInfo requires.
    let ok = unsafe {
        GetFileInformationByHandleEx(
            file.as_raw_handle() as _,
            FileIdInfo,
            (&mut info as *mut FILE_ID_INFO).cast(),
            std::mem::size_of::<FILE_ID_INFO>() as u32,
        )
    };
    (ok != 0).then_some(FileId::Volume { serial: info.VolumeSerialNumber, id: info.FileId.Identifier })
}

/// The file's change time (100 ns ticks), which Windows moves on every write
/// and `SetFileTime` callers do not set back in practice; std reads it only
/// on nightly.
#[cfg(windows)]
fn change_time(f: &std::fs::File) -> Option<(i64, i64)> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::Storage::FileSystem::{FileBasicInfo, GetFileInformationByHandleEx, FILE_BASIC_INFO};
    let mut info = FILE_BASIC_INFO::default();
    // SAFETY: the handle is open for the call, and the buffer is a
    // FILE_BASIC_INFO of the size passed, as FileBasicInfo requires.
    let ok = unsafe {
        GetFileInformationByHandleEx(
            f.as_raw_handle() as _,
            FileBasicInfo,
            (&mut info as *mut FILE_BASIC_INFO).cast(),
            std::mem::size_of::<FILE_BASIC_INFO>() as u32,
        )
    };
    (ok != 0 && info.ChangeTime != 0).then_some((info.ChangeTime, 0))
}

/// Decoded tracks for a live engine, where loads never end: decks on one file
/// share its samples while any of them holds them, but nothing is kept for a
/// file no deck holds any more, so memory follows the tracks loaded now, not
/// every track the session has visited. Safe to share between load threads:
/// a load of a file already being decoded waits for that decode and shares
/// it, while loads of other files go ahead.
///
/// Each file keeps a `Weak` to every track loaded from it, since any one of
/// them may be the last to hold the samples. A dead `Weak<Track>` keeps only
/// the small `Track` allocation, never the samples, and dead ones are
/// dropped on every load.
#[derive(Default)]
pub struct TrackCache {
    files: Mutex<HashMap<FileKey, Holders>>,
    /// The rate every track is resampled to; None keeps each file's own.
    sample_rate: Option<u32>,
}

/// Every track loaded from one file, locked while that file decodes.
type Holders = Arc<Mutex<Vec<Weak<Track>>>>;

impl TrackCache {
    /// A cache whose tracks play at `sample_rate`.
    pub fn at(sample_rate: u32) -> TrackCache {
        TrackCache { sample_rate: Some(sample_rate), ..TrackCache::default() }
    }

    pub fn load(&self, path: &Path, spec: &LoadSpec) -> Result<Arc<Track>, ProtoError> {
        let (opened, key) = open_keyed(path)?;
        let file = {
            let mut files = self.files.lock().unwrap_or_else(|e| e.into_inner());
            // A file nobody else is loading (only the map holds its slot) and
            // no deck holds is forgotten. Only a load that cloned a slot ever
            // locks it, and a cloned slot is kept without being locked, so
            // this never waits on another file's decode.
            files.retain(|_, f| {
                Arc::strong_count(f) > 1 || f.lock().unwrap_or_else(|e| e.into_inner()).iter().any(|t| t.strong_count() > 0)
            });
            files.entry(key).or_default().clone()
        };
        // Held across the decode: a second load of this file waits here and
        // then finds the first one's track.
        let mut held = file.lock().unwrap_or_else(|e| e.into_inner());
        held.retain(|t| t.strong_count() > 0);
        let (sr, pcm, source) = match held.iter().find_map(Weak::upgrade) {
            Some(t) => (t.sample_rate, t.pcm.clone(), t.source.clone()),
            None => {
                let d = decode_open_at(opened, path, self.sample_rate, u64::MAX)?;
                (d.sample_rate, Arc::new(d.pcm), d.source)
            }
        };
        let track = Arc::new(Track::new(sr, pcm, spec.beats.clone(), spec.bpm).with_source(source));
        held.push(Arc::downgrade(&track));
        Ok(track)
    }
}

/// Loads tracks for the fake-clock session through a `TrackCache`, relative
/// paths resolving against `base`.
pub fn session_loader(base: PathBuf) -> impl FnMut(&LoadSpec) -> Result<Arc<Track>, ProtoError> {
    session_loader_at(base, None)
}

/// As `session_loader`, resampling each file to `sample_rate` when given.
pub fn session_loader_at(base: PathBuf, sample_rate: Option<u32>) -> impl FnMut(&LoadSpec) -> Result<Arc<Track>, ProtoError> {
    let cache = TrackCache { sample_rate, ..TrackCache::default() };
    move |spec: &LoadSpec| cache.load(&base.join(&spec.path), spec)
}

pub fn render_plan_files(plan: &Plan, base: &Path) -> Result<RenderOutput, ProtoError> {
    render_plan(plan, file_loader_at(base.to_path_buf(), Some(plan.sample_rate)))
}

pub fn render_plan_files_with(plan: &Plan, base: &Path, opts: RenderOptions) -> Result<RenderOutput, ProtoError> {
    render_plan_with(plan, file_loader_at(base.to_path_buf(), Some(plan.sample_rate)), opts)
}

fn sha256_hex(pcm: &[f32]) -> String {
    let mut h = Sha256::new();
    for s in pcm {
        h.update(s.to_le_bytes());
    }
    h.finalize().iter().map(|b| format!("{b:02x}")).collect()
}

/// Records which decks are heard and each deck's tempo as the render goes.
struct Observer {
    segments: Vec<Segment>,
    tempo: Vec<TempoPoint>,
    /// Last recorded (tempo, track identity) per deck.
    last: [Option<(f64, usize)>; MAX_DECKS],
}

impl Observer {
    fn new() -> Observer {
        Observer { segments: Vec::new(), tempo: Vec::new(), last: [None; MAX_DECKS] }
    }

    fn observe(&mut self, engine: &Engine, now: u64) {
        let mut mask = 0u8;
        for i in 0..MAX_DECKS {
            let id = i as DeckId + 1;
            let Some(d) = engine.deck(id) else { continue };
            if d.audible() {
                mask |= 1 << i;
            }
            let key = d.track.as_ref().map(|t| (d.tempo, Arc::as_ptr(t) as usize));
            if key.is_some() && key != self.last[i] {
                let t = d.track.as_ref().expect("key is some");
                self.tempo.push(TempoPoint {
                    deck: id,
                    frame: now,
                    tempo: d.tempo,
                    bpm: t.bpm_at(t.frames_to_ms(d.pos)).map(|b| b * d.tempo),
                });
            }
            self.last[i] = key;
        }
        let seg = Segment { start: now, end: now, decks: mask };
        match self.segments.last_mut() {
            Some(l) if l.decks == mask => {}
            // Changed twice on one frame: the earlier state lasted no time.
            Some(l) if l.start == now => {
                l.decks = mask;
                let n = self.segments.len();
                if n >= 2 && self.segments[n - 2].decks == mask {
                    self.segments.pop();
                }
            }
            Some(l) => {
                l.end = now;
                self.segments.push(seg);
            }
            None => self.segments.push(seg),
        }
    }

    fn finish(mut self, end: u64) -> (Vec<Segment>, Vec<Overlap>, Vec<TempoPoint>) {
        if let Some(l) = self.segments.last_mut() {
            l.end = end;
            if l.start == l.end {
                self.segments.pop();
            }
        }
        let overlaps = overlaps(&self.segments);
        (self.segments, overlaps, self.tempo)
    }
}

fn solo(seg: Option<&Segment>) -> Option<Solo> {
    let s = seg?;
    (s.decks.count_ones() == 1).then(|| Solo { deck: s.decks.trailing_zeros() as DeckId + 1, frames: s.end - s.start })
}

fn overlaps(segments: &[Segment]) -> Vec<Overlap> {
    let mut out = Vec::new();
    let mut i = 0;
    while i < segments.len() {
        if segments[i].decks.count_ones() < 2 {
            i += 1;
            continue;
        }
        let first = i;
        let mut decks = 0u8;
        while i < segments.len() && segments[i].decks.count_ones() >= 2 {
            decks |= segments[i].decks;
            i += 1;
        }
        out.push(Overlap {
            start: segments[first].start,
            end: segments[i - 1].end,
            decks,
            solo_before: solo(first.checked_sub(1).map(|k| &segments[k])),
            solo_after: solo(segments.get(i)),
        });
    }
    out
}

fn at_frame_of_ms(ms: f64, sr: u32) -> u64 {
    (ms * sr as f64 / 1000.0).round() as u64
}

/// Frames from now until `at` is due: Some(0) means now, None means it cannot
/// happen from the current state (the deck is empty, paused, or looping
/// short of the target).
/// The deck whose playhead a plan action can start, stop or put somewhere
/// else, if any: the only work that can turn an end out of reach into one
/// within reach. Tempo only changes the speed of a deck already moving (and
/// is never 0), and ramps only move knobs, so neither can.
fn deck_moved_by(action: &Action) -> Option<DeckId> {
    match action {
        Action::Cmd(Command::Load(spec)) => Some(spec.deck),
        Action::Cmd(Command::Apply(
            EngineCmd::Load { deck, .. }
            | EngineCmd::Play { deck, .. }
            | EngineCmd::Cue { deck }
            | EngineCmd::Seek { deck, .. }
            | EngineCmd::Loop { deck, .. }
            | EngineCmd::BeatLoop { deck, .. }
            | EngineCmd::BeatJump { deck, .. },
        )) => Some(*deck),
        _ => None,
    }
}

/// Which events could still bring a deck-relative end within reach: those
/// that move the end's deck, then those that move a deck such an event waits
/// on, and so on. Everything else (a master mute, a fader or tempo change,
/// any ramp, another deck nothing waits on) cannot, so it does not keep a
/// stuck render going. Worked out once from the plan: the answer does not
/// depend on where the render is. An absolute end is always within reach,
/// so nothing is needed for it.
fn end_relevance(plan: &Plan) -> Vec<bool> {
    let mut decks = [false; MAX_DECKS];
    let mut events = vec![false; plan.events.len()];
    let At::Deck { deck, .. } = plan.end else {
        return events;
    };
    decks[deck as usize - 1] = true;
    loop {
        let mut grew = false;
        for (i, ev) in plan.events.iter().enumerate() {
            if events[i] || !deck_moved_by(&ev.action).is_some_and(|d| decks[d as usize - 1]) {
                continue;
            }
            events[i] = true;
            grew = true;
            if let At::Deck { deck, .. } = ev.at {
                decks[deck as usize - 1] = true;
            }
        }
        if !grew {
            return events;
        }
    }
}

fn due_in(at: At, engine: &Engine, now: u64) -> Option<u64> {
    let sr = engine.sample_rate();
    match at {
        At::Frame(f) => Some(f.saturating_sub(now)),
        At::Ms(ms) => Some(at_frame_of_ms(ms, sr).saturating_sub(now)),
        At::Deck { deck, pos } => {
            let d = engine.deck(deck)?;
            let t = d.track.as_ref()?;
            let target_ms = match pos {
                DeckPos::Ms(ms) => Some(ms),
                DeckPos::Bar(b) => t.bar_time_ms(b),
                DeckPos::Beat(b) => t.beat_time_ms(b),
            }?;
            // A thousandth of a frame (about 20 ns) counts as there: the
            // playhead is exact to float rounding, not to zero.
            let target = t.ms_to_frames(target_ms) - POS_EPS_FRAMES;
            if d.pos >= target {
                return Some(0);
            }
            if !d.playing {
                return None;
            }
            if let Some((a, b)) = d.looping {
                if d.pos >= a && d.pos < b && target >= b {
                    return None;
                }
            }
            let step = d.step(sr as f64);
            if step <= 0.0 {
                return None;
            }
            let n = ((target - d.pos) / step).ceil().max(0.0);
            Some(n as u64)
        }
    }
}

fn fail(event: usize, e: ProtoError) -> ProtoError {
    ProtoError::new(e.code, format!("events[{event}]: {}", e.message))
}

/// Everything a render holds at once, in stereo f32 frames: every distinct
/// decoded track (all are decoded up front and kept until the render ends)
/// and every buffer it renders. One WAV file's worth, just under 4 GiB.
const RENDER_BUDGET_FRAMES: u64 = crate::wav::MAX_F32_FRAMES;

/// The most frames a render may run: `max_ms`, and never more than its
/// memory budget leaves once `source_frames` of decoded audio are held,
/// shared between the `held` buffers it renders (the mix, plus one per deck
/// output). Everything rendered is held until it is written, so no buffer
/// outgrows one WAV file, and what a render retains, sources included,
/// stays within the budget however many tracks and deck outputs it has.
fn render_ceiling(max_ms: f64, sr: u32, held: u64, source_frames: u64, budget: u64) -> u64 {
    at_frame_of_ms(max_ms, sr).min(budget.saturating_sub(source_frames) / held.max(1))
}

/// Render `plan`, resolving each `load` through `load`.
pub fn render_plan(
    plan: &Plan,
    load: impl FnMut(&LoadSpec, u64) -> Result<Arc<Track>, ProtoError>,
) -> Result<RenderOutput, ProtoError> {
    render_plan_with(plan, load, RenderOptions::default())
}

pub fn render_plan_with(
    plan: &Plan,
    load: impl FnMut(&LoadSpec, u64) -> Result<Arc<Track>, ProtoError>,
    opts: RenderOptions,
) -> Result<RenderOutput, ProtoError> {
    render_within(plan, load, opts, RENDER_BUDGET_FRAMES)
}

fn render_within(
    plan: &Plan,
    mut load: impl FnMut(&LoadSpec, u64) -> Result<Arc<Track>, ProtoError>,
    opts: RenderOptions,
    budget: u64,
) -> Result<RenderOutput, ProtoError> {
    let sr = plan.sample_rate;
    let mut loads_deck = [false; MAX_DECKS];
    for ev in &plan.events {
        if let Action::Cmd(Command::Load(spec)) = &ev.action {
            loads_deck[spec.deck as usize - 1] = true;
        }
    }
    let deck_buffers = if opts.deck_outputs { loads_deck.iter().filter(|&&l| l).count() as u64 } else { 0 };
    let held = 1 + deck_buffers;
    let end_frame = match plan.end {
        At::Frame(f) => Some(f),
        At::Ms(ms) => Some(at_frame_of_ms(ms, sr)),
        At::Deck { .. } => None,
    };
    // The longest render allowed with `source_frames` of decoded audio held,
    // how long that is, and what limits it, for the messages below.
    let ceiling = |source_frames: u64| {
        let max_frames = render_ceiling(plan.max_ms, sr, held, source_frames, budget);
        let buffers = if deck_buffers == 0 {
            String::new()
        } else {
            format!(" shared by the mix and {deck_buffers} deck outputs")
        };
        let sources = if source_frames == 0 {
            String::new()
        } else {
            format!(" after {source_frames} frames of decoded tracks")
        };
        let limit = format!("max_ms, or what one WAV file holds at {sr} Hz{sources}{buffers}");
        (max_frames, max_frames as f64 * 1000.0 / sr as f64, limit)
    };
    let past = |end: u64, (max_frames, max_ms_held, limit): &(u64, f64, String)| {
        ProtoError::new(
            ErrorCode::Invalid,
            format!(
                "plan.end at frame {end} is past the longest render allowed ({max_frames} frames, {max_ms_held:.0} ms: {limit})"
            ),
        )
    };
    // An absolute end (in ms or frames) past the ceiling is known too far
    // before anything is decoded or held.
    let before = ceiling(0);
    if let Some(end) = end_frame.filter(|&f| f > before.0) {
        return Err(past(end, &before));
    }
    // Decode everything up front, so decode time is reported apart from render
    // time and the render loop itself never waits on IO. Every distinct track
    // stays held until the render ends, so each counts against the budget as
    // it arrives, and the decoding stops at the one that leaves no room.
    let decode_start = Instant::now();
    let mut loaded: HashMap<usize, Arc<Track>> = HashMap::new();
    let mut sources: Vec<*const f32> = Vec::new();
    let mut reads: Vec<SourceId> = Vec::new();
    let mut source_frames = 0u64;
    for (i, ev) in plan.events.iter().enumerate() {
        if let Action::Cmd(Command::Load(spec)) = &ev.action {
            // The most a new track may decode to and still leave the render
            // room: at least one frame per rendered buffer, or the absolute
            // end's worth. The loader stops decoding past it, so a track
            // that does not fit is never held whole.
            let needed = held.saturating_mul(end_frame.unwrap_or(1).max(1));
            let room = budget.saturating_sub(source_frames).saturating_sub(needed);
            let track = load(spec, room).map_err(|e| fail(i, e))?;
            if let Some(s) = track.source.as_ref().filter(|s| !reads.contains(s)) {
                reads.push(s.clone());
            }
            // Tracks decoded from one file share their samples; count them once.
            let samples = track.pcm.as_ptr();
            if !sources.contains(&samples) {
                sources.push(samples);
                // What the samples retain, spare capacity included: a
                // buffer grown geometrically holds more than its frames.
                source_frames += (track.pcm.capacity() / 2) as u64;
                let now = ceiling(source_frames);
                if now.0 == 0 {
                    return Err(fail(
                        i,
                        ProtoError::new(
                            ErrorCode::Invalid,
                            format!(
                                "the decoded tracks so far ({source_frames} frames) leave no room to render within what one WAV file holds ({budget} frames)"
                            ),
                        ),
                    ));
                }
                if let Some(end) = end_frame.filter(|&f| f > now.0) {
                    return Err(fail(i, past(end, &now)));
                }
            }
            loaded.insert(i, track);
        }
    }
    drop(sources);
    let decode_wall_s = decode_start.elapsed().as_secs_f64();
    let (max_frames, max_ms_held, limit) = ceiling(source_frames);

    let mut engine = Engine::new(sr);
    let mut pending: Vec<usize> = (0..plan.events.len()).collect();
    let mut ramps: Vec<ActiveRamp> = Vec::new();
    let mut master_tempo = false;
    let mut fired = Vec::new();
    let mut pcm: Vec<f32> = Vec::new();
    let tl_step = (sr as u64 * TIMELINE_STEP_MS as u64 / 1000).max(1);
    let mut observer = Observer::new();
    let mut scratch: [Vec<f32>; MAX_DECKS] = Default::default();
    let mut deck_pcm: [Vec<f32>; MAX_DECKS] = Default::default();
    let relevant = end_relevance(plan);
    let render_start = Instant::now();

    loop {
        let now = engine.frame();
        let fired_before = fired.len();
        // Fire every due event in plan order. Firing one can make another due
        // (a seek past a bar), so scan until nothing more fires.
        loop {
            let mut any = false;
            let mut i = 0;
            while i < pending.len() {
                let idx = pending[i];
                let ev = &plan.events[idx];
                if due_in(ev.at, &engine, now) != Some(0) {
                    i += 1;
                    continue;
                }
                pending.remove(i);
                any = true;
                fired.push(Fired { event: idx, frame: now });
                match &ev.action {
                    Action::Cmd(Command::Load(spec)) => {
                        let track = loaded.remove(&idx).expect("decoded above");
                        let cmd = EngineCmd::Load { deck: spec.deck, track };
                        yield_ramp(&mut ramps, &cmd);
                        engine.apply(cmd).map_err(|e| fail(idx, e.into()))?;
                    }
                    Action::Cmd(Command::Regrid(spec)) => {
                        let track = engine.regridded(spec.deck, spec.beats.clone(), spec.bpm).map_err(|e| fail(idx, e.into()))?;
                        engine.apply(EngineCmd::Regrid { deck: spec.deck, track }).map_err(|e| fail(idx, e.into()))?;
                    }
                    Action::Cmd(Command::Apply(cmd)) => {
                        master_tempo |= matches!(cmd, EngineCmd::MasterTempo { enabled: true, .. });
                        // Narrowing the pitch range under a tempo ramp would
                        // fail the ramp at the step that crosses the new edge,
                        // blamed on the ramp. The ramp's steps run from where
                        // the tempo is now (inside the new range, or the
                        // command itself fails) to its target, so checking
                        // the target is enough, and fails here, on this event.
                        if let EngineCmd::PitchRange { deck, range } = *cmd {
                            if let Some(r) = ramps.iter().find(|r| r.target == KnobTarget::Tempo(deck)) {
                                if (r.to - 1.0).abs() > range / 100.0 + 1e-9 {
                                    return Err(fail(
                                        idx,
                                        ProtoError::new(
                                            ErrorCode::Invalid,
                                            format!(
                                                "pitch_range +/-{range}% cuts off the tempo ramp of events[{}], still heading to {}",
                                                r.event, r.to
                                            ),
                                        ),
                                    ));
                                }
                            }
                        }
                        yield_ramp(&mut ramps, cmd);
                        engine.apply(cmd.clone()).map_err(|e| fail(idx, e.into()))?;
                    }
                    Action::Cmd(_) => unreachable!("rejected by parse_plan"),
                    Action::Ramp(r) => {
                        let from = engine
                            .knob(r.target)
                            .ok_or_else(|| fail(idx, ProtoError::new(ErrorCode::Invalid, "ramp target deck does not exist")))?;
                        // Both ends inside the pitch range keep every step
                        // inside it, so a ramp that fits fails nowhere later.
                        if let KnobTarget::Tempo(deck) = r.target {
                            let range = engine.deck(deck).map_or(0.0, |d| d.pitch_range);
                            if (r.to - 1.0).abs() > range / 100.0 + 1e-9 {
                                return Err(fail(
                                    idx,
                                    ProtoError::new(
                                        ErrorCode::Invalid,
                                        format!("ramp.to tempo {} is outside the deck's pitch range (+/-{range}%)", r.to),
                                    ),
                                ));
                            }
                        }
                        let len = match r.over {
                            Over::Frames(f) => f,
                            Over::Ms(ms) => at_frame_of_ms(ms, sr).max(1),
                            Over::Beats { deck, beats } => {
                                let d = engine.deck(deck).ok_or_else(|| fail(idx, ProtoError::new(ErrorCode::Invalid, "no such deck")))?;
                                let t = d.track.as_ref().ok_or_else(|| {
                                    fail(idx, ProtoError::new(ErrorCode::NoTrack, "a ramp measured in beats needs a loaded deck"))
                                })?;
                                let no_grid = || fail(idx, ProtoError::new(ErrorCode::NoBeatgrid, "the ramp's deck has no beatgrid or bpm"));
                                let here = t.frames_to_ms(d.pos);
                                let b0 = t.beat_index_at(here).ok_or_else(no_grid)?;
                                let track_ms = t.beat_time_ms(b0 + beats).ok_or_else(no_grid)? - here;
                                at_frame_of_ms(track_ms / d.tempo, sr).max(1)
                            }
                        };
                        // No render runs past what one WAV file holds, so no
                        // ramp longer than that can land. Refusing it keeps
                        // its end frame (start + len) from overflowing.
                        if len > crate::wav::MAX_F32_FRAMES {
                            return Err(fail(
                                idx,
                                ProtoError::new(
                                    ErrorCode::Invalid,
                                    format!(
                                        "ramp.over is {len} frames, longer than any render can run ({} frames, what one WAV file holds)",
                                        crate::wav::MAX_F32_FRAMES
                                    ),
                                ),
                            ));
                        }
                        // A newer ramp on the same knob replaces the older one.
                        ramps.retain(|a| a.target != r.target);
                        ramps.push(ActiveRamp { target: r.target, from, to: r.to, start: now, len, next: now, event: idx });
                    }
                }
            }
            if !any {
                break;
            }
        }

        // Ramp steps due now.
        let mut tempo_stepped = false;
        let mut k = 0;
        while k < ramps.len() {
            let r = &mut ramps[k];
            if r.next == now {
                let t = ((now - r.start) as f64 / r.len as f64).min(1.0);
                let v = r.from + (r.to - r.from) * t;
                let (target, event) = (r.target, r.event);
                let done = now >= r.start + r.len;
                if !done {
                    r.next = (now + RAMP_STEP_FRAMES).min(r.start + r.len);
                }
                engine.apply(target.command(v)).map_err(|e| fail(event, e.into()))?;
                tempo_stepped |= matches!(target, KnobTarget::Tempo(_));
                if done {
                    ramps.remove(k);
                    continue;
                }
            }
            k += 1;
        }

        // A tempo ramp step is recorded on its own frame, so the tempo series
        // matches the varispeed the audio actually got.
        if now.is_multiple_of(tl_step) || fired.len() > fired_before || tempo_stepped {
            observer.observe(&engine, now);
        }

        let end_in = due_in(plan.end, &engine, now);
        if end_in == Some(0) {
            break;
        }
        // Nothing left can bring the end within reach: every pending event
        // that could move a deck the end depends on waits on a deck that
        // cannot get there or falls due after the render budget ends, and the
        // end cannot arrive from this state either (no ramp can change that).
        // Say so now, rather than render (and hold in memory) silence all
        // the way to max_ms. An event due exactly at the budget still fires:
        // events fire before the budget check on their frame.
        let can_fire = |at| due_in(at, &engine, now).is_some_and(|d| now.saturating_add(d) <= max_frames);
        if end_in.is_none() && !pending.iter().any(|&idx| relevant[idx] && can_fire(plan.events[idx].at)) {
            return Err(ProtoError::new(
                ErrorCode::Invalid,
                format!(
                    "plan.end can never be reached: at {} ms no deck is moving toward it and no event or ramp is left that could change that",
                    now * 1000 / sr as u64
                ),
            ));
        }
        if now >= max_frames {
            return Err(ProtoError::new(
                ErrorCode::Invalid,
                format!(
                    "plan.end was not reached within {max_ms_held:.0} ms ({limit})"
                ),
            ));
        }

        let mut n = plan.block_frames as u64;
        for &idx in &pending {
            if let Some(d) = due_in(plan.events[idx].at, &engine, now) {
                n = n.min(d);
            }
        }
        for r in &ramps {
            n = n.min(r.next - now);
        }
        if let Some(d) = due_in(plan.end, &engine, now) {
            n = n.min(d);
        }
        n = n.min(tl_step - now % tl_step);
        n = n.min(max_frames - now).max(1);

        // Every buffer is charged max_frames frames of the budget, so none
        // may hold more than that in spare capacity either.
        let limit = max_frames as usize * 2;
        let start = pcm.len();
        reserve_within(&mut pcm, n as usize * 2, limit);
        pcm.resize(start + n as usize * 2, 0.0);
        if opts.deck_outputs {
            for s in scratch.iter_mut() {
                s.resize(n as usize * 2, 0.0);
            }
            let mut split = scratch.each_mut().map(|v| v.as_mut_slice());
            engine.render_split(&mut pcm[start..], &mut split);
            for i in 0..MAX_DECKS {
                if loads_deck[i] {
                    reserve_within(&mut deck_pcm[i], scratch[i].len(), limit);
                    deck_pcm[i].extend_from_slice(&scratch[i]);
                }
            }
        } else {
            engine.render(&mut pcm[start..]);
        }
    }
    let render_wall_s = render_start.elapsed().as_secs_f64();

    let sha256 = sha256_hex(&pcm);
    let (timeline, overlaps, tempo) = observer.finish(engine.frame());
    let decks = if opts.deck_outputs {
        deck_pcm
            .into_iter()
            .enumerate()
            .filter(|(i, _)| loads_deck[*i])
            .map(|(i, pcm)| DeckOutput { deck: i as DeckId + 1, sha256: sha256_hex(&pcm), pcm })
            .collect()
    } else {
        Vec::new()
    };
    Ok(RenderOutput {
        sample_rate: sr,
        frames: engine.frame(),
        pcm,
        fired,
        sha256,
        decode_wall_s,
        render_wall_s,
        timeline,
        overlaps,
        tempo,
        master_tempo,
        decks,
        reads,
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn a_decode_in_progress_holds_back_only_loads_of_its_own_file() {
        use std::time::Duration;
        // Codex on 8a6342f1 feared the prune step locks every file's entry,
        // including one held through a slow decode, while holding the map.
        // It skips any entry a load holds a clone of, which is the only way
        // an entry is ever locked, so another file's load goes ahead.
        let d = std::env::temp_dir().join(format!("odj-cache-independent-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let write = |name: &str| {
            let p = d.join(name);
            let mut f = std::io::BufWriter::new(std::fs::File::create(&p).unwrap());
            crate::wav::write_f32(&mut f, 48000, &[0.0; 9600]).unwrap();
            p
        };
        let (a, b) = (write("a.wav"), write("b.wav"));
        let spec = LoadSpec { deck: 1, path: String::new(), beats: vec![], bpm: None };
        let cache = Arc::new(TrackCache::default());
        // A load of a.wav mid-decode: its entry cloned out of the map and
        // locked, exactly as `load` holds it across `decode_open_within`.
        let entry = cache.files.lock().unwrap().entry(FileKey::of(&a)).or_default().clone();
        let decoding = entry.lock().unwrap();
        let load_in_thread = |path: PathBuf| {
            let (tx, rx) = std::sync::mpsc::channel();
            let (c, spec) = (cache.clone(), spec.clone());
            std::thread::spawn(move || {
                let _ = tx.send(c.load(&path, &spec).map(|t| t.frames));
            });
            rx
        };
        let other = load_in_thread(b.clone()).recv_timeout(Duration::from_secs(10));
        // Control: a load of a.wav itself waits for that decode, and then
        // goes ahead once it is done.
        let same = load_in_thread(a.clone());
        let waited = same.recv_timeout(Duration::from_millis(300)).is_err();
        drop(decoding);
        let after = same.recv_timeout(Duration::from_secs(10));
        assert!(other.expect("a load of b.wav waited on a.wav's decode").unwrap() > 0);
        assert!(waited, "a load of a.wav did not wait for the decode of a.wav in progress");
        assert!(after.expect("a load of a.wav never went ahead").unwrap() > 0);
        // And the prune still forgets what nobody holds: with every track
        // dropped, a load leaves only its own file in the map, while a file
        // a deck still holds is kept.
        drop(entry);
        let held_b = cache.load(&b, &spec).unwrap();
        assert_eq!(cache.files.lock().unwrap().len(), 1, "a file nobody holds was kept");
        cache.load(&a, &spec).unwrap();
        assert_eq!(cache.files.lock().unwrap().len(), 2, "a file a deck holds was forgotten");
        drop(held_b);
        let _ = std::fs::remove_dir_all(&d);
    }

    #[cfg(unix)]
    #[test]
    fn samples_are_cached_under_the_file_they_were_decoded_from() {
        // Codex on b1f97c9d: the key was read from the path before the
        // decoder opened it again, so a path repointed in between stored the
        // new file's samples under the old file's key, and a later load of
        // the old file was handed them. A symlink repointed is that case
        // without touching either file (a rename or unlink would move the
        // old file's change time, and with it its key).
        fn write(p: &Path, v: f32) {
            let mut f = std::io::BufWriter::new(std::fs::File::create(p).unwrap());
            crate::wav::write_f32(&mut f, 48000, &[v; 1920]).unwrap();
        }
        fn point(link: &Path, to: &str) {
            let tmp = link.with_file_name("cur.tmp");
            let _ = std::fs::remove_file(&tmp);
            std::os::unix::fs::symlink(to, &tmp).unwrap();
            std::fs::rename(&tmp, link).unwrap();
        }
        fn repoint(p: &Path) {
            if p.file_name().is_some_and(|n| n == "cur.wav") {
                point(p, "b.wav");
            }
        }
        let d = std::env::temp_dir().join(format!("odj-cache-swap-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        write(&d.join("a.wav"), 0.25);
        write(&d.join("b.wav"), 0.5);
        let spec = |path: &str| LoadSpec { deck: 1, path: path.into(), beats: vec![], bpm: None };
        let cache = TrackCache::default();
        let mut offline = file_loader(d.clone());
        type Load<'a> = Box<dyn FnMut(&str) -> Arc<Track> + 'a>;
        let loaders: [(&str, Load); 2] = [
            ("session", Box::new(|p: &str| cache.load(&d.join(p), &spec(p)).unwrap())),
            ("offline", Box::new(|p: &str| offline(&spec(p), u64::MAX).unwrap())),
        ];
        for (name, mut load) in loaders {
            point(&d.join("cur.wav"), "a.wav");
            AFTER_OPEN.with(|h| h.set(Some(repoint)));
            let first = load("cur.wav");
            AFTER_OPEN.with(|h| h.set(None));
            let a = load("a.wav");
            assert_eq!(a.pcm[0], 0.25, "{name}: a.wav was handed the samples of the file put in its place");
            assert_eq!(first.pcm[0], 0.25, "{name}: the load did not decode the file it opened");
            // Control: a.wav is the file decoded, so it shares those samples,
            // and the file the path names now gets its own.
            assert!(Arc::ptr_eq(&first.pcm, &a.pcm), "{name}: a.wav was decoded again");
            assert_eq!(load("cur.wav").pcm[0], 0.5, "{name}: the repointed path was handed the old samples");
        }
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_track_shared_from_either_cache_names_the_file_it_was_decoded_from() {
        // A second load of one file shares the first one's samples; it must
        // share what they were read from too, or a caller asking the track
        // it was handed would find nothing read.
        let d = std::env::temp_dir().join(format!("odj-cache-source-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let p = d.join("a.wav");
        let mut f = std::io::BufWriter::new(std::fs::File::create(&p).unwrap());
        crate::wav::write_f32(&mut f, 48000, &[0.0; 960]).unwrap();
        drop(f);
        let id = SourceId::of(&std::fs::File::open(&p).unwrap()).unwrap();
        let spec = LoadSpec { deck: 1, path: "a.wav".into(), beats: vec![], bpm: None };
        let cache = TrackCache::default();
        let first = cache.load(&p, &spec).unwrap();
        let second = cache.load(&p, &spec).unwrap();
        assert!(Arc::ptr_eq(&first.pcm, &second.pcm), "the second load was not a cache hit");
        assert_eq!((first.source.as_ref(), second.source.as_ref()), (Some(&id), Some(&id)));
        let mut load = file_loader(d.clone());
        let (first, second) = (load(&spec, u64::MAX).unwrap(), load(&spec, u64::MAX).unwrap());
        assert!(Arc::ptr_eq(&first.pcm, &second.pcm), "the second load was not a cache hit");
        assert_eq!((first.source.as_ref(), second.source.as_ref()), (Some(&id), Some(&id)));
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_render_never_runs_past_what_its_wav_file_holds() {
        let max = RENDER_BUDGET_FRAMES;
        // The default 4 h ceiling is more than a WAV file holds at 48 kHz
        // (about 3.1 h), so the file's limit is the ceiling there.
        assert_eq!(render_ceiling(crate::plan::DEFAULT_MAX_MS, 48000, 1, 0, max), max);
        assert_eq!(render_ceiling(1e300, 384000, 1, 0, max), max);
        // Control: a max_ms inside the limit is the ceiling itself.
        assert_eq!(render_ceiling(3_600_000.0, 48000, 1, 0, max), 3600 * 48000);
        assert_eq!(render_ceiling(4.0 * 3_600_000.0, 8000, 1, 0, max), 4 * 3600 * 8000);
    }

    /// A plan loading decks 1 and 2 that ends at absolute frame `end`.
    fn two_deck_plan(end: u64) -> Plan {
        crate::plan::parse_plan(&serde_json::json!({
            "end": {"frame": end},
            "events": [
                {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a.wav"}},
                {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 2, "path": "b.wav"}},
            ]
        }))
        .unwrap()
    }

    #[test]
    fn deck_outputs_share_one_wavs_worth_of_memory_with_the_mix() {
        // Codex's case: four deck outputs each as long as a WAV file holds
        // would retain about 20 GiB. The mix and every deck output now share
        // one file's worth, so with two decks each gets a third.
        let max = RENDER_BUDGET_FRAMES;
        assert_eq!(render_ceiling(1e300, 48000, 5, 0, max), max / 5);
        // The loader answers only once the plan is past the preflight, so an
        // error from it means the render was allowed to start.
        let reached = |_: &LoadSpec, _: u64| -> Result<Arc<Track>, ProtoError> { Err(ProtoError::new(ErrorCode::Io, "reached decode")) };
        let with_decks = RenderOptions { deck_outputs: true };
        let Err(e) = render_plan_with(&two_deck_plan(max / 3 + 1), reached, with_decks) else { panic!() };
        assert!(e.message.contains("past the longest render allowed") && e.message.contains("2 deck outputs"), "{}", e.message);
        // Controls: at the shared limit it starts, and without deck outputs
        // the mix alone has the whole file.
        let Err(e) = render_plan_with(&two_deck_plan(max / 3), reached, with_decks) else { panic!() };
        assert!(e.message.contains("reached decode"), "{}", e.message);
        let Err(e) = render_plan_with(&two_deck_plan(max / 3 + 1), reached, RenderOptions::default()) else { panic!() };
        assert!(e.message.contains("reached decode"), "{}", e.message);
    }

    /// A loader handing out in-memory tracks of `frames` frames by path, one
    /// shared sample buffer per path, counting how many loads it answered.
    /// It ignores the room it is given, so the render's own checks are what
    /// a test of it sees.
    fn memory_loader(frames: usize, calls: &std::cell::Cell<usize>) -> impl FnMut(&LoadSpec, u64) -> Result<Arc<Track>, ProtoError> + '_ {
        let mut cache: HashMap<String, Arc<Vec<f32>>> = HashMap::new();
        move |spec: &LoadSpec, _room: u64| {
            calls.set(calls.get() + 1);
            let pcm = cache.entry(spec.path.clone()).or_insert_with(|| vec![0.0f32; frames * 2].into()).clone();
            Ok(Arc::new(Track::new(48000, pcm, vec![], None)))
        }
    }

    fn plan_of(end: serde_json::Value, paths: &[&str]) -> Plan {
        let events: Vec<_> = paths
            .iter()
            .enumerate()
            .map(|(i, p)| serde_json::json!({"at": {"ms": 0}, "cmd": {"type": "load", "deck": i + 1, "path": p}}))
            .collect();
        crate::plan::parse_plan(&serde_json::json!({"end": end, "events": events})).unwrap()
    }

    #[test]
    fn decoded_tracks_count_against_the_render_budget() {
        // Codex's case: the ceiling counted only the rendered buffers while
        // every decoded track stays held too, so a long track plus a long mix
        // could retain twice the budget. Here the budget is 48000 frames and
        // each track 20000.
        let budget = 48000;
        let calls = std::cell::Cell::new(0);
        let run = |end: u64, paths: &[&str], opts: RenderOptions| {
            calls.set(0);
            render_within(&plan_of(serde_json::json!({"frame": end}), paths), memory_loader(20000, &calls), opts, budget)
        };
        let e = run(28001, &["a"], RenderOptions::default()).err().unwrap();
        assert!(e.message.contains("past the longest render allowed (28000 frames") && e.message.contains("20000 frames of decoded tracks"), "{}", e.message);
        assert_eq!(run(28000, &["a"], RenderOptions::default()).unwrap().frames, 28000);
        // One file loaded twice holds one copy, so it counts once.
        assert_eq!(run(28000, &["a", "a"], RenderOptions::default()).unwrap().frames, 28000);
        // Two files hold two.
        assert!(run(8001, &["a", "b"], RenderOptions::default()).is_err());
        assert_eq!(run(8000, &["a", "b"], RenderOptions::default()).unwrap().frames, 8000);
        // Deck outputs share what the sources leave.
        let decks = RenderOptions { deck_outputs: true };
        assert!(run(14001, &["a"], decks).is_err());
        assert_eq!(run(14000, &["a"], decks).unwrap().frames, 14000);
        // Tracks that fill the budget stop the decoding at the one that does,
        // before the rest are decoded.
        let e = run(1, &["a", "b", "c", "d"], RenderOptions::default()).err().unwrap();
        assert!(e.message.starts_with("events[2]") && e.message.contains("leave no room"), "{}", e.message);
        assert_eq!(calls.get(), 3);
        // A deck-relative end is bounded by the same ceiling while rendering.
        let playing = crate::plan::parse_plan(&serde_json::json!({"end": {"deck": 1, "position_ms": 10000}, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a"}},
            {"at": {"ms": 0}, "cmd": {"type": "play", "deck": 1, "playing": true}}]}))
        .unwrap();
        let e = render_within(&playing, memory_loader(1_000_000, &calls), RenderOptions::default(), 1_010_000).err().unwrap();
        assert!(e.message.contains("not reached within") && e.message.contains("1000000 frames of decoded tracks"), "{}", e.message);
    }

    #[test]
    fn a_track_is_charged_what_its_buffer_retains() {
        // Codex on 6a4cfb88: a decoded buffer grown geometrically keeps
        // spare capacity, and a track was charged only its frames. Here the
        // track has 20000 frames in a buffer with room for 30000, so of a
        // 48000-frame budget 18000 are left to render, not 28000.
        let spare = |_: &LoadSpec, _: u64| -> Result<Arc<Track>, ProtoError> {
            let mut pcm = Vec::with_capacity(30000 * 2);
            pcm.resize(20000 * 2, 0.0f32);
            Ok(Arc::new(Track::new(48000, pcm, vec![], None)))
        };
        let run = |end: u64| render_within(&plan_of(serde_json::json!({"frame": end}), &["a"]), spare, RenderOptions::default(), 48000);
        let e = run(18001).err().unwrap();
        assert!(e.message.contains("past the longest render allowed (18000 frames"), "{}", e.message);
        assert_eq!(run(18000).unwrap().frames, 18000);
    }

    #[test]
    fn rendered_buffers_hold_no_more_than_the_budget_charged_them() {
        // Codex's case: the budget counted frames, but each buffer grew by
        // doubling, so one could hold nearly twice what it was charged. Here
        // the budget leaves 100000 frames to the mix, and doubling past
        // 99000 frames would hold 262144 samples.
        let calls = std::cell::Cell::new(0);
        let out = render_within(&plan_of(serde_json::json!({"frame": 99000}), &["a"]), memory_loader(60000, &calls), RenderOptions::default(), 160000).unwrap();
        assert_eq!(out.pcm.len(), 198000);
        assert!(out.pcm.capacity() <= 200000, "the mix holds {} samples", out.pcm.capacity());
        // Deck outputs are charged the same share as the mix.
        let decks = RenderOptions { deck_outputs: true };
        let out = render_within(&plan_of(serde_json::json!({"frame": 49000}), &["a"]), memory_loader(60000, &calls), decks, 160000).unwrap();
        assert!(out.pcm.capacity() <= 100000, "the mix holds {} samples", out.pcm.capacity());
        assert!(out.decks[0].pcm.capacity() <= 100000, "deck 1 holds {} samples", out.decks[0].pcm.capacity());
        // Control: growth stays geometric below the limit, not one block at a
        // time, so a long render does not copy itself once per block.
        let mut v = Vec::new();
        let mut grew = 0;
        for _ in 0..1000 {
            let cap = v.capacity();
            reserve_within(&mut v, 64, usize::MAX);
            v.resize(v.len() + 64, 0.0);
            grew += usize::from(v.capacity() != cap);
        }
        assert!(grew <= 12, "grew {grew} times for 1000 appends");
        // Samples the limit is too small for still get their room.
        let mut v = vec![0.0f32; 10];
        reserve_within(&mut v, 20, 15);
        assert!(v.capacity() >= 30);
    }

    #[test]
    fn each_load_is_told_the_room_the_render_leaves() {
        // Codex's case: the budget was checked only once a track had been
        // decoded whole, so a long file after others could be held in full
        // past the budget before being refused. Each load is now given the
        // room left, and the file loader stops decoding past it.
        let rooms = std::cell::RefCell::new(Vec::new());
        let calls = std::cell::Cell::new(0);
        let mut inner = memory_loader(20000, &calls);
        let spy = |spec: &LoadSpec, room: u64| {
            rooms.borrow_mut().push(room);
            inner(spec, room)
        };
        let _ = render_within(&plan_of(serde_json::json!({"frame": 1000}), &["a", "a", "b"]), spy, RenderOptions::default(), 48000);
        // 48000 less 1000 for the end; then less a's 20000 (twice, since a
        // repeat is a cache hit that costs nothing more); then less b's.
        assert_eq!(*rooms.borrow(), vec![47000, 27000, 27000]);
        // With deck outputs and a deck-relative end, one frame per buffer.
        rooms.borrow_mut().clear();
        let mut inner = memory_loader(20000, &calls);
        let spy = |spec: &LoadSpec, room: u64| {
            rooms.borrow_mut().push(room);
            inner(spec, room)
        };
        let deck_end = crate::plan::parse_plan(&serde_json::json!({"end": {"deck": 1, "position_ms": 0}, "events": [
            {"at": {"ms": 0}, "cmd": {"type": "load", "deck": 1, "path": "a"}}]}))
        .unwrap();
        let _ = render_within(&deck_end, spy, RenderOptions { deck_outputs: true }, 48000);
        assert_eq!(*rooms.borrow(), vec![47998]);
    }
}
