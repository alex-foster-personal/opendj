/**
 * Shared contracts for the /performance rekordbox-parity build.
 *
 * THIS FILE IS THE CONTRACT between the parallel build units
 * (topbar, wavestack, deck, mixer, browser, audio-engine).
 * Builders MUST NOT edit it. If a contract change is needed, stop and
 * escalate to the coordinating session.
 *
 * Grounding: .planning/rekordbox-parity/SCREENSHOT-SPEC.md (ground truth)
 * and .planning/rekordbox-parity/COMPONENT-MAP.md (data sources + the
 * /anlz and /rb-meta JSON shapes mirrored 1:1 below).
 */

// ---------------------------------------------------------------- core ids

/** Physical deck slot 1-4. Layout: 1 top-left, 2 top-right, 3 bottom-left, 4 bottom-right. */
export type DeckId = 1 | 2 | 3 | 4;

/** Beat Sync phase target. Beat mode matches the closest beat; bar mode also
 * requires the follower PQTZ beat number (1..4) to match the master. */
export type SyncMode = 'beat' | 'bar';

/** Hot-cue bank slot letter. djmdCue Kind 1..8 maps to A..H (COMPONENT-MAP 2.3). */
export type HotCueSlot = 'A' | 'B' | 'C' | 'D' | 'E' | 'F' | 'G' | 'H';

/** Cue taxonomy from the /anlz endpoint. Kind 9-11 rows are excluded server-side v1. */
export type AnlzCueKind = 'memory' | 'hot_cue' | 'loop';

/** Artwork size enum for GET /tracks/{sid}/artwork (COMPONENT-MAP 2.2). */
export type ArtworkSize = 's' | 'm' | 'orig';

// ------------------------------------------------- /anlz response payload
// Mirrors GET /api/v1/tracks/{stable_id}/anlz (COMPONENT-MAP 2.3) exactly.
// Empty arrays are REAL states (track has no cues / no beatgrid) - never pad.

/** One waveform band set. Arrays are same-length normalised floats 0..1. */
export interface AnlzWaveformBands {
	/** Number of points in each band array. */
	length: number;
	/** Low-frequency energy per point (rendered orange). */
	low: number[];
	/** Mid-frequency energy per point (rendered blue). */
	mid: number[];
	/** High-frequency energy per point (rendered white). */
	high: number[];
}

/** Waveform payload: 400-point preview strip + <=2400-point detail. */
export interface AnlzWaveform {
	/** 'tri' = real tri-band colour data (.EXT PWV5/PWV4); 'mono' = heights only
	 * (PWAV/PWV3) - when 'mono' the client renders a single-colour waveform,
	 * NEVER synthesised bands. */
	kind: 'tri' | 'mono';
	/** Native 400-point PWAV overview (deck strip + browser Preview column). */
	preview: AnlzWaveformBands;
	/** PWV3/PWV5 downsampled server-side via per-bucket max (wavestack rows). */
	detail: AnlzWaveformBands;
}

/** One beat from ANLZ PQTZ. */
export interface AnlzBeat {
	/** Beat number within the bar, 1..4. n == 1 is the bar tick. */
	n: number;
	/** Local BPM at this beat. */
	bpm: number;
	/** Beat time in SECONDS from track start. */
	t: number;
}

/** Beatgrid payload from ANLZ PQTZ. */
export interface AnlzBeatgrid {
	/** Total beats; 0 with empty beats[] is a real no-grid state. */
	beat_count: number;
	/** Ordered beats. */
	beats: AnlzBeat[];
}

/** One cue/loop row sourced from djmdCue (ANLZ PCOB is empty in rb6/7). */
export interface AnlzCue {
	/** memory | hot_cue | loop (djmdCue Kind 0 / 1..8 / OutMsec > 0). */
	kind: AnlzCueKind;
	/** Hot-cue bank letter A..H; null for memory cues. */
	slot: HotCueSlot | null;
	/** Cue-in position in MILLISECONDS. */
	in_ms: number;
	/** Loop-out position in ms; null when not a loop. */
	out_ms: number | null;
	/** True when this row defines a loop (out_ms present). */
	is_loop: boolean;
	/** True when rekordbox stored this loop as the active one. */
	active_loop: boolean;
	/** Beat-length of the loop (4, 8, 16 ...); null when not beat-quantised. */
	beat_loop_size: number | null;
	/** rekordbox colour table index for the marker; null when unset. */
	color_table_index: number | null;
	/** User comment on the cue ('2nd drop'); null when unset. */
	comment: string | null;
}

