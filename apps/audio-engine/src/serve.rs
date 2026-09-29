//! `odj-audio serve`: the engine as a long-lived process speaking protocol v1
//! on stdin and stdout.
//!
//! Three clocks drive the same engine:
//!
//! - **fake**: single thread. Time moves only on `engine_advance`, rendering as
//!   fast as the CPU allows. Same input lines, same audio, every time: this is
//!   the headless simulator agents train against.
//! - **wall**: an audio thread paced by the wall clock into a null sink, for CI
//!   and headless boxes with no sound card.
//! - **device** (`--features device`): the audio thread is the device callback.
//!
//! In the threaded modes the audio side owns the engine and talks to the rest
//! only through lock-free rings: commands in, results and snapshots out.
//! Loads decode on worker threads; a deck's later commands wait behind its own
//! pending load so the mailbox order holds per deck, while other decks and the
//! mixer stay responsive.

use std::collections::{HashMap, VecDeque};
use std::io::{self, BufRead, Write};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::mpsc;
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant};

use serde_json::Value;

use crate::deck::Track;
use crate::engine::{DeckId, Engine, EngineCmd, ErrorCode, Rejected, Retired, Snapshot, MAX_BLOCK, MAX_DECKS};
use crate::offline::TrackCache;
use crate::protocol::{self, Advance, Command, HostTime, LoadSpec, ProtoError};

/// The longest single `engine_advance`, in seconds of audio. The session
/// answers nothing else until an advance is rendered, so a longer one (or a
/// frame count near u64::MAX) would hold it for good.
pub const MAX_ADVANCE_S: u64 = 3600;

/// State messages per second in the threaded modes.
pub const STATE_HZ: u32 = 30;

/// Slots in the command ring from the control side to the audio side.
const CMD_SLOTS: usize = 1024;

/// How long shutdown waits for the audio side to apply what is queued.
const DRAIN_LIMIT: Duration = Duration::from_secs(2);
/// How long shutdown waits for loads still decoding; after that they and the
/// work queued behind them are refused, so each still gets a result.
const LOAD_DRAIN_LIMIT: Duration = Duration::from_secs(5);

/// The most work one deck may park behind its pending load: the mailbox's
/// bound less the slot the load itself takes when it finishes, so an empty
/// mailbox takes the load and everything parked behind it. Past it a
/// command is refused as the mailbox refuses one.
const QUEUE_SLOTS: usize = CMD_SLOTS - 1;

/// The longest line read from stdin, its newline aside. A load's beatgrid is
/// the largest thing a command carries, at tens of bytes a beat, so this
/// holds hundreds of thousands of beats; a longer line is skipped without
/// being held whole, and refused.
pub const MAX_LINE_BYTES: usize = 8 << 20;

/// On the threaded clocks, how many lines, and how many bytes of them, the
/// stdin reader may hand the control side before it has handled them. Past
/// either the reader stops reading, so a supervisor writing faster than the
/// engine takes commands is held back by its pipe, not by this process's
/// memory.
const LINE_SLOTS: usize = CMD_SLOTS;
const LINE_BYTES: usize = 4 * MAX_LINE_BYTES;

/// One line from stdin, read within `MAX_LINE_BYTES`.
enum ReadLine {
    Line(String),
    TooLong,
    Eof,
}

fn read_line(input: &mut impl BufRead, buf: &mut Vec<u8>) -> io::Result<ReadLine> {
    buf.clear();
    let n = io::Read::take(&mut *input, MAX_LINE_BYTES as u64 + 1).read_until(b'\n', buf)?;
    if n == 0 {
        return Ok(ReadLine::Eof);
    }
    if buf.last() == Some(&b'\n') {
        buf.pop();
        if buf.last() == Some(&b'\r') {
            buf.pop();
        }
    } else if buf.len() > MAX_LINE_BYTES {
        // Skip the rest of the line a buffer at a time, holding none of it.
        loop {
            let (done, used) = {
                let b = input.fill_buf()?;
                match b.iter().position(|&c| c == b'\n') {
                    _ if b.is_empty() => (true, 0),
                    Some(i) => (true, i + 1),
                    None => (false, b.len()),
                }
            };
            input.consume(used);
            if done {
                return Ok(ReadLine::TooLong);
            }
        }
    }
    String::from_utf8(std::mem::take(buf))
        .map(ReadLine::Line)
        .map_err(|e| io::Error::new(io::ErrorKind::InvalidData, e))
}

fn too_long() -> ProtoError {
    ProtoError::new(ErrorCode::Invalid, format!("a line longer than {MAX_LINE_BYTES} bytes was skipped"))
}

/// Lines the reader has handed over and the control side not yet handled.
#[derive(Default)]
struct InFlight {
    held: Mutex<(usize, usize)>,
    room: std::sync::Condvar,
}

impl InFlight {
    /// Wait until a line of `len` bytes fits (one line always fits alone).
    fn take(&self, len: usize) {
        let mut held = self.held.lock().unwrap();
        while held.0 >= LINE_SLOTS || (held.0 > 0 && held.1 + len > LINE_BYTES) {
            held = self.room.wait(held).unwrap();
        }
        held.0 += 1;
        held.1 += len;
    }

    fn give_back(&self, len: usize) {
        let mut held = self.held.lock().unwrap();
        held.0 -= 1;
        held.1 -= len;
        self.room.notify_one();
    }
}

fn send(out: &mut impl Write, v: &Value) -> io::Result<()> {
    serde_json::to_writer(&mut *out, v)?;
    out.write_all(b"\n")?;
    out.flush()
}

/// Serve on the fake clock. `record`, when given, receives every rendered
/// frame (interleaved stereo), for writing the session out afterwards.
pub fn serve_fake(
    input: impl BufRead,
    mut out: impl Write,
    sample_rate: u32,
    mut load: impl FnMut(&LoadSpec) -> Result<Arc<Track>, ProtoError>,
    mut record: Option<&mut Vec<f32>>,
) -> io::Result<()> {
    let mut engine = Engine::new(sample_rate);
    let mut scratch = vec![0.0f32; MAX_BLOCK * 2];
    send(&mut out, &protocol::hello_json("fake", sample_rate))?;
    let mut input = input;
    let mut buf = Vec::new();
    loop {
        let line = match read_line(&mut input, &mut buf)? {
            ReadLine::Line(l) => l,
            ReadLine::TooLong => {
                send(&mut out, &protocol::result_json(None, &Err(too_long())))?;
                continue;
            }
            ReadLine::Eof => break,
        };
        if line.trim().is_empty() {
            continue;
        }
        let (id, parsed) = protocol::parse_line(&line);
        let mut then_state = false;
        let mut stop = false;
        let res: Result<(), ProtoError> = parsed.and_then(|cmd| match cmd {
            Command::Load(spec) => {
                let track = load(&spec)?;
                engine.apply(EngineCmd::Load { deck: spec.deck, track }).map(drop).map_err(Into::into)
            }
            Command::Apply(c) => engine.apply(c).map(drop).map_err(Into::into),
            Command::NoOp => Ok(()),
            Command::Advance(a) => {
                let recorded = record.as_deref().map(|r| r.len() as u64 / 2);
                let mut left = advance_frames(a, sample_rate, recorded)?;
                while left > 0 {
                    let n = left.min(MAX_BLOCK as u64) as usize;
                    engine.render(&mut scratch[..n * 2]);
                    if let Some(r) = record.as_deref_mut() {
                        r.extend_from_slice(&scratch[..n * 2]);
                    }
                    left -= n as u64;
                }
                then_state = true;
                Ok(())
            }
            Command::State => {
                then_state = true;
                Ok(())
            }
            Command::Shutdown => {
                stop = true;
                Ok(())
            }
        });
        send(&mut out, &protocol::result_json(id.as_ref(), &res))?;
        if then_state {
            send(&mut out, &protocol::state_json(&engine.snapshot(), None))?;
        }
        if stop {
            break;
        }
    }
    Ok(())
}

/// Frames an `engine_advance` renders, refused before any of them are when it
/// runs past MAX_ADVANCE_S or would take a recording (`recorded` frames so
/// far) past what its WAV file can hold.
fn advance_frames(a: Advance, sample_rate: u32, recorded: Option<u64>) -> Result<u64, ProtoError> {
    let limit = MAX_ADVANCE_S * sample_rate as u64;
    let frames = match a {
        Advance::Frames(f) => f,
        // A huge ms saturates the cast to u64::MAX, which the bound refuses.
        Advance::Ms(ms) => (ms * sample_rate as f64 / 1000.0).round() as u64,
    };
    if frames > limit {
        return Err(ProtoError::new(
            ErrorCode::Invalid,
            format!("engine_advance may move at most {MAX_ADVANCE_S} s ({limit} frames) at a time"),
        ));
    }
    if let Some(done) = recorded {
        if done + frames > crate::wav::MAX_F32_FRAMES {
            return Err(ProtoError::new(
                ErrorCode::Invalid,
                format!(
                    "engine_advance would take the recording past {} frames, the most a WAV file holds",
                    crate::wav::MAX_F32_FRAMES
                ),
            ));
        }
    }
    Ok(frames)
}

/// Where results and state go: stdout in the binary, a buffer in tests.
type Out = Arc<Mutex<Box<dyn Write + Send>>>;

/// What the audio side sends back for each command.
type AudioResult = (u64, Result<Retired, Rejected>);

/// The half of the engine that lives on the audio thread. Everything it
/// touches in `process` is preallocated.
pub struct AudioSide {
    engine: Engine,
    cmd_rx: rtrb::Consumer<(u64, EngineCmd)>,
    res_tx: rtrb::Producer<AudioResult>,
    state_tx: rtrb::Producer<(Snapshot, u64)>,
    state_req: Arc<AtomicBool>,
    frames_per_state: u64,
    frames_since_state: u64,
    epoch: Instant,
    /// How far past now the first frame of the next `process` call will be
    /// heard: the device's output latency plus whatever of its buffer is
    /// already rendered. Zero on the wall clock.
    ahead_ns: u64,
    scratch: Vec<f32>,
    /// Runs after a block renders, before its state request is settled: the
    /// moment a request arriving mid-block used to be lost.
    #[cfg(test)]
    mid_block: Option<fn(&AtomicBool)>,
}

