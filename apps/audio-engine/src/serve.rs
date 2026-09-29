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
use crate::protocol::{self, Advance, Command, LoadSpec, ProtoError};

/// State messages per second in the threaded modes.
pub const STATE_HZ: u32 = 30;

/// Slots in the command ring from the control side to the audio side.
const CMD_SLOTS: usize = 1024;

/// How long shutdown waits for the audio side to apply what is queued.
const DRAIN_LIMIT: Duration = Duration::from_secs(2);
/// How long shutdown waits for loads still decoding; after that they and the
/// work queued behind them are refused, so each still gets a result.
const LOAD_DRAIN_LIMIT: Duration = Duration::from_secs(5);

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
    for line in input.lines() {
        let line = line?;
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
            Command::Advance(a) => {
                let mut left = match a {
                    Advance::Frames(f) => f,
                    Advance::Ms(ms) => (ms * sample_rate as f64 / 1000.0).round() as u64,
                };
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
        send(&mut out, &protocol::result_json(id.as_deref(), &res))?;
        if then_state {
            send(&mut out, &protocol::state_json(&engine.snapshot(), None))?;
        }
        if stop {
            break;
        }
    }
    Ok(())
}

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
    scratch: Vec<f32>,
}

impl AudioSide {
    /// Render `frames` frames into `self.scratch` after applying queued
    /// commands, publishing a snapshot when one is due. Returns the rendered
    /// interleaved stereo.
    pub fn process(&mut self, frames: usize) -> &[f32] {
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
        let n = frames.min(self.scratch.len() / 2);
        self.engine.render(&mut self.scratch[..n * 2]);
        self.frames_since_state += n as u64;
        if self.frames_since_state >= self.frames_per_state || self.state_req.swap(false, Ordering::Relaxed) {
            self.frames_since_state = 0;
            let host_ns = self.epoch.elapsed().as_nanos() as u64;
            let _ = self.state_tx.push((self.engine.snapshot(), host_ns));
        }
        &self.scratch[..n * 2]
    }
}

enum Msg {
    Line(String),
    Decoded { seq: u64, deck: DeckId, result: Result<Arc<Track>, ProtoError> },
    Eof,
    /// The audio side returned. Before `stop` is set that means it failed to
    /// start or died, e.g. the output device would not open.
    AudioExited,
}

/// Work parked behind a deck's pending load.
enum Queued {
    Cmd(Option<String>, EngineCmd),
    Load(Option<String>, LoadSpec),
}

/// The control half: owns the command ring's producer, the per-deck load
/// queues and the id bookkeeping. Runs on the main thread.
struct Control {
    cmd_tx: rtrb::Producer<(u64, EngineCmd)>,
    ids: Arc<Mutex<HashMap<u64, Option<String>>>>,
    out: Arc<Mutex<io::Stdout>>,
    msg_tx: mpsc::Sender<Msg>,
    next_seq: u64,
    /// Per deck: Some while a load is decoding, holding work queued behind it.
    waiting: [Option<VecDeque<Queued>>; MAX_DECKS],
    /// Per deck: the seq of the load that is decoding, while `waiting` is Some.
    loading: [Option<u64>; MAX_DECKS],
    state_req: Arc<AtomicBool>,
    /// The engine's rate; loads are resampled to it off the audio thread.
    sample_rate: u32,
}

impl Control {
    fn reply(&self, id: Option<&str>, res: Result<(), ProtoError>) {
        let mut o = self.out.lock().unwrap();
        let _ = send(&mut *o, &protocol::result_json(id, &res));
    }