/** One phrase from ANLZ .EXT PSSI; response field may be an empty array. */
export interface AnlzPhrase {
	/** Phrase start in seconds. */
	start_s: number;
	/** Phrase end in seconds. */
	end_s: number;
	/** Raw PSSI phrase kind id (rendered as chevron cadence only at v1). */
	kind: number;
	/** Raw PSSI mood id. */
	mood: number;
}

/** Full GET /tracks/{stable_id}/anlz response. */
export interface AnlzData {
	/** 40-hex stable id the payload belongs to. */
	stable_id: string;
	/** The points param the detail waveform was downsampled to (<=2400). */
	points: number;
	/** Waveform bands; see AnlzWaveform. */
	waveform: AnlzWaveform;
	/** Beatgrid; see AnlzBeatgrid. */
	beatgrid: AnlzBeatgrid;
	/** Cues + loops; sparse coverage is real - most tracks show none. */
	cues: AnlzCue[];
	/** Phrases; empty array when PSSI absent. */
	phrases: AnlzPhrase[];
}

// ---------------------------------------------- /rb-meta response payload
// Mirrors GET /api/v1/tracks/{stable_id}/rb-meta (COMPONENT-MAP 2.4) exactly.

export interface RbMeta {
	/** 40-hex stable id. */
	stable_id: string;
	/** Always 'rekordbox' at v1. */
	vendor: string;
	/** djmdContent.ID in the decrypted master.plain.db. */
	vendor_id: string;
	/** True when the resolved FolderPath exists on disk. False for the 74%
	 * dead-path rows - a REAL library state the browser must show. */
	file_exists: boolean;
	/** True when FolderPath is a tidal:/soundcloud:/spotify: URI (cloud icon,
	 * load action inert). */
	is_streaming: boolean;
	/** Resolved absolute path or streaming URI. */
	folder_path: string;
	/** Genre column value from djmdContent join; null when unset. */
	genre: string | null;
	/** True when artwork files exist - lets the browser skip doomed fetches. */
	artwork_available: boolean;
	/** True when the ANLZ dir exists - ditto for /anlz. */
	analysis_available: boolean;
	/** Live djmdCue row count for the track. */
	cue_count: number;
}

// -------------------------------------------------------------- deck state

/** A hot-cue bank slot as the Deck component consumes it (derived from
 * AnlzCue rows with kind 'hot_cue' or 'loop' and slot != null). */
export interface HotCue {
	/** Bank letter A..H. */
	slot: HotCueSlot;
	/** Jump target in ms; clicking a populated slot seeks here (real). */
	in_ms: number;
	/** Loop-out ms when the slot stores a loop; null otherwise. */
	out_ms: number | null;
	/** True when the slot stores a loop (renders loop glyph + time chips). */
	is_loop: boolean;
	/** rekordbox colour table index; null = default colour. */
	color_table_index: number | null;
	/** User comment; null when unset. */
	comment: string | null;
}

/** Active loop on a deck. v1: display-only from stored cues; the beat-length
 * cluster (INT / 8 / < >) is inert per COMPONENT-MAP 1.3. */
export interface LoopState {
	/** Loop-in position ms. */
	in_ms: number;
	/** Loop-out position ms. */
	out_ms: number;
	/** True when the audio engine is actively looping this range. */
	engaged: boolean;
	/** Beat length (4/8/16) when beat-quantised; null when time-based. */
	beat_length: number | null;
}

// ------------------------------------------------------------- stem state

/** Rekordbox's three user-facing stem groups. Instrumental is the real
 * Demucs bass + other pair, never source-audio subtraction. */
export type StemControl = 'vocal' | 'instrumental' | 'drums';

export interface StemControlState {
	muted: boolean;
	solo: boolean;
}

export interface StemAlignment {
	sample_rate_hz: number;
	frame_count: number;
	channel_count: number;
	duration_ms: number;
}

/** Serializable stem graph capability and control read model. `ready` is
 * reserved for four decoded, metadata-aligned Demucs parts. */
export interface StemDeckState {
	status: 'unavailable' | 'ready' | 'error';
	source: 'demucs' | null;
	model: string | null;
	alignment: StemAlignment | null;
	controls: Record<StemControl, StemControlState>;
	error: string | null;
}

/** Everything one deck panel + its wavestack row renders. Owned by the
 * audio-engine rune store (MUST live in a .svelte.ts module). */