impl AudioSide {
    /// Render `frames` frames into `self.scratch` after applying queued
    /// commands, publishing a snapshot when one is due. Returns the rendered
    /// interleaved stereo.
    /// Set before `process` by a clock whose output is heard later than it is
    /// rendered, so state carries the time its position is actually heard.
    pub fn set_ahead_ns(&mut self, ns: u64) {
        self.ahead_ns = ns;
    }

    pub fn process(&mut self, frames: usize) -> &[f32] {
        // Take a state request before this block's commands: every command
        // the control side sent before asking is in the ring by then, so
        // this block's state reflects them. A request that arrives later in
        // the block stays armed for the next one, never answered by a state
        // that predates it.
        let mut requested = self.state_req.swap(false, Ordering::Acquire);
        // Take a command only while its result has a slot to go to: a result
        // dropped here would free a retired track on this thread. With the
        // result ring full, the rest wait in the command ring until the pump
        // drains it, so nothing is lost and nothing is freed here.
        while self.res_tx.slots() > 0 {
            let Ok((seq, cmd)) = self.cmd_rx.pop() else { break };
            let r = self.engine.apply(cmd);
            let pushed = self.res_tx.push((seq, r));
            debug_assert!(pushed.is_ok(), "a slot was checked free and this is the only producer");
        }
        // Commands held back by a full result ring may have been sent before
        // the request: leave it for a block that has applied them.
        if requested && !self.cmd_rx.is_empty() {
            self.state_req.store(true, Ordering::Relaxed);
            requested = false;
        }
        let n = frames.min(self.scratch.len() / 2);
        let now_ns = self.epoch.elapsed().as_nanos() as u64 + self.ahead_ns;
        // Render up to each state boundary and publish there, so the feed
        // keeps its STATE_HZ cadence whatever the block size: a block longer
        // than the interval (1024 frames at 8 kHz, a large device buffer)
        // publishes once per interval it spans, each at its own frame.
        let mut done = 0;
        // Whether a state went out at the end of this block (not merely was
        // due: a full ring drops it).
        let mut published_at_end = false;
        while done < n {
            let to_boundary = self.frames_per_state.saturating_sub(self.frames_since_state).max(1);
            let chunk = ((n - done) as u64).min(to_boundary) as usize;
            self.engine.render(&mut self.scratch[done * 2..(done + chunk) * 2]);
            done += chunk;
            self.frames_since_state += chunk as u64;
            published_at_end = false;
            if self.frames_since_state >= self.frames_per_state {
                published_at_end = self.publish(now_ns, done);
            }
        }
        // A state the control side asked for (after a load or a command
        // that wants one) goes out at the end of the block, once. With the
        // state ring full (stdout slow) the request stays armed for the next
        // block, so an `engine_state` answered ok always gets its state.
        #[cfg(test)]
        if let Some(hook) = self.mid_block {
            hook(&self.state_req);
        }
        if requested && !published_at_end && !self.publish(now_ns, n) {
            self.state_req.store(true, Ordering::Relaxed);
        }
        &self.scratch[..n * 2]
    }

    /// Publish the engine's state as of `frames_in` frames into this block,
    /// heard at `now_ns` (now plus the clock's lead) plus those frames.
    /// False when the state ring is full and the state was dropped.
    fn publish(&mut self, now_ns: u64, frames_in: usize) -> bool {
        self.frames_since_state = 0;
        let in_ns = frames_in as u64 * 1_000_000_000 / self.engine.sample_rate() as u64;
        self.state_tx.push((self.engine.snapshot(), now_ns + in_ns)).is_ok()
    }
}

enum Msg {
    Line(String),
    /// A line past `MAX_LINE_BYTES`, skipped by the reader.
    TooLong,
    Decoded { seq: u64, deck: DeckId, result: Result<Arc<Track>, ProtoError> },
    Eof,
    /// The audio side returned. Before `stop` is set that means it failed to
    /// start or died, e.g. the output device would not open.
    AudioExited,
    /// A write to the output failed: whoever reads results and state is gone,
    /// so nothing the engine does can be reported any more.
    OutputClosed,
}

/// Work parked behind a deck's pending load.
enum Queued {
    Cmd(Option<Value>, EngineCmd),
    Load(Option<Value>, LoadSpec),
    /// An `engine_state` sent after the work ahead of it here: it is asked
    /// of the audio side only once this has passed on every deck it waits on.
    Fence(u64),
    /// The deck's decoded load, by its seq, waiting for room in the mailbox
    /// that other decks' traffic has taken.
    Ready(u64, EngineCmd),
}

/// The control half: owns the command ring's producer, the per-deck load
/// queues and the id bookkeeping. Runs on the main thread.
struct Control {
    cmd_tx: rtrb::Producer<(u64, EngineCmd)>,
    ids: Arc<Mutex<HashMap<u64, Option<Value>>>>,
    out: Out,
    msg_tx: mpsc::Sender<Msg>,
    next_seq: u64,
    /// Per deck: Some while a load is decoding, holding work queued behind
    /// it, and after it decodes for as long as that work waits for room in
    /// the mailbox (released in order as the audio side drains it).
    waiting: [Option<VecDeque<Queued>>; MAX_DECKS],
    /// Per deck: the seq of the load that is decoding, while `waiting` is Some.
    loading: [Option<u64>; MAX_DECKS],
    state_req: Arc<AtomicBool>,
    /// Decoded tracks, shared by the load threads.
    tracks: Arc<TrackCache>,
    /// Per `engine_state` waiting on pending loads: how many decks' fences
    /// have yet to pass.
    fences: HashMap<u64, usize>,
    next_fence: u64,
}

impl Control {
    fn reply(&self, id: Option<&Value>, res: Result<(), ProtoError>) {
        let mut o = self.out.lock().unwrap();
        if send(&mut *o, &protocol::result_json(id, &res)).is_err() {
            let _ = self.msg_tx.send(Msg::OutputClosed);
        }
    }

    fn seq(&mut self, id: Option<Value>) -> u64 {
        let seq = self.next_seq;
        self.next_seq += 1;
        self.ids.lock().unwrap().insert(seq, id);
        seq
    }

    /// Enqueue for the audio side; false (and an error reply) when the
    /// mailbox is full and the command was dropped.
    fn push_seq(&mut self, seq: u64, cmd: EngineCmd) -> bool {
        if self.cmd_tx.push((seq, cmd)).is_err() {
            let id = self.ids.lock().unwrap().remove(&seq).flatten();
            self.reply(id.as_ref(), Err(ProtoError::new(ErrorCode::Invalid, "engine mailbox is full; command dropped")));
            return false;
        }
        true
    }

    fn deck_of(cmd: &EngineCmd) -> Option<DeckId> {
        use EngineCmd::*;
        match cmd {
            Load { deck, .. } | Unload { deck } | Play { deck, .. } | Cue { deck } | Seek { deck, .. }
            | Quantize { deck, .. } | QuantizeGrid { deck, .. }
            | Loop { deck, .. } | BeatLoop { deck, .. } | BeatJump { deck, .. } | Tempo { deck, .. }
            | PitchRange { deck, .. } | Trim { deck, .. } | Eq { deck, .. } | Filter { deck, .. }
            | Fader { deck, .. } | Assign { deck, .. } => Some(*deck),
            Crossfader { .. } | MasterVolume { .. } | MasterMute { .. } => None,
        }
    }

    /// Park `item` behind the deck's pending load, if it has one. Returns
    /// the item back when the deck has no load pending, so it runs now.
    fn park(&mut self, deck: DeckId, item: Queued) -> Option<Queued> {
        let Some(q) = self.waiting[deck as usize - 1].as_mut() else { return Some(item) };
        if q.len() < QUEUE_SLOTS {
            q.push_back(item);
        } else {
            let (Queued::Cmd(id, _) | Queued::Load(id, _)) = item else {
                // A fence is only re-parked behind a load released from a
                // queue that held it, which leaves room; were it ever full,
                // asking now is later than asked, never earlier.
                if let Queued::Fence(f) = item {
                    self.fence_passed(f);
                }
                return None;
            };
            self.reply(
                id.as_ref(),
                Err(ProtoError::new(
                    ErrorCode::Invalid,
                    format!("engine mailbox is full ({QUEUE_SLOTS} commands wait on deck {deck}'s load); command dropped"),
                )),
            );
        }
        None
    }

    fn dispatch(&mut self, id: Option<Value>, cmd: EngineCmd) {
        let (id, cmd) = match Self::deck_of(&cmd) {
            Some(d) => match self.park(d, Queued::Cmd(id, cmd)) {
                Some(Queued::Cmd(id, cmd)) => (id, cmd),
                _ => return,
            },
            None => (id, cmd),
        };
        let seq = self.seq(id);
        let _ = self.push_seq(seq, cmd);
    }

    fn load(&mut self, id: Option<Value>, spec: LoadSpec) {
        let deck = spec.deck;
        let (id, spec) = match self.park(deck, Queued::Load(id, spec)) {
            Some(Queued::Load(id, spec)) => (id, spec),
            _ => return,
        };
        self.start_load(id, spec, VecDeque::new());
    }

    /// Decode `spec` off this thread, with `behind` parked behind it.
    fn start_load(&mut self, id: Option<Value>, spec: LoadSpec, behind: VecDeque<Queued>) {
        let deck = spec.deck;
        let seq = self.seq(id);
        self.waiting[deck as usize - 1] = Some(behind);
        self.loading[deck as usize - 1] = Some(seq);
        let tx = self.msg_tx.clone();
        let tracks = self.tracks.clone();
        std::thread::spawn(move || {
            // Decks on one file share its samples, and a load of a file
            // another deck is still decoding waits for that decode.
            let result = tracks.load(std::path::Path::new(&spec.path), &spec);
            let _ = tx.send(Msg::Decoded { seq, deck, result });
        });
    }