    fn seq(&mut self, id: Option<String>) -> u64 {
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
            self.reply(id.as_deref(), Err(ProtoError::new(ErrorCode::Invalid, "engine mailbox is full; command dropped")));
            return false;
        }
        true
    }

    fn deck_of(cmd: &EngineCmd) -> Option<DeckId> {
        use EngineCmd::*;
        match cmd {
            Load { deck, .. } | Unload { deck } | Play { deck, .. } | Cue { deck } | Seek { deck, .. }
            | Loop { deck, .. } | BeatLoop { deck, .. } | BeatJump { deck, .. } | Tempo { deck, .. }
            | PitchRange { deck, .. } | MasterTempo { deck, .. } | KeyNudge { deck, .. } | Trim { deck, .. } | Eq { deck, .. } | Filter { deck, .. }
            | Fader { deck, .. } | Assign { deck, .. } => Some(*deck),
            Crossfader { .. } | MasterVolume { .. } | MasterMute { .. } => None,
        }
    }

    fn queue_for(&mut self, deck: DeckId) -> Option<&mut VecDeque<Queued>> {
        self.waiting[deck as usize - 1].as_mut()
    }

    fn dispatch(&mut self, id: Option<String>, cmd: EngineCmd) {
        if let Some(d) = Self::deck_of(&cmd) {
            if let Some(q) = self.queue_for(d) {
                q.push_back(Queued::Cmd(id, cmd));
                return;
            }
        }
        let seq = self.seq(id);
        let _ = self.push_seq(seq, cmd);
    }

    fn load(&mut self, id: Option<String>, spec: LoadSpec) {
        let deck = spec.deck;
        if let Some(q) = self.queue_for(deck) {
            q.push_back(Queued::Load(id, spec));
            return;
        }
        let seq = self.seq(id);
        self.waiting[deck as usize - 1] = Some(VecDeque::new());
        self.loading[deck as usize - 1] = Some(seq);
        let tx = self.msg_tx.clone();
        let sr = self.sample_rate;
        std::thread::spawn(move || {
            let result = crate::decode::decode_at(std::path::Path::new(&spec.path), sr)
                .map(|d| Arc::new(Track::new(d.sample_rate, d.pcm, spec.beats, spec.bpm)));
            let _ = tx.send(Msg::Decoded { seq, deck, result });
        });
    }

    fn finish_load(&mut self, seq: u64, deck: DeckId, result: Result<Arc<Track>, ProtoError>) {
        self.loading[deck as usize - 1] = None;
        // True when the load will not reach the engine.
        let dropped = match result {
            Ok(track) => !self.push_seq(seq, EngineCmd::Load { deck, track }),
            Err(e) => {
                let id = self.ids.lock().unwrap().remove(&seq).flatten();
                self.reply(id.as_deref(), Err(e));
                true
            }
        };
        let q = self.waiting[deck as usize - 1].take().unwrap_or_default();
        if dropped {
            // The load failed to decode or never reached the engine, so the
            // work queued behind it would run against the previous track.
            // Fail it instead: every command after the failed load is refused.
            self.refuse_queued(q);
            return;
        }
        // Release this deck's queue in order. A queued load re-arms the wait,
        // and everything after it stays parked behind that load.
        for item in q {
            match item {
                Queued::Cmd(id, cmd) => self.dispatch(id, cmd),
                Queued::Load(id, spec) => self.load(id, spec),
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
                    id.as_deref(),
                    Err(ProtoError::new(ErrorCode::Invalid, "the engine shut down before this load finished")),
                );
            }
            if let Some(q) = self.waiting[d].take() {
                self.refuse_queued(q);
            }
        }
    }

    fn refuse_queued(&self, q: VecDeque<Queued>) {
        for item in q {
            let id = match item {
                Queued::Cmd(id, _) | Queued::Load(id, _) => id,
            };
            self.reply(
                id.as_deref(),
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
            Err(e) => self.reply(id.as_deref(), Err(e)),
            Ok(Command::Load(spec)) => self.load(id, spec),
            Ok(Command::Apply(c)) => self.dispatch(id, c),
            Ok(Command::Advance(_)) => self.reply(
                id.as_deref(),
                Err(ProtoError::new(
                    ErrorCode::WrongClock,
                    format!("engine_advance needs the fake clock; this engine runs on the {clock} clock"),
                )),
            ),
            Ok(Command::State) => {
                self.state_req.store(true, Ordering::Relaxed);
                self.reply(id.as_deref(), Ok(()));
            }
            Ok(Command::Shutdown) => {
                self.reply(id.as_deref(), Ok(()));
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
    serve_threaded_from(io::BufReader::new(io::stdin()), sample_rate, clock, run_audio)
}

fn serve_threaded_from(
    input: impl BufRead + Send + 'static,
    sample_rate: u32,
    clock: &'static str,
    run_audio: impl FnOnce(AudioSide, Arc<AtomicBool>) + Send + 'static,
) -> io::Result<()> {
    let (cmd_tx, cmd_rx) = rtrb::RingBuffer::new(CMD_SLOTS);
    let (res_tx, mut res_rx) = rtrb::RingBuffer::<AudioResult>::new(1024);
    let (state_tx, mut state_rx) = rtrb::RingBuffer::new(64);
    let stop = Arc::new(AtomicBool::new(false));
    let state_req = Arc::new(AtomicBool::new(false));
    let out = Arc::new(Mutex::new(io::stdout()));
    let ids: Arc<Mutex<HashMap<u64, Option<String>>>> = Arc::new(Mutex::new(HashMap::new()));

    {
        let mut o = out.lock().unwrap();
        send(&mut *o, &protocol::hello_json(clock, sample_rate))?;
    }

    let audio = AudioSide {
        engine: Engine::new(sample_rate),
        cmd_rx,
        res_tx,
        state_tx,
        state_req: state_req.clone(),
        frames_per_state: (sample_rate / STATE_HZ) as u64,
        frames_since_state: 0,
        epoch: Instant::now(),
        scratch: vec![0.0; MAX_BLOCK * 2 * 8],
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
    let pump = std::thread::spawn(move || loop {
        let mut idle = true;
        while let Ok((seq, r)) = res_rx.pop() {
            idle = false;
            let id = pump_ids.lock().unwrap().remove(&seq).flatten();
            let res = r.map(drop).map_err(ProtoError::from);
            let mut o = pump_out.lock().unwrap();
            let _ = send(&mut *o, &protocol::result_json(id.as_deref(), &res));
        }
        while let Ok((snap, host_ns)) = state_rx.pop() {
            idle = false;
            let mut o = pump_out.lock().unwrap();
            let _ = send(&mut *o, &protocol::state_json(&snap, Some(host_ns)));
        }
        if pump_stop_flag.load(Ordering::Relaxed) && idle {
            break;
        }
        if idle {
            std::thread::sleep(Duration::from_millis(2));
        }
    });

    let reader_tx = msg_tx.clone();
    std::thread::spawn(move || {
        for line in input.lines() {
            match line {
                Ok(l) => {
                    if reader_tx.send(Msg::Line(l)).is_err() {
                        return;
                    }
                }
                Err(_) => break,
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
        sample_rate,
    };
    let mut audio_failed = false;
    while let Ok(msg) = msg_rx.recv() {
        match msg {
            Msg::Line(l) => {
                if !control.handle_line(&l, clock) {
                    break;
                }
            }
            Msg::Decoded { seq, deck, result } => control.finish_load(seq, deck, result),
            // Supervisor gone: the engine has no reason to outlive it.
            Msg::Eof => break,
            Msg::AudioExited => {
                audio_failed = true;
                break;
            }
        }
    }
    // Loads still decoding hold their own result and the work parked behind
    // them. Finish them (which releases that work to the mailbox), bounded;
    // whatever is still pending after that is refused. A dead audio side
    // would never apply any of it, so it is refused at once.
    if !audio_failed {
        let deadline = Instant::now() + LOAD_DRAIN_LIMIT;
        while control.loads_pending() {
            match msg_rx.recv_timeout(deadline.saturating_duration_since(Instant::now())) {
                Ok(Msg::Decoded { seq, deck, result }) => control.finish_load(seq, deck, result),
                Ok(Msg::AudioExited) => {
                    audio_failed = true;
                    break;
                }
                // Nothing sent after shutdown or EOF is taken.
                Ok(Msg::Line(_) | Msg::Eof) => {}
                Err(_) => break,
            }
        }
    }
    control.refuse_pending();
    // Let the audio side apply everything already queued before stopping,
    // so every command sent before shutdown or EOF gets its result. A dead
    // audio side never drains, so it is not waited on.
    if !audio_failed {
        let deadline = Instant::now() + DRAIN_LIMIT;
        while control.cmd_tx.slots() < CMD_SLOTS && Instant::now() < deadline {
            std::thread::sleep(Duration::from_millis(1));
        }
    }
    stop.store(true, Ordering::Relaxed);
    let _ = audio_thread.join();
    pump_stop.store(true, Ordering::Relaxed);
    let _ = pump.join();
    if audio_failed {
        return Err(io::Error::other("the audio side stopped before shutdown (see stderr)"));
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
        let (cmd_tx, cmd_rx) = rtrb::RingBuffer::new(16);
        let (res_tx, res_rx) = rtrb::RingBuffer::new(res_slots);
        let (state_tx, _state_rx) = rtrb::RingBuffer::new(4);
        let side = AudioSide {
            engine: Engine::new(48000),
            cmd_rx,
            res_tx,
            state_tx,
            state_req: Arc::new(AtomicBool::new(false)),
            frames_per_state: u64::MAX,
            frames_since_state: 0,
            epoch: Instant::now(),
            scratch: vec![0.0; MAX_BLOCK * 2],
        };
        (side, cmd_tx, res_rx)
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
            let _ = tx.send(serve_threaded_from(io::BufReader::new(reader), 48000, "wall", |_side, _stop| {}));
        });
        let r = rx.recv_timeout(Duration::from_secs(10)).expect("serve hung after its audio side stopped");
        assert!(r.is_err());
    }
}