export interface DeckState {
	/** Which physical deck this is. */
	deck_id: DeckId;
	/** Loaded track stable id; null = empty deck (blank wave row, dim panel). */
	stable_id: string | null;
	/** Track title; null until loaded. */
	title: string | null;
	/** Track artist; null until loaded. */
	artist: string | null;
	/** Track BPM from TrackOut (format to 2dp, e.g. 128.00); null unknown. */
	bpm: number | null;
	/** Camelot key text from TrackOut (e.g. '7A'); null unknown. */
	key: string | null;
	/** Musical pitch transposition applied by the real deck DSP, in integer
	 * semitones from -12 through +12. The source key remains immutable. */
	key_shift_semitones: number;
	/** Track length ms from TrackOut.duration_ms; null until loaded. */
	duration_ms: number | null;
	/** Playhead position ms - UI mirror of the engine clock, updated via rAF. */
	position_ms: number;
	/** Desired transport state. Future worklet schedules update this immediately. */
	playing: boolean;
	/** True only while the currently committed worklet segment is audible. */
	audible: boolean;
	/** True while desired transport/DSP state is scheduled but not yet audible. */
	transport_pending: boolean;
	/** CUE point ms for return-to-cue transport semantics; null = track start. */
	cue_ms: number | null;
	/** Playback rate ratio; 1.0 = 0% pitch. Driven live by the deck pitch
	 * fader (setTempoRatio), constrained to the deck's selected pitch range. */
	pitch: number;
	/** Quantize transport seeks, cue placement, and loop endpoints to PQTZ. */
	quantize_enabled: boolean;
	/** Follow the elected master deck's local PQTZ tempo and phase. */
	beat_sync_enabled: boolean;
	/** Preserve source pitch while tempo changes through Signalsmith Stretch. */
	master_tempo_enabled: boolean;
	/** Arm slip mode. This does not alter an existing transport schedule. */
	slip_enabled: boolean;
	/** True only while an audible loop has an independently advancing hidden transport position. */
	slip_active: boolean;
	/** Hidden linear playhead in milliseconds while SLIP is active, otherwise null. */
	slip_position_ms: number | null;
	/** Beat-only or beat-number-within-bar phase alignment. */
	sync_mode: SyncMode;
	/** Explicit grid/rate/scheduling failure. null means no sync failure. */
	sync_error: string | null;
	/** Terminal AudioWorklet failure. null means the processor is healthy. */
	processor_error: string | null;
	/** Real precomputed stem artifact and graph state. */
	stems: StemDeckState;
	/** Active loop or null. */
	loop: LoopState | null;
	/** Hot-cue bank content (empty slots = letters absent from this array). */
	hot_cues: HotCue[];
	/** Server-issued CAS revisions for every A-H slot, including empty slots. */
	hot_cue_revisions: Record<HotCueSlot, string>;
	/** Analysis payload once fetched; null while absent. */
	anlz: AnlzData | null;
	/** Explicit no-analysis state: the error code (e.g. ANALYSIS_NOT_FOUND)
	 * when /anlz 404s. Renders the 'no analysis' treatment - never invented
	 * waveforms. null = not attempted or succeeded. */
	anlz_error: string | null;
	/** Globally exclusive MASTER deck state. */
	is_master: boolean;
}

/** Serializable real post-deck-DSP, pre-mixer/master analyser snapshot. */
export interface DeckAudioSnapshot {
	context_time_s: number;
	sample_rate_hz: number;
	fft_size: number;
	frequency_db: number[];
	time_domain: number[];
}

// ------------------------------------------------------------- mixer state

/** EQ band selector for AudioEngine.setEq. */
export type EqBand = 'low' | 'mid' | 'high';

/** Crossfader bus assignment for one channel.
 * 'A' = left bus, 'B' = right bus, 'THRU' = bypass the crossfader. */
export type CrossfaderAssign = 'A' | 'B' | 'THRU';

/** One mixer channel strip. Channel order on screen is 3 1 2 4. */
export interface MixerChannelState {
	/** The deck this strip controls. */
	deck_id: DeckId;
	/** TRIM knob 0..1; 0.5 = unity gain. Engine maps to input GainNode. */
	trim: number;
	/** HIGH knob 0..1; 0.5 = flat. Engine maps to highshelf BiquadFilter dB. */
	eq_high: number;
	/** MID knob 0..1; 0.5 = flat. Engine maps to peaking BiquadFilter dB. */
	eq_mid: number;
	/** LOW knob 0..1; 0.5 = flat. Engine maps to lowshelf BiquadFilter dB. */
	eq_low: number;
	/** Vertical channel fader 0..1; 1 = full. Engine maps to fader GainNode. */
	fader: number;
	/** Crossfader bus assignment (the 2x2 numeral matrices). */
	assign: CrossfaderAssign;
	/** Headphone pre-fader cue assignment for this channel. */
	cue_enabled: boolean;
}