    fn finish_load(&mut self, seq: u64, deck: DeckId, result: Result<Arc<Track>, ProtoError>) {
        self.loading[deck as usize - 1] = None;
        let mut q = self.waiting[deck as usize - 1].take().unwrap_or_default();
        match result {
            Ok(track) => {
                q.push_front(Queued::Ready(seq, EngineCmd::Load { deck, track }));
                self.release(deck, q);
            }
            Err(e) => {
                let id = self.ids.lock().unwrap().remove(&seq).flatten();
                self.reply(id.as_ref(), Err(e));
                // The work queued behind it would run against the previous
                // track. Fail it instead: every command after the failed load
                // is refused.
                self.refuse_queued(q);
            }
        }
    }

    /// Send a deck's decoded load and the work behind it to the audio side,
    /// in order, as far as the mailbox has room. Everything accepted is
    /// kept: what does not fit stays parked (new work for the deck parks
    /// behind it) and goes once the audio side has drained room for it. A
    /// queued load re-arms the wait, with everything after it behind it.
    fn release(&mut self, deck: DeckId, mut q: VecDeque<Queued>) {
        while let Some(item) = q.pop_front() {
            match item {
                Queued::Ready(..) | Queued::Cmd(..) if self.cmd_tx.slots() == 0 => {
                    q.push_front(item);
                    break;
                }
                Queued::Ready(seq, cmd) => {
                    let _ = self.push_seq(seq, cmd);
                }
                Queued::Cmd(id, cmd) => {
                    let seq = self.seq(id);
                    let _ = self.push_seq(seq, cmd);
                }
                Queued::Load(id, spec) => {
                    self.start_load(id, spec, q);
                    return;
                }
                // Everything ahead of it on this deck is in the mailbox.
                Queued::Fence(f) => self.fence_passed(f),
            }
        }
        self.waiting[deck as usize - 1] = if q.is_empty() { None } else { Some(q) };
    }

    /// Whether any deck's decoded work waits for room in the mailbox.
    fn staged(&self) -> bool {
        (0..MAX_DECKS).any(|d| self.loading[d].is_none() && self.waiting[d].is_some())
    }

    /// Send on what waits for room, now that the audio side may have drained.
    fn release_staged(&mut self) {
        for d in 0..MAX_DECKS {
            if self.loading[d].is_none() {
                if let Some(q) = self.waiting[d].take() {
                    self.release(d as DeckId + 1, q);
                }
            }
        }
    }

    fn loads_pending(&self) -> bool {
        self.waiting.iter().any(Option::is_some)
    }

    /// Shutdown gave up on the loads still decoding: refuse each of them and
    /// everything queued behind them, so no command is left without a result.
    fn refuse_pending(&mut self) {
        for d in 0..MAX_DECKS {
            if let Some(seq) = self.loading[d].take() {
                let id = self.ids.lock().unwrap().remove(&seq).flatten();
                self.reply(
                    id.as_ref(),
                    Err(ProtoError::new(ErrorCode::Invalid, "the engine shut down before this load finished")),
                );
            }
            if let Some(q) = self.waiting[d].take() {
                self.refuse_queued(q);
            }
        }
    }

    /// One deck's work ahead of state request `f` has gone to the audio
    /// side (or failed); once every deck's has, the request is made.
    fn fence_passed(&mut self, f: u64) {
        let Some(left) = self.fences.get_mut(&f) else { return };
        *left -= 1;
        if *left == 0 {
            self.fences.remove(&f);
            self.state_req.store(true, Ordering::Release);
        }
    }

    fn refuse_queued(&mut self, q: VecDeque<Queued>) {
        for item in q {
            let id = match item {
                Queued::Cmd(id, _) | Queued::Load(id, _) => id,
                // Only shutdown refuses a decoded load: it never ran.
                Queued::Ready(seq, _) => {
                    let id = self.ids.lock().unwrap().remove(&seq).flatten();
                    self.reply(id.as_ref(), Err(ProtoError::new(ErrorCode::Invalid, "the engine stopped before this command ran")));
                    continue;
                }
                // The work ahead of it is done with, failed or not: the state
                // after that is the one asked for.
                Queued::Fence(f) => {
                    self.fence_passed(f);
                    continue;
                }
            };
            self.reply(
                id.as_ref(),
                Err(ProtoError::new(ErrorCode::Invalid, "the load this command waited on failed; command dropped")),
            );
        }
    }

    fn handle_line(&mut self, line: &str, clock: &str) -> bool {
        if line.trim().is_empty() {
            return true;
        }
        let (id, parsed) = protocol::parse_line(line);
        match parsed {
            Err(e) => self.reply(id.as_ref(), Err(e)),
            Ok(Command::Load(spec)) => self.load(id, spec),
            Ok(Command::Apply(c)) => self.dispatch(id, c),
            Ok(Command::NoOp) => self.reply(id.as_ref(), Ok(())),
            Ok(Command::Advance(_)) => self.reply(
                id.as_ref(),
                Err(ProtoError::new(
                    ErrorCode::WrongClock,
                    format!("engine_advance needs the fake clock; this engine runs on the {clock} clock"),
                )),
            ),
            Ok(Command::State) => {
                // Commands sent before this that wait on a deck's load are
                // not in the mailbox yet, so the request waits behind them,
                // on every such deck, and is made once they have gone ahead.
                let pending: Vec<usize> = (0..MAX_DECKS).filter(|&d| self.waiting[d].is_some()).collect();
                if pending.iter().any(|&d| self.waiting[d].as_ref().is_some_and(|q| q.len() >= QUEUE_SLOTS)) {
                    self.reply(
                        id.as_ref(),
                        Err(ProtoError::new(
                            ErrorCode::Invalid,
                            format!("engine mailbox is full ({QUEUE_SLOTS} commands wait on a deck's load); command dropped"),
                        )),
                    );
                    return true;
                }
                if pending.is_empty() {
                    // Release: the commands pushed before this are visible
                    // to the audio side once it sees the request.
                    self.state_req.store(true, Ordering::Release);
                } else {
                    let f = self.next_fence;
                    self.next_fence += 1;
                    self.fences.insert(f, pending.len());
                    for d in pending {
                        if let Some(q) = self.waiting[d].as_mut() {
                            q.push_back(Queued::Fence(f));
                        }
                    }
                }
                self.reply(id.as_ref(), Ok(()));
            }
            Ok(Command::Shutdown) => {
                self.reply(id.as_ref(), Ok(()));
                return false;
            }
        }
        true
    }
}

/// Serve on a threaded clock. `run_audio` owns the audio side for the life of
/// the process and must return once `stop` is set.
pub fn serve_threaded(
    sample_rate: u32,
    clock: &'static str,
    run_audio: impl FnOnce(AudioSide, Arc<AtomicBool>) + Send + 'static,
) -> io::Result<()> {
    serve_threaded_from(io::BufReader::new(io::stdin()), io::stdout(), sample_rate, clock, run_audio)
}