/** One real browser-selectable audio output. Labels may be empty until the
 * browser grants device-label permission. */
export interface HeadphoneOutputDevice {
	id: string;
	label: string;
}

/** Serializable headphone cue-bus read model. `active` means the monitor
 * stream is attached to the element and the selected sink accepted playback. */
export interface HeadphoneState {
	mix: number;
	level: number;
	selected_output_device_id: string | null;
	outputs: HeadphoneOutputDevice[];
	supported: boolean;
	active: boolean;
	error: string | null;
}

/** Whole mixer surface including the real headphone cue bus. */
export interface MixerState {
	/** All four channel strips keyed by deck. */
	channels: Record<DeckId, MixerChannelState>;
	/** Horizontal crossfader position 0..1; 0 = full A, 1 = full B. */
	crossfader: number;
	/** Master volume 0..1 (topbar horizontal slider -> master GainNode). */
	master: number;
	/** Headphone cue / monitor output state. */
	headphones: HeadphoneState;
}

// ----------------------------------------------------------- browser state

/** One hydrated row in the browser track list. Field order mirrors the
 * screenshot columns (SCREENSHOT-SPEC 5c). */
export interface TrackRow {
	/** 40-hex stable id. */
	stable_id: string;
	/** 1-based membership position within the pane playlist (# column). */
	order: number;
	/** Track Title column; null renders empty. */
	title: string | null;
	/** Artist column; null renders empty. */
	artist: string | null;
	/** K column - Camelot key; null renders empty. */
	key: string | null;
	/** B column - BPM; null renders empty. */
	bpm: number | null;
	/** Rating column 0..5 stars, editable via existing PATCH + If-Match etag. */
	rating: number | null;
	/** Current etag for optimistic-concurrency PATCH; from GET /tracks/{sid}. */
	etag: string;
	/** Comments column (TrackOut.notes); mostly empty, matches screenshot. */
	comments: string | null;
	/** Time column source in ms; render MM:SS. null renders empty. */
	duration_ms: number | null;
	/** rb-meta payload once fetched (cloud icon, missing-file state, Genre
	 * column); null while not yet loaded. */
	rb_meta: RbMeta | null;
	/** Preview column waveform once lazily fetched; null = not loaded yet.
	 * 'unavailable' = /anlz said no analysis - render the empty strip. */
	preview: AnlzWaveformBands | 'unavailable' | null;
}

/** One node in the playlist tree panel (5b). state.db has NO folder
 * hierarchy, so v1 produces: one 'all_tracks' node + flat 'playlist' nodes. */
export interface PlaylistNode {
	/** Playlist id from PlaylistSummary; 'all' for the All Tracks node. */
	playlist_id: string;
	/** Display name. */
	name: string;
	/** Right-aligned track count - OUR real count, never the screenshot's. */
	track_count: number;
	/** Node flavour; 'folder' reserved for future hierarchy, unused v1. */
	kind: 'all_tracks' | 'playlist' | 'folder';
	/** Children for folder nodes; always [] at v1. */
	children: PlaylistNode[];
}

// ------------------------------------------------------------ audio engine

/**
 * Public interface of the client-side Web Audio engine (build unit
 * audio-engine). Graph per deck: source -> TRIM gain -> 3-band EQ
 * (lowshelf/peaking/highshelf) -> channel fader gain -> crossfader gain ->
 * master gain -> destination. The implementing rune store MUST live in a
 * .svelte.ts module (rune_outside_svelte otherwise - RECON-FRONTEND 10.1).
 *
 * Fail-fast contract: every method throws (or rejects) loudly on invalid
 * input or missing backing data - no silent no-ops, no fabricated audio.
 */
export interface AudioEngine {
	/** Stop and release all route-owned processors, nodes, clocks, and context. */
	dispose(): Promise<void>;
	/** Fetch /tracks/{sid}/audio, decodeAudioData, build the deck chain and
	 * populate DeckState. Rejects with the backend error code on 404
	 * (AUDIO_FILE_MISSING / AUDIO_IS_STREAMING_URI / TRACK_NOT_FOUND). */
	load(deck: DeckId, stable_id: string): Promise<void>;
	/** Start/resume transport from the current position. Throws if no track
	 * is loaded on the deck. */
	play(deck: DeckId): Promise<void>;
	/** Pause transport, keeping position. Throws if no track loaded. */
	pause(deck: DeckId): Promise<void>;
	/** Seek to a position in ms (hot-cue click / CUE return). Implemented as
	 * buffer-source restart at offset. Throws if no track loaded. */
	cueJump(deck: DeckId, ms: number): Promise<void>;
	/** Set playback-rate ratio from the pitch fader. Validates 0 < ratio and
	 * that it fits the deck's selected pitch range. */
	setPitch(deck: DeckId, ratio: number): Promise<void>;
	/** Semantic alias for setPitch: ratio controls tempo, while Master Tempo
	 * independently controls whether pitch is preserved. */
	setTempoRatio(deck: DeckId, ratio: number): Promise<void>;
	/** Engage a loop range (ms) or disengage with null. */
	setLoop(deck: DeckId, loop: { in_ms: number; out_ms: number } | null): Promise<void>;
	/** Enable/disable PQTZ snapping. Defaults true per deck. */
	setQuantize(deck: DeckId, enabled: boolean): void;
	/** Enable/disable master tempo/phase following. Defaults true per deck. */
	setBeatSync(deck: DeckId, enabled: boolean): Promise<void>;
	/** Enable/disable pitch preservation in the Signalsmith processor. */
	setMasterTempo(deck: DeckId, enabled: boolean): Promise<void>;
	/** Arm or disarm SLIP. Disarming active slip resumes through the normal schedule first. */
	setSlip(deck: DeckId, enabled: boolean): Promise<void>;
	/** Shift the loaded deck by exactly one semitone. */
	nudgeKey(deck: DeckId, semitones: -1 | 1): Promise<void>;
	/** Align the loaded deck to the elected loaded master using the documented
	 * deterministic Camelot harmonic policy. */
	syncKey(deck: DeckId): Promise<void>;
	/** Select beat or bar phase alignment for Beat Sync. */
	setSyncMode(deck: DeckId, mode: SyncMode): Promise<void>;
	/** Elect one loaded deck as the globally exclusive master. */
	setDeckMaster(deck: DeckId): Promise<void>;
	/** Mute one real stem group. Rejects unless aligned artifacts are ready. */
	setStemMute(deck: DeckId, stem: StemControl, muted: boolean): void;
	/** Solo one real stem group. Rejects unless aligned artifacts are ready. */
	setStemSolo(deck: DeckId, stem: StemControl, solo: boolean): void;
	/** Explicit seek entry point. cueJump delegates here so quantize is central. */
	quantizedSeek(deck: DeckId, ms: number): Promise<void>;
	/** Capture real post-Signalsmith analyser data; never synthesised. */
	captureDeckAudio(deck: DeckId): DeckAudioSnapshot;
	/** TRIM knob 0..1 (0.5 = unity) -> per-channel input GainNode. */
	setTrim(deck: DeckId, value: number): void;
	/** One EQ band knob 0..1 (0.5 = flat) -> Biquad gain in dB. */
	setEq(deck: DeckId, band: EqBand, value: number): void;
	/** Channel fader 0..1 -> fader GainNode. */
	setFader(deck: DeckId, value: number): void;
	/** Crossfader 0..1 (0 = full A, 1 = full B); applies the gain pair to
	 * every channel assigned A or B. */
	setCrossfader(value: number): void;
	/** Route a channel to crossfader bus A, B, or THRU (bypass). */
	assignChannel(deck: DeckId, assign: CrossfaderAssign): void;
	/** Enable or disable a channel's post-EQ, pre-fader headphone cue tap. */
	setChannelCue(deck: DeckId, enabled: boolean): void;
	/** Set CUE-to-MASTER monitor mix and headphone level. */
	setHeadphoneMix(value: number): void;
	setHeadphoneLevel(value: number): void;
	/** Enumerate browser audio-output devices for explicit sink selection. */
	refreshHeadphoneOutputs(): Promise<void>;
	/** Acquire an output through the browser's user-gesture permission chooser and select it. */
	acquireHeadphoneOutput(): Promise<void>;
	/** Route the real monitor element to an explicitly enumerated output device. */
	selectHeadphoneOutput(deviceId: string): Promise<void>;
}