fn serve_threaded_from(
    input: impl BufRead + Send + 'static,
    output: impl Write + Send + 'static,
    sample_rate: u32,
    clock: &'static str,
    run_audio: impl FnOnce(AudioSide, Arc<AtomicBool>) + Send + 'static,
) -> io::Result<()> {
    let (cmd_tx, cmd_rx) = rtrb::RingBuffer::new(CMD_SLOTS);
    let (res_tx, mut res_rx) = rtrb::RingBuffer::<AudioResult>::new(1024);
    let (state_tx, mut state_rx) = rtrb::RingBuffer::new(64);
    let stop = Arc::new(AtomicBool::new(false));
    let state_req = Arc::new(AtomicBool::new(false));
    let out: Out = Arc::new(Mutex::new(Box::new(output)));
    let ids: Arc<Mutex<HashMap<u64, Option<Value>>>> = Arc::new(Mutex::new(HashMap::new()));

    {
        let mut o = out.lock().unwrap();
        send(&mut *o, &protocol::hello_json(clock, sample_rate))?;
    }

    // One clock for when state is heard and when it is sent, so the feed's
    // heard_in_ns needs no clock shared with the receiver.
    let epoch = Instant::now();
    let audio = AudioSide {
        engine: Engine::new(sample_rate),
        cmd_rx,
        res_tx,
        state_tx,
        state_req: state_req.clone(),
        frames_per_state: (sample_rate / STATE_HZ) as u64,
        frames_since_state: 0,
        epoch,
        ahead_ns: 0,
        scratch: vec![0.0; MAX_BLOCK * 2 * 8],
        #[cfg(test)]
        mid_block: None,
    };
    let (msg_tx, msg_rx) = mpsc::channel::<Msg>();
    let audio_stop = stop.clone();
    let exit_tx = msg_tx.clone();
    let audio_thread = std::thread::Builder::new().name("odj-audio".into()).spawn(move || {
        run_audio(audio, audio_stop);
        let _ = exit_tx.send(Msg::AudioExited);
    })?;

    // Pump: results and snapshots out to stdout; retired tracks freed here.
    // The pump stops only after the audio side has, so a result pushed by
    // the audio side's last block is still written out.
    let pump_stop = Arc::new(AtomicBool::new(false));
    let pump_stop_flag = pump_stop.clone();
    let pump_out = out.clone();
    let pump_ids = ids.clone();
    let pump_tx = msg_tx.clone();
    let pump = std::thread::spawn(move || {
        // Once a write fails the engine is stopping; the pump keeps draining
        // (retired tracks are still freed here, off the audio thread) but
        // writes nothing more.
        let mut closed = false;
        let mut write = |v: &Value| {
            if !closed && send(&mut *pump_out.lock().unwrap(), v).is_err() {
                closed = true;
                let _ = pump_tx.send(Msg::OutputClosed);
            }
        };
        loop {
            let mut idle = true;
            while let Ok((seq, r)) = res_rx.pop() {
                idle = false;
                let id = pump_ids.lock().unwrap().remove(&seq).flatten();
                let res = r.map(drop).map_err(ProtoError::from);
                write(&protocol::result_json(id.as_ref(), &res));
            }
            while let Ok((snap, heard_ns)) = state_rx.pop() {
                idle = false;
                let host = HostTime { heard_ns, sent_ns: epoch.elapsed().as_nanos() as u64 };
                write(&protocol::state_json(&snap, Some(host)));
            }
            if pump_stop_flag.load(Ordering::Relaxed) && idle {
                break;
            }
            if idle {
                std::thread::sleep(Duration::from_millis(2));
            }
        }
    });

    let reader_tx = msg_tx.clone();
    let in_flight = Arc::new(InFlight::default());
    let reader_room = in_flight.clone();
    let mut input = input;
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        loop {
            let msg = match read_line(&mut input, &mut buf) {
                Ok(ReadLine::Line(l)) => {
                    reader_room.take(l.len());
                    Msg::Line(l)
                }
                // Its refusal is a reply the control side still has to
                // write, so it takes a slot like any line.
                Ok(ReadLine::TooLong) => {
                    reader_room.take(0);
                    Msg::TooLong
                }
                Ok(ReadLine::Eof) | Err(_) => break,
            };
            if reader_tx.send(msg).is_err() {
                return;
            }
        }
        let _ = reader_tx.send(Msg::Eof);
    });

    let mut control = Control {
        cmd_tx,
        ids,
        out,
        msg_tx,
        next_seq: 0,
        waiting: Default::default(),
        loading: [None; MAX_DECKS],
        state_req,
        tracks: Arc::default(),
        fences: HashMap::new(),
        next_fence: 0,
    };
    let mut audio_failed = false;
    let mut output_closed = false;
    loop {
        // Work that waits for room in the mailbox goes as the audio side
        // drains, so while any does the loop wakes to send it on.
        control.release_staged();
        let msg = if control.staged() {
            match msg_rx.recv_timeout(Duration::from_millis(1)) {
                Ok(msg) => msg,
                Err(mpsc::RecvTimeoutError::Timeout) => continue,
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        } else {
            match msg_rx.recv() {
                Ok(msg) => msg,
                Err(_) => break,
            }
        };
        match msg {
            Msg::Line(l) => {
                let go_on = control.handle_line(&l, clock);
                in_flight.give_back(l.len());
                if !go_on {
                    break;
                }
            }
            Msg::TooLong => {
                control.reply(None, Err(too_long()));
                in_flight.give_back(0);
            }
            Msg::Decoded { seq, deck, result } => control.finish_load(seq, deck, result),
            // Supervisor gone: the engine has no reason to outlive it.
            Msg::Eof => break,
            Msg::AudioExited => {
                audio_failed = true;
                break;
            }
            // Nobody hears results any more: stop rather than keep playing
            // and applying commands whose outcome is thrown away.
            Msg::OutputClosed => {
                output_closed = true;
                break;
            }
        }
    }
    // Loads still decoding hold their own result and the work parked behind
    // them. Finish them (which releases that work to the mailbox), bounded;
    // whatever is still pending after that is refused. A dead audio side
    // would never apply any of it, so it is refused at once, and with the
    // output closed nothing is waited for.
    if !audio_failed && !output_closed {
        let deadline = Instant::now() + LOAD_DRAIN_LIMIT;
        while control.loads_pending() && Instant::now() < deadline {
            control.release_staged();
            if !control.loads_pending() {
                break;
            }
            let wait = deadline.saturating_duration_since(Instant::now());
            match msg_rx.recv_timeout(if control.staged() { wait.min(Duration::from_millis(1)) } else { wait }) {
                Ok(Msg::Decoded { seq, deck, result }) => control.finish_load(seq, deck, result),
                Ok(Msg::AudioExited) => {
                    audio_failed = true;
                    break;
                }
                Ok(Msg::OutputClosed) => {
                    output_closed = true;
                    break;
                }
                // Nothing sent after shutdown or EOF is taken.
                Ok(Msg::Line(_) | Msg::TooLong | Msg::Eof) => {}
                Err(mpsc::RecvTimeoutError::Timeout) => {}
                Err(mpsc::RecvTimeoutError::Disconnected) => break,
            }
        }
    }
    control.refuse_pending();
    // Let the audio side apply everything already queued before stopping,
    // so every command sent before shutdown or EOF gets its result, and
    // answer an `engine_state` already acknowledged: its request stays set
    // until a block has taken it and will publish it. A dead audio side
    // never drains, so it is not waited on, and neither is one whose
    // results nobody reads.
    if !audio_failed && !output_closed {
        let deadline = Instant::now() + DRAIN_LIMIT;
        let busy = |c: &Control| c.cmd_tx.slots() < CMD_SLOTS || c.state_req.load(Ordering::Acquire);
        while busy(&control) && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    stop.store(true, Ordering::Relaxed);
    let _ = audio_thread.join();
    pump_stop.store(true, Ordering::Relaxed);
    let _ = pump.join();
    // Anything still without a result never ran: its audio side died with
    // it in the mailbox, or the drain gave up. Refuse each one by its id.
    let unanswered: Vec<Option<Value>> = control.ids.lock().unwrap().drain().map(|(_, id)| id).collect();
    for id in unanswered {
        control.reply(
            id.as_ref(),
            Err(ProtoError::new(ErrorCode::Invalid, "the engine stopped before this command ran")),
        );
    }
    if audio_failed {
        return Err(io::Error::other("the audio side stopped before shutdown (see stderr)"));
    }
    if output_closed {
        return Err(io::Error::new(io::ErrorKind::BrokenPipe, "the output closed, so the engine stopped"));
    }
    Ok(())
}

/// The wall clock: render into a null sink, paced by `Instant`.
pub fn run_wall(block_frames: usize) -> impl FnOnce(AudioSide, Arc<AtomicBool>) + Send + 'static {
    move |mut side: AudioSide, stop: Arc<AtomicBool>| {
        let sr = side.engine.sample_rate() as f64;
        let start = Instant::now();
        let mut frames: u64 = 0;
        while !stop.load(Ordering::Relaxed) {
            side.process(block_frames);
            frames += block_frames as u64;
            let due = start + Duration::from_secs_f64(frames as f64 / sr);
            let now = Instant::now();
            if due > now {
                std::thread::sleep(due - now);
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn side(res_slots: usize) -> (AudioSide, rtrb::Producer<(u64, EngineCmd)>, rtrb::Consumer<AudioResult>) {
        let (side, cmd_tx, res_rx, _state_rx) = side_at(48000, u64::MAX, res_slots);
        (side, cmd_tx, res_rx)
    }

    type SideParts =
        (AudioSide, rtrb::Producer<(u64, EngineCmd)>, rtrb::Consumer<AudioResult>, rtrb::Consumer<(Snapshot, u64)>);

    fn side_at(sample_rate: u32, frames_per_state: u64, res_slots: usize) -> SideParts {
        let (cmd_tx, cmd_rx) = rtrb::RingBuffer::new(16);
        let (res_tx, res_rx) = rtrb::RingBuffer::new(res_slots);
        let (state_tx, state_rx) = rtrb::RingBuffer::new(64);
        let side = AudioSide {
            engine: Engine::new(sample_rate),
            cmd_rx,
            res_tx,
            state_tx,
            state_req: Arc::new(AtomicBool::new(false)),
            frames_per_state,
            frames_since_state: 0,
            epoch: Instant::now(),
            ahead_ns: 0,
            scratch: vec![0.0; MAX_BLOCK * 2 * 8],
            mid_block: None,
        };
        (side, cmd_tx, res_rx, state_rx)
    }

    fn drain(rx: &mut rtrb::Consumer<(Snapshot, u64)>) -> Vec<(u64, u64)> {
        let mut v = Vec::new();
        while let Ok((s, ns)) = rx.pop() {
            v.push((s.frame, ns));
        }
        v
    }

    #[test]
    fn state_keeps_its_cadence_when_blocks_are_longer_than_the_interval() {
        // `serve --clock wall --sample-rate 8000 --block 1024`: 128 ms blocks
        // against a 266-frame (about 33 ms) state interval.
        let per = (8000 / STATE_HZ) as u64;
        let (mut side, _cmd, _res, mut states) = side_at(8000, per, 4);
        // Within one block, each state is heard one interval after the last.
        side.process(1024);
        let first = drain(&mut states);
        assert_eq!(first.iter().map(|s| s.0).collect::<Vec<_>>(), vec![per, 2 * per, 3 * per]);
        let step = per * 1_000_000_000 / 8000;
        assert!(first.windows(2).all(|w| w[1].1 - w[0].1 == step), "{first:?}");
        for _ in 1..10 {
            side.process(1024);
        }
        let got: Vec<_> = first.into_iter().chain(drain(&mut states)).collect();
        assert_eq!(got.len() as u64, 10 * 1024 / per, "{got:?}");
        // Each at its own boundary frame.
        for (k, &(frame, _)) in got.iter().enumerate() {
            assert_eq!(frame, (k as u64 + 1) * per);
        }
        // Control: blocks shorter than the interval still publish once per
        // interval, not once per block.
        let (mut side, _cmd, _res, mut states) = side_at(8000, per, 4);
        for _ in 0..40 {
            side.process(64);
        }
        let got = drain(&mut states);
        assert_eq!(got.len() as u64, 40 * 64 / per, "{got:?}");
        assert!(got.iter().all(|&(f, _)| f % per == 0), "{got:?}");
    }

    #[test]
    fn a_requested_state_goes_out_once_at_the_end_of_the_block() {
        let (mut side, _cmd, _res, mut states) = side_at(8000, u64::MAX, 4);
        side.state_req.store(true, Ordering::Relaxed);
        side.process(100);
        assert_eq!(drain(&mut states).iter().map(|s| s.0).collect::<Vec<_>>(), vec![100]);
        side.process(100);
        assert!(drain(&mut states).is_empty(), "the request was consumed");
    }

    #[test]
    fn a_requested_state_waits_for_room_in_a_full_state_ring() {
        // Codex on 00209c94: with stdout slow and the state ring full, the
        // requested state was dropped and the request cleared, so an
        // `engine_state` answered ok never got its state.
        let (mut side, _cmd, _res, mut states) = side_at(8000, u64::MAX, 4);
        while side.state_tx.push((side.engine.snapshot(), 0)).is_ok() {}
        side.state_req.store(true, Ordering::Relaxed);
        side.process(100);
        assert!(side.state_req.load(Ordering::Relaxed), "the request was cleared with its state dropped");
        assert_eq!(drain(&mut states).len(), 64);
        // The ring drained, the next block sends it, and then it is spent.
        side.process(100);
        assert_eq!(drain(&mut states).iter().map(|s| s.0).collect::<Vec<_>>(), vec![200]);
        assert!(!side.state_req.load(Ordering::Relaxed));
        side.process(100);
        assert!(drain(&mut states).is_empty(), "the request outlived its state");
        // Likewise when the block's own periodic state is the one dropped.
        let per = 100;
        let (mut side, _cmd, _res, mut states) = side_at(8000, per, 4);
        while side.state_tx.push((side.engine.snapshot(), 0)).is_ok() {}
        side.state_req.store(true, Ordering::Relaxed);
        side.process(100);
        assert!(side.state_req.load(Ordering::Relaxed), "a dropped periodic state satisfied the request");
        drain(&mut states);
        side.process(50);
        assert_eq!(drain(&mut states).iter().map(|s| s.0).collect::<Vec<_>>(), vec![150]);
    }

    #[test]
    fn a_state_request_arriving_mid_block_waits_for_the_next_block() {
        // Codex on 7124cb2e: a request set after the block's periodic state
        // was cleared by that state, which predates it and the commands
        // before it.
        let (mut side, _cmd, _res, mut states) = side_at(8000, 100, 4);
        side.mid_block = Some(|r| r.store(true, Ordering::Release));
        side.process(100);
        side.mid_block = None;
        assert_eq!(drain(&mut states).iter().map(|s| s.0).collect::<Vec<_>>(), vec![100], "the periodic state");
        assert!(side.state_req.load(Ordering::Relaxed), "a state from before the request answered it");
        side.process(50);
        assert_eq!(drain(&mut states).iter().map(|s| s.0).collect::<Vec<_>>(), vec![150]);
        // Control: a request there before the block is answered by the
        // block's periodic state, not by a second one.
        side.state_req.store(true, Ordering::Release);
        side.process(100);
        assert_eq!(drain(&mut states).iter().map(|s| s.0).collect::<Vec<_>>(), vec![250]);
        assert!(!side.state_req.load(Ordering::Relaxed));
    }

    #[test]
    fn a_state_request_waits_for_the_commands_sent_before_it() {
        // A command held back by a full result ring may have been sent
        // before the request, so the state waits for the block that applies it.
        let (mut side, mut cmd_tx, mut res_rx, mut states) = side_at(8000, u64::MAX, 1);
        for muted in [false, true] {
            cmd_tx.push((muted as u64, EngineCmd::MasterMute { muted })).unwrap();
        }
        side.state_req.store(true, Ordering::Release);
        side.process(64);
        assert!(drain(&mut states).is_empty(), "a state went out with a command before it still waiting");
        assert!(res_rx.pop().unwrap().1.is_ok());
        side.process(64);
        let got: Vec<_> = std::iter::from_fn(|| states.pop().ok()).collect();
        assert_eq!(got.len(), 1);
        assert!(got[0].0.master_muted, "the state is from before the second command");
        assert_eq!(got[0].0.frame, 128);
    }

    /// A control side with its own mailbox, reading nothing from the load
    /// threads it starts, so a test settles each load itself.
    fn control() -> (Control, rtrb::Consumer<(u64, EngineCmd)>, Captured) {
        let (cmd_tx, cmd_rx) = rtrb::RingBuffer::new(CMD_SLOTS);
        let out = Captured::default();
        let (msg_tx, msg_rx) = mpsc::channel();
        std::mem::forget(msg_rx);
        let control = Control {
            cmd_tx,
            ids: Arc::default(),
            out: Arc::new(Mutex::new(Box::new(out.clone()))),
            msg_tx,
            next_seq: 0,
            waiting: Default::default(),
            loading: [None; MAX_DECKS],
            state_req: Arc::new(AtomicBool::new(false)),
            tracks: Arc::default(),
            fences: HashMap::new(),
            next_fence: 0,
        };
        (control, cmd_rx, out)
    }

    fn silent_track() -> Result<Arc<Track>, ProtoError> {
        Ok(Arc::new(Track::new(48000, vec![0.0; 9600], vec![], None)))
    }

    #[test]
    fn a_state_request_waits_behind_work_parked_on_a_pending_load() {
        // Codex on 0f81c719: `load` (slow), `play` on that deck, then
        // `engine_state`: the play was parked behind the load, not in the
        // mailbox, so the state went out before either had happened.
        let (mut c, mut cmd_rx, _out) = control();
        let line = |c: &mut Control, v: Value| assert!(c.handle_line(&v.to_string(), "wall"));
        line(&mut c, serde_json::json!({"cmd": {"type": "load", "deck": 1, "path": "nope-1.wav"}}));
        line(&mut c, serde_json::json!({"cmd": {"type": "play", "deck": 1, "playing": true}}));
        line(&mut c, serde_json::json!({"cmd": {"type": "engine_state"}}));
        assert!(!c.state_req.load(Ordering::Acquire), "the state was asked for ahead of the parked play");
        c.finish_load(c.loading[0].unwrap(), 1, silent_track());
        assert!(c.state_req.load(Ordering::Acquire), "the state was never asked for");
        let sent: Vec<_> = std::iter::from_fn(|| cmd_rx.pop().ok()).map(|(_, cmd)| cmd).collect();
        assert!(matches!(sent[..], [EngineCmd::Load { .. }, EngineCmd::Play { .. }]), "{sent:?}");
        // A load that fails lets it through (the state after that is the
        // one asked for), and a second load queued behind the first holds
        // it again.
        c.state_req.store(false, Ordering::Relaxed);
        line(&mut c, serde_json::json!({"cmd": {"type": "load", "deck": 2, "path": "nope-2.wav"}}));
        line(&mut c, serde_json::json!({"cmd": {"type": "load", "deck": 2, "path": "nope-3.wav"}}));
        line(&mut c, serde_json::json!({"cmd": {"type": "load", "deck": 3, "path": "nope-4.wav"}}));
        line(&mut c, serde_json::json!({"cmd": {"type": "engine_state"}}));
        c.finish_load(c.loading[2].unwrap(), 3, Err(ProtoError::new(ErrorCode::Decode, "bad")));
        assert!(!c.state_req.load(Ordering::Acquire), "asked for with deck 2's loads still ahead of it");
        c.finish_load(c.loading[1].unwrap(), 2, silent_track());
        assert!(!c.state_req.load(Ordering::Acquire), "asked for with deck 2's second load still ahead of it");
        c.finish_load(c.loading[1].unwrap(), 2, silent_track());
        assert!(c.state_req.load(Ordering::Acquire), "the state was never asked for");
        // Control: with nothing pending it is asked for at once.
        c.state_req.store(false, Ordering::Relaxed);
        line(&mut c, serde_json::json!({"cmd": {"type": "engine_state"}}));
        assert!(c.state_req.load(Ordering::Acquire));
        assert!(c.fences.is_empty());
    }

    #[test]
    fn everything_parked_behind_a_load_fits_the_mailbox_with_the_load() {
        // Codex on b68a0857: the queue took as many commands as the mailbox
        // holds, so with the audio side not draining, the load itself took
        // a slot and the last parked command was refused on release.
        let (mut c, mut cmd_rx, out) = control();
        let line = |c: &mut Control, v: Value| assert!(c.handle_line(&v.to_string(), "wall"));
        line(&mut c, serde_json::json!({"cmd": {"type": "load", "deck": 1, "path": "nope.wav"}}));
        for i in 0..=QUEUE_SLOTS {
            line(&mut c, serde_json::json!({"id": i, "cmd": {"type": "fader", "deck": 1, "value": 0.5}}));
        }
        c.finish_load(c.loading[0].unwrap(), 1, silent_track());
        let sent = std::iter::from_fn(|| cmd_rx.pop().ok()).count();
        // The load and every parked command fill the mailbox, to the slot:
        // one fewer would refuse a command the mailbox had room for.
        assert_eq!(sent, CMD_SLOTS, "the load and every parked command reach the mailbox");
        let text = String::from_utf8(out.0.lock().unwrap().clone()).unwrap();
        let full: Vec<Value> = text
            .lines()
            .map(|l| serde_json::from_str::<Value>(l).unwrap())
            .filter(|v| v["error"]["message"].as_str().is_some_and(|m| m.contains("mailbox is full")))
            .collect();
        // Control: the one past the bound is still refused, at once.
        assert_eq!(full.len(), 1, "{full:?}");
        assert_eq!(full[0]["id"], QUEUE_SLOTS);
    }

    #[test]
    fn work_accepted_behind_a_load_waits_for_room_the_mailbox_does_not_have() {
        // Codex on 2c2fe0ab: other decks' traffic already in the mailbox
        // when a load finished left no room for everything accepted behind
        // it, and the last of it was refused then, after being accepted.
        // Now what does not fit waits, in order, and goes as the audio side
        // drains. Here the mailbox is full of crossfader moves, so even the
        // load itself waits, with 500 commands behind it.
        let (mut c, mut cmd_rx, out) = control();
        let line = |c: &mut Control, v: Value| assert!(c.handle_line(&v.to_string(), "wall"));
        line(&mut c, serde_json::json!({"cmd": {"type": "load", "deck": 1, "path": "nope.wav"}}));
        let parked = 500;
        for i in 0..parked {
            line(&mut c, serde_json::json!({"id": i, "cmd": {"type": "fader", "deck": 1, "value": 0.5}}));
        }
        for _ in 0..CMD_SLOTS {
            line(&mut c, serde_json::json!({"cmd": {"type": "crossfader", "value": 0.5}}));
        }
        c.finish_load(c.loading[0].unwrap(), 1, silent_track());
        assert!(c.staged(), "the decoded load did not wait for room");
        // Sent while the deck's work waits: it goes after that work, and an
        // `engine_state` waits for all of it.
        line(&mut c, serde_json::json!({"id": "late", "cmd": {"type": "fader", "deck": 1, "value": 0.25}}));
        line(&mut c, serde_json::json!({"id": "state", "cmd": {"type": "engine_state"}}));
        let mut order = Vec::new();
        let mut rounds = 0;
        while c.staged() {
            // The audio side drains a block's worth, then the control side
            // sends on what now fits.
            for _ in 0..100 {
                if let Ok((seq, cmd)) = cmd_rx.pop() {
                    order.push((seq, matches!(cmd, EngineCmd::Load { .. }), matches!(cmd, EngineCmd::Fader { .. })));
                }
            }
            assert!(!c.state_req.load(Ordering::Acquire) || !c.staged(), "the state was asked before the work ahead of it went");
            c.release_staged();
            rounds += 1;
            assert!(rounds < 100, "the staged work never drained");
        }
        order.extend(std::iter::from_fn(|| cmd_rx.pop().ok()).map(|(seq, cmd)| (seq, matches!(cmd, EngineCmd::Load { .. }), matches!(cmd, EngineCmd::Fader { .. }))));
        assert!(c.state_req.load(Ordering::Acquire), "the state was never asked");
        let text = String::from_utf8(out.0.lock().unwrap().clone()).unwrap();
        assert!(!text.contains("mailbox is full"), "accepted work was refused:\n{text}");
        // The load, then every parked fader and the late one, in order.
        let deck: Vec<&(u64, bool, bool)> = order.iter().filter(|(_, load, fader)| *load || *fader).collect();
        assert_eq!(deck.len(), 1 + parked + 1);
        assert!(deck[0].1, "the load did not go first");
        assert!(deck.windows(2).all(|w| w[0].0 < w[1].0), "the deck's work went out of order");
        let ids = c.ids.lock().unwrap();
        assert!(ids.values().any(|id| id.as_ref() == Some(&Value::from("late"))), "the late command never reached the mailbox");
    }

    #[test]
    fn a_threaded_engine_sends_waiting_work_on_as_the_mailbox_drains() {
        // The same case end to end: the control loop wakes by itself to send
        // on work that waits for room, with no further input to wake it; and
        // when stdin closes first, the shutdown drain sends it on too rather
        // than refuse work it accepted.
        let d = std::env::temp_dir().join(format!("odj-staged-{}", std::process::id()));
        std::fs::create_dir_all(&d).unwrap();
        let wav = d.join("a.wav");
        crate::wav::write_f32(&mut io::BufWriter::new(std::fs::File::create(&wav).unwrap()), 48000, &[0.0; 9600]).unwrap();
        for close_first in [false, true] {
            let (reader, mut writer) = io::pipe().unwrap();
            let out = Captured::default();
            let go = Arc::new(AtomicBool::new(false));
            let (tx, rx) = mpsc::channel();
            let (o, g) = (out.clone(), go.clone());
            std::thread::spawn(move || {
                let r = serve_threaded_from(io::BufReader::new(reader), o, 48000, "wall", move |mut side, stop| {
                    while !stop.load(Ordering::Relaxed) {
                        if g.load(Ordering::Relaxed) {
                            side.process(64);
                        }
                        std::thread::sleep(Duration::from_millis(1));
                    }
                });
                let _ = tx.send(r);
            });
            // The mailbox fills while the audio side is held, then the load
            // decodes into no room, with work parked behind it.
            for _ in 0..CMD_SLOTS {
                writeln!(writer, r#"{{"cmd": {{"type": "crossfader", "value": 0.5}}}}"#).unwrap();
            }
            writeln!(writer, r#"{{"id": "load", "cmd": {{"type": "load", "deck": 1, "path": {}}}}}"#, Value::from(wav.display().to_string())).unwrap();
            for i in 0..200 {
                writeln!(writer, r#"{{"id": {i}, "cmd": {{"type": "fader", "deck": 1, "value": 0.5}}}}"#).unwrap();
            }
            std::thread::sleep(Duration::from_millis(300));
            let writer = if close_first {
                drop(writer);
                std::thread::sleep(Duration::from_millis(100));
                None
            } else {
                Some(writer)
            };
            let started = Instant::now();
            go.store(true, Ordering::Relaxed);
            let results = || -> Vec<Value> {
                let text = String::from_utf8(out.0.lock().unwrap().clone()).unwrap();
                text.lines().map(|l| serde_json::from_str::<Value>(l).unwrap()).filter(|v| v["type"] == "result").collect()
            };
            let deadline = Instant::now() + Duration::from_secs(10);
            while !results().iter().any(|v| v["id"] == 199) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(5));
            }
            let took = started.elapsed();
            let got = results();
            drop(writer);
            let stopping = Instant::now();
            rx.recv_timeout(Duration::from_secs(10)).expect("serve hung").unwrap();
            let stopped_in = stopping.elapsed();
            let case = if close_first { "stdin closed first" } else { "stdin open" };
            assert!(got.iter().any(|v| v["id"] == 199), "{case}: the parked work never went");
            assert!(got.iter().filter(|v| v["id"] == "load" || v["id"].is_u64()).all(|v| v["ok"] == true), "{case}: {got:?}");
            assert!(took < LOAD_DRAIN_LIMIT / 2, "{case}: the parked work took {took:?}");
            // With nothing left waiting, stopping does not sit out the limit.
            assert!(stopped_in < LOAD_DRAIN_LIMIT / 2, "{case}: stopping took {stopped_in:?}");
        }
        let _ = std::fs::remove_dir_all(&d);
    }

    #[test]
    fn a_full_result_ring_holds_commands_back_instead_of_dropping_results() {
        let (mut side, mut cmd_tx, mut res_rx) = side(2);
        for seq in 0..5 {
            cmd_tx.push((seq, EngineCmd::MasterMute { muted: seq % 2 == 0 })).unwrap();
        }
        side.process(64);
        // Two slots, two results; the other three stay queued, not lost.
        assert_eq!(res_rx.slots(), 2);
        assert_eq!(cmd_tx.slots(), 16 - 3);
        let mut seen = Vec::new();
        for _ in 0..3 {
            while let Ok((seq, r)) = res_rx.pop() {
                assert!(r.is_ok());
                seen.push(seq);
            }
            side.process(64);
        }
        assert_eq!(seen, vec![0, 1, 2, 3, 4]);
        assert_eq!(cmd_tx.slots(), 16);
    }

    #[test]
    fn an_audio_side_that_fails_to_start_ends_serve_with_an_error() {
        // As the device clock does when the output stream will not open: the
        // audio side returns before shutdown. serve must end, not wait forever.
        // stdin held open with nothing on it, as a live supervisor's is.
        let (reader, _writer) = io::pipe().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = tx.send(serve_threaded_from(io::BufReader::new(reader), io::sink(), 48000, "wall", |_side, _stop| {}));
        });
        let r = rx.recv_timeout(Duration::from_secs(10)).expect("serve hung after its audio side stopped");
        assert!(r.is_err());
    }

    /// A shared buffer that tests read serve's output from.
    #[derive(Clone, Default)]
    struct Captured(Arc<Mutex<Vec<u8>>>);

    impl Write for Captured {
        fn write(&mut self, b: &[u8]) -> io::Result<usize> {
            self.0.lock().unwrap().extend_from_slice(b);
            Ok(b.len())
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn commands_left_in_the_mailbox_when_the_audio_side_dies_are_refused() {
        // The command is taken into the mailbox, then the audio side dies
        // without ever draining it. It still gets a result.
        let (reader, mut writer) = io::pipe().unwrap();
        let out = Captured::default();
        let (tx, rx) = mpsc::channel();
        let o = out.clone();
        std::thread::spawn(move || {
            let r = serve_threaded_from(io::BufReader::new(reader), o, 48000, "wall", |_side, _stop| {
                std::thread::sleep(Duration::from_millis(300));
            });
            let _ = tx.send(r);
        });
        writeln!(writer, r#"{{"id": "stuck", "cmd": {{"type": "crossfader", "value": 0.5}}}}"#).unwrap();
        let r = rx.recv_timeout(Duration::from_secs(10)).expect("serve hung after its audio side stopped");
        assert!(r.is_err());
        let text = String::from_utf8(out.0.lock().unwrap().clone()).unwrap();
        let result = text
            .lines()
            .map(|l| serde_json::from_str::<Value>(l).unwrap())
            .find(|v| v["type"] == "result" && v["id"] == "stuck")
            .unwrap_or_else(|| panic!("no result for the stranded command:\n{text}"));
        assert_eq!(result["ok"], false, "{result}");
        assert!(result["error"]["message"].as_str().unwrap().contains("stopped before"), "{result}");
    }

    #[test]
    fn a_state_request_acknowledged_before_shutdown_is_answered() {
        // Codex on 6a4cfb88: `engine_state` then EOF, with the audio side
        // between blocks and nothing in the mailbox, stopped the audio side
        // before any block took the request, so the acknowledged state never
        // came. The audio side here only renders every 50 ms and checks
        // for stop before each block, as the wall clock's loop does.
        let run = |input: &'static str| {
            let out = Captured::default();
            let (tx, rx) = mpsc::channel();
            let o = out.clone();
            let started = Instant::now();
            std::thread::spawn(move || {
                let r = serve_threaded_from(io::Cursor::new(input), o, 48000, "wall", |mut side, stop| loop {
                    std::thread::sleep(Duration::from_millis(50));
                    if stop.load(Ordering::Relaxed) {
                        break;
                    }
                    side.process(64);
                });
                let _ = tx.send(r);
            });
            rx.recv_timeout(Duration::from_secs(10)).expect("serve hung").unwrap();
            let text = String::from_utf8(out.0.lock().unwrap().clone()).unwrap();
            let lines: Vec<Value> = text.lines().map(|l| serde_json::from_str(l).unwrap()).collect();
            (lines, started.elapsed())
        };
        let (lines, _) = run("{\"id\": \"s\", \"cmd\": {\"type\": \"engine_state\"}}\n");
        assert!(lines.iter().any(|v| v["id"] == "s" && v["ok"] == true), "{lines:?}");
        assert!(lines.iter().any(|v| v["type"] == "state"), "the acknowledged state never came: {lines:?}");
        // Control: with no request pending, EOF stops at once rather than
        // waiting out the drain limit.
        let (lines, took) = run("");
        assert!(!lines.iter().any(|v| v["type"] == "state"), "{lines:?}");
        assert!(took < DRAIN_LIMIT / 2, "stopping took {took:?} with nothing to wait for");
    }

    /// Output that works until `fail` is set, then refuses every write, as
    /// stdout does once its reader has gone.
    #[derive(Clone, Default)]
    struct Closable {
        text: Captured,
        fail: Arc<AtomicBool>,
    }

    impl Write for Closable {
        fn write(&mut self, b: &[u8]) -> io::Result<usize> {
            if self.fail.load(Ordering::Relaxed) {
                return Err(io::ErrorKind::BrokenPipe.into());
            }
            self.text.write(b)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn a_closed_output_stops_the_engine() {
        // Codex on 00209c94: with stdout's reader gone and stdin still open,
        // the engine kept applying commands and dropping every result.
        // Either writer may meet the closed output first: the pump (a result
        // or a state from a running audio side) or the control side (a reply
        // it writes itself, here to a malformed line).
        for (line, audio_runs) in [(r#"{"id": 1, "cmd": {"type": "crossfader", "value": 0.5}}"#, true), ("not json", false)] {
            let (reader, mut writer) = io::pipe().unwrap();
            let out = Closable::default();
            let (tx, rx) = mpsc::channel();
            let (o, audio_stopped) = (out.clone(), Arc::new(AtomicBool::new(false)));
            let stopped = audio_stopped.clone();
            std::thread::spawn(move || {
                let r = serve_threaded_from(io::BufReader::new(reader), o, 48000, "wall", move |mut side, stop| {
                    while !stop.load(Ordering::Relaxed) {
                        if audio_runs {
                            side.process(64);
                        }
                        std::thread::sleep(Duration::from_millis(1));
                    }
                    stopped.store(true, Ordering::Relaxed);
                });
                let _ = tx.send(r);
            });
            // Control: a working output keeps a session with stdin open going.
            writeln!(writer, "{line}").unwrap();
            assert!(rx.recv_timeout(Duration::from_millis(300)).is_err(), "{line}: serve ended with its output working");
            assert!(String::from_utf8(out.text.0.lock().unwrap().clone()).unwrap().contains(r#""type":"result""#));
            out.fail.store(true, Ordering::Relaxed);
            writeln!(writer, "{line}").unwrap();
            let r = rx.recv_timeout(Duration::from_secs(10)).unwrap_or_else(|_| panic!("{line}: serve kept running with its output closed"));
            assert_eq!(r.unwrap_err().kind(), io::ErrorKind::BrokenPipe, "{line}");
            assert!(audio_stopped.load(Ordering::Relaxed), "{line}: the audio side was left running");
            drop(writer);
        }
    }

    /// Endless lines, line `i` being `len_of(i)` bytes (newline included),
    /// whole lines only, counting each as it is started; ends at a line
    /// boundary once `end` is set, and never starts more than `cap` lines.
    struct Lines {
        len_of: fn(usize) -> usize,
        cur: usize,
        at: usize,
        made: Arc<std::sync::atomic::AtomicUsize>,
        end: Arc<AtomicBool>,
        cap: usize,
    }

    impl io::Read for Lines {
        fn read(&mut self, b: &mut [u8]) -> io::Result<usize> {
            use std::sync::atomic::Ordering::SeqCst;
            if self.at == 0 {
                while !self.end.load(SeqCst) && self.made.load(SeqCst) >= self.cap {
                    std::thread::sleep(Duration::from_millis(5));
                }
                if self.end.load(SeqCst) {
                    return Ok(0);
                }
                self.cur = (self.len_of)(self.made.fetch_add(1, SeqCst));
            }
            let n = b.len().min(self.cur - self.at);
            b[..n].fill(b'x');
            if self.at + n == self.cur {
                b[n - 1] = b'\n';
                self.at = 0;
            } else {
                self.at += n;
            }
            Ok(n)
        }
    }

    /// Output whose writes stall after its first line (the hello) until
    /// `open` is set, as a pipe nobody reads does.
    #[derive(Clone, Default)]
    struct Stalled {
        text: Captured,
        open: Arc<AtomicBool>,
    }

    impl Write for Stalled {
        fn write(&mut self, b: &[u8]) -> io::Result<usize> {
            while self.text.0.lock().unwrap().contains(&b'\n') && !self.open.load(Ordering::SeqCst) {
                std::thread::sleep(Duration::from_millis(5));
            }
            self.text.write(b)
        }
        fn flush(&mut self) -> io::Result<()> {
            Ok(())
        }
    }

    #[test]
    fn a_stalled_session_stops_reading_stdin() {
        // Codex on 7124cb2e: the reader handed the control side every line
        // it could read, without limit, while the control side was stuck
        // writing to a stdout nobody read. By lines, and by bytes.
        // And (Codex on b68a0857) by the refusals of lines past the limit,
        // which the control side writes too: once short lines fill every
        // slot, an oversized line waits for one as well.
        // (name, length of line i, lines on offer, most lines read)
        type Case = (&'static str, fn(usize) -> usize, usize, usize);
        let cases: [Case; 3] = [
            ("9-byte", |_| 9, 200_000, LINE_SLOTS + 1 + 8192 / 9 + 1),
            ("1 MiB", |_| 1 << 20, 100, LINE_BYTES / (1 << 20) + 2),
            // A length counts the newline: one byte past the limit is + 2.
            ("oversized", |i| if i < LINE_SLOTS { 9 } else { MAX_LINE_BYTES + 2 }, LINE_SLOTS + 20, LINE_SLOTS + 2),
        ];
        for (len, len_of, cap, bound) in cases {
            let made = Arc::new(std::sync::atomic::AtomicUsize::new(0));
            let end = Arc::new(AtomicBool::new(false));
            let input = io::BufReader::with_capacity(8192, Lines { len_of, cur: 0, at: 0, made: made.clone(), end: end.clone(), cap });
            let out = Stalled::default();
            let (tx, rx) = mpsc::channel();
            let o = out.clone();
            std::thread::spawn(move || {
                let r = serve_threaded_from(input, o, 48000, "wall", |_side, stop| {
                    while !stop.load(Ordering::Relaxed) {
                        std::thread::sleep(Duration::from_millis(1));
                    }
                });
                let _ = tx.send(r);
            });
            let settled = || {
                let mut last = usize::MAX;
                loop {
                    std::thread::sleep(Duration::from_millis(200));
                    let now = made.load(Ordering::SeqCst);
                    if now == last {
                        return now;
                    }
                    last = now;
                }
            };
            let held = settled();
            assert!(held <= bound, "{len} lines: read {held} lines with the output stalled (bound {bound})");
            // Control: once the output moves, reading resumes and every
            // line gets its result.
            out.open.store(true, Ordering::SeqCst);
            let deadline = Instant::now() + Duration::from_secs(10);
            while made.load(Ordering::SeqCst) < held + 5.min(cap - held) && Instant::now() < deadline {
                std::thread::sleep(Duration::from_millis(5));
            }
            end.store(true, Ordering::SeqCst);
            rx.recv_timeout(Duration::from_secs(20)).expect("serve hung").unwrap();
            let lines = out.text.0.lock().unwrap().iter().filter(|&&c| c == b'\n').count();
            assert!(made.load(Ordering::SeqCst) > held || held == cap, "{len} lines: reading never resumed");
            assert_eq!(lines, 1 + made.load(Ordering::SeqCst), "{len} lines: one result per line");
        }
    }

    #[test]
    fn a_line_past_the_limit_is_skipped_and_refused() {
        // A line is held whole only up to MAX_LINE_BYTES; past that it is
        // skipped and refused, and the next line is read as usual. The
        // fake clock and the threaded ones read alike.
        let mut input = Vec::new();
        input.extend(std::iter::repeat_n(b'x', MAX_LINE_BYTES + 1));
        input.push(b'\n');
        // Control: a line of exactly the limit is read whole (leading
        // whitespace is valid JSON).
        let at_limit = r#"{"id": "max", "cmd": {"type": "crossfader", "value": 0.5}}"#;
        input.extend(std::iter::repeat_n(b' ', MAX_LINE_BYTES - at_limit.len()));
        input.extend_from_slice(at_limit.as_bytes());
        input.push(b'\n');
        // A CRLF line ends as a LF one does.
        input.extend_from_slice(br#"{"id": "after", "cmd": {"type": "crossfader", "value": 0.5}}"#);
        input.extend_from_slice(b"\r\n");
        // One at the end with no newline is refused too.
        input.extend(std::iter::repeat_n(b'x', MAX_LINE_BYTES + 5));
        let check = |text: &str| {
            let results: Vec<Value> = text.lines().map(|l| serde_json::from_str::<Value>(l).unwrap()).filter(|v| v["type"] == "result").collect();
            assert_eq!(results.len(), 4, "{results:?}");
            // The threaded clocks write results from two threads, so their
            // order is not the lines' order.
            let skipped = results.iter().filter(|r| r["id"].is_null()).collect::<Vec<_>>();
            assert_eq!(skipped.len(), 2, "{results:?}");
            assert!(skipped.iter().all(|r| r["error"]["message"].as_str().unwrap().contains("longer than")), "{skipped:?}");
            for id in ["max", "after"] {
                let r = results.iter().find(|r| r["id"] == id).unwrap_or_else(|| panic!("no result for {id}: {results:?}"));
                assert_eq!(r["ok"], true, "{r}");
            }
        };
        let mut out = Vec::new();
        serve_fake(io::Cursor::new(input.clone()), &mut out, 48000, |_| unreachable!(), None).unwrap();
        check(&String::from_utf8(out).unwrap());
        let out = Captured::default();
        serve_threaded_from(io::Cursor::new(input), out.clone(), 48000, "wall", |mut side, stop| {
            while !stop.load(Ordering::Relaxed) {
                side.process(64);
                std::thread::sleep(Duration::from_millis(1));
            }
        })
        .unwrap();
        check(&String::from_utf8(out.0.lock().unwrap().clone()).unwrap());
    }

    #[cfg(unix)]
    #[test]
    fn work_parked_behind_a_pending_load_is_bounded_like_the_mailbox() {
        // Codex on 00209c94: commands for a deck whose load is still decoding
        // queued without limit. The load reads a FIFO nobody writes to yet,
        // so it stays pending until the test lets it fail.
        let dir = std::env::temp_dir().join(format!("odj-serve-parked-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let fifo = dir.join("slow.wav");
        let _ = std::fs::remove_file(&fifo);
        assert!(std::process::Command::new("mkfifo").arg(&fifo).status().unwrap().success());
        let (reader, mut writer) = io::pipe().unwrap();
        let out = Captured::default();
        let (tx, rx) = mpsc::channel();
        let o = out.clone();
        std::thread::spawn(move || {
            let r = serve_threaded_from(io::BufReader::new(reader), o, 48000, "wall", |mut side, stop| {
                while !stop.load(Ordering::Relaxed) {
                    side.process(64);
                    std::thread::sleep(Duration::from_millis(1));
                }
            });
            let _ = tx.send(r);
        });
        let results = || -> Vec<Value> {
            let text = String::from_utf8(out.0.lock().unwrap().clone()).unwrap();
            text.lines().map(|l| serde_json::from_str::<Value>(l).unwrap()).filter(|v| v["type"] == "result").collect()
        };
        writeln!(writer, "{}", serde_json::json!({"id": "load", "cmd": {"type": "load", "deck": 1, "path": fifo}})).unwrap();
        let extra = 50;
        for i in 0..QUEUE_SLOTS + extra {
            writeln!(writer, "{}", serde_json::json!({"id": i, "cmd": {"type": "fader", "deck": 1, "value": 0.5}})).unwrap();
        }
        // Past the bound, each is refused at once, while the load still waits.
        let deadline = Instant::now() + Duration::from_secs(10);
        while results().len() < extra && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(5));
        }
        let early = results();
        assert_eq!(early.len(), extra, "{early:?}");
        for r in &early {
            let i = r["id"].as_u64().unwrap() as usize;
            assert!(i >= QUEUE_SLOTS, "{r}");
            assert!(r["error"]["message"].as_str().unwrap().contains("engine mailbox is full"), "{r}");
        }
        // Control: the first QUEUE_SLOTS stayed parked, and when the load
        // fails (the FIFO opened and closed with nothing in it) each of them
        // is refused as waiting on it, not as a full mailbox.
        drop(std::fs::OpenOptions::new().write(true).open(&fifo).unwrap());
        drop(writer);
        rx.recv_timeout(Duration::from_secs(20)).expect("serve hung").unwrap();
        let all = results();
        assert_eq!(all.len(), QUEUE_SLOTS + extra + 1, "one result per command");
        let waited = all.iter().filter(|r| r["error"]["message"].as_str().is_some_and(|m| m.contains("waited on failed"))).count();
        assert_eq!(waited, QUEUE_SLOTS);
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn threaded_loads_of_one_file_share_its_samples() {
        // Codex's case: on the wall and device clocks every load decoded its
        // own copy, even of a file another deck held or was still decoding.
        let dir = std::env::temp_dir().join(format!("odj-serve-share-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let write = |name: &str, v: f32| {
            let p = dir.join(name);
            let f = std::fs::File::create(&p).unwrap();
            crate::wav::write_f32(&mut io::BufWriter::new(f), 48000, &vec![v; 96000]).unwrap();
            p.display().to_string()
        };
        let (a, b) = (write("a.wav", 0.1), write("b.wav", 0.2));
        let (reader, mut writer) = io::pipe().unwrap();
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || {
            let _ = serve_threaded_from(io::BufReader::new(reader), io::sink(), 48000, "wall", move |mut side, stop| {
                while !stop.load(Ordering::Relaxed) {
                    side.process(64);
                    std::thread::sleep(Duration::from_millis(1));
                }
                let pcm = |d| side.engine.deck(d).and_then(|d| d.loaded()).map(|t| t.pcm.clone());
                let _ = tx.send([pcm(1), pcm(2), pcm(3)]);
            });
        });
        // Decks 1 and 2 load a.wav back to back, so the second load arrives
        // while the first is still decoding; deck 3 loads another file.
        for (deck, path) in [(1, &a), (2, &a), (3, &b)] {
            writeln!(writer, "{}", serde_json::json!({"cmd": {"type": "load", "deck": deck, "path": path}})).unwrap();
        }
        drop(writer);
        let [one, two, three] = rx.recv_timeout(Duration::from_secs(10)).expect("serve hung");
        let (one, two, three) = (one.expect("deck 1 loaded"), two.expect("deck 2 loaded"), three.expect("deck 3 loaded"));
        assert!(Arc::ptr_eq(&one, &two), "deck 2 decoded a second copy of a.wav");
        // Control: another file gets its own samples.
        assert!(!Arc::ptr_eq(&one, &three));
        assert_eq!((one[0], three[0]), (0.1, 0.2));
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn a_load_of_a_file_being_decoded_waits_and_shares_it() {
        // Many threads load one file at once: one decode, one copy.
        let dir = std::env::temp_dir().join(format!("odj-cache-race-{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("a.wav");
        crate::wav::write_f32(&mut io::BufWriter::new(std::fs::File::create(&p).unwrap()), 48000, &vec![0.1; 960_000]).unwrap();
        let cache = Arc::new(TrackCache::default());
        let spec = LoadSpec { deck: 1, path: p.display().to_string(), beats: vec![], bpm: None };
        let tracks: Vec<Arc<Track>> = (0..8)
            .map(|_| {
                let (cache, spec, p) = (cache.clone(), spec.clone(), p.clone());
                std::thread::spawn(move || cache.load(&p, &spec).unwrap())
            })
            .collect::<Vec<_>>()
            .into_iter()
            .map(|h| h.join().unwrap())
            .collect();
        assert!(tracks.iter().all(|t| Arc::ptr_eq(&t.pcm, &tracks[0].pcm)), "concurrent loads decoded more than one copy");
        // Control: once every holder lets go, the samples are freed.
        let samples = Arc::downgrade(&tracks[0].pcm);
        drop(tracks);
        assert!(samples.upgrade().is_none());
        let _ = std::fs::remove_dir_all(&dir);
    }

    #[test]
    fn an_advance_is_bounded_before_it_renders() {
        let hour = MAX_ADVANCE_S * 48000;
        assert_eq!(advance_frames(Advance::Frames(hour), 48000, None).unwrap(), hour);
        assert_eq!(advance_frames(Advance::Ms(MAX_ADVANCE_S as f64 * 1000.0), 48000, None).unwrap(), hour);
        for a in [Advance::Frames(hour + 1), Advance::Frames(u64::MAX), Advance::Ms(1e300), Advance::Ms(3_600_001.0)] {
            assert_eq!(advance_frames(a, 48000, None).unwrap_err().code, ErrorCode::Invalid, "{a:?}");
        }
        // A recording stops where its WAV file would: the last frame that
        // fits is accepted, one more is not.
        let max = crate::wav::MAX_F32_FRAMES;
        assert_eq!(advance_frames(Advance::Frames(1000), 48000, Some(max - 1000)).unwrap(), 1000);
        assert_eq!(advance_frames(Advance::Frames(1001), 48000, Some(max - 1000)).unwrap_err().code, ErrorCode::Invalid);
        // Control: without a recording the same advance is fine.
        assert_eq!(advance_frames(Advance::Frames(1001), 48000, None).unwrap(), 1001);
    }

    #[test]
    fn state_is_stamped_with_when_its_position_is_heard() {
        let (mut side, _cmd_tx, _res_rx) = side(4);
        let (state_tx, mut state_rx) = rtrb::RingBuffer::new(4);
        side.state_tx = state_tx;
        // One state per 480-frame block, published at the block's end.
        side.frames_per_state = 480;
        let t0 = side.epoch.elapsed().as_nanos() as u64;
        side.set_ahead_ns(50_000_000);
        side.process(480);
        let (_, host_ns) = state_rx.pop().unwrap();
        // 50 ms of device latency plus the 10 ms block just rendered.
        let lead = host_ns - t0;
        assert!((60_000_000..70_000_000).contains(&lead), "lead {lead} ns");
        // Control: with nothing ahead, only the block's own length.
        side.set_ahead_ns(0);
        let t1 = side.epoch.elapsed().as_nanos() as u64;
        side.process(480);
        let (_, host_ns) = state_rx.pop().unwrap();
        assert!((10_000_000..20_000_000).contains(&(host_ns - t1)), "lead {} ns", host_ns - t1);
    }
}
