/**
 * The page's link to `odj-audio`, the Rust audio engine (GSD plan 20-02).
 *
 * The Python engine supervises `odj-audio` and publishes where it listens at
 * `GET /api/v1/audio-engine` (the socket URL plus a token that changes on every
 * engine restart). This client reads that route, opens the engine's loopback
 * WebSocket directly, and speaks protocol v1: `{id, cmd}` lines in; `hello`,
 * `result` and `state` (about 30 a second) out. The state feed never passes
 * through Python.
 *
 * The Web Audio engine stays the one users hear by default until the null
 * tests (plan 20-05) and switch-over (20-07). The opt-in Rust engine mode
 * (`rust-mode.svelte.ts`, `?engine=rust`) plays through this client, and agents
 * and the shadow-engine tests can use it with the page's own command vocabulary.
 *
 * Kept free of `$lib` imports and browser globals: `fetch`, `WebSocket` and the
 * clock are passed in, so the unit tests drive it with stand-ins.
 */

/** What `GET /api/v1/audio-engine` returns (`apps/engine_core/audio_engine_api.py`). */
export interface AudioEngineStatus {
	state: 'off' | 'starting' | 'running' | 'restarting' | 'stopped' | 'failed' | 'unavailable';
	clock: string | null;
	ws_url: string | null;
	token: string | null;
	protocol: number | null;
	generation: number;
	error: string | null;
}

export interface EngineDeckState {
	deck: number;
	loaded: boolean;
	playing: boolean;
	position_ms: number;
	duration_ms: number;
	/** Track ms per engine ms while playing, 0 when stopped. */
	rate: number;
	tempo: number;
	cue_ms: number | null;
	/** The engaged loop, or null. */
	loop: { in_ms: number; out_ms: number } | null;
}

export interface EngineState {
	type: 'state';
	frame: number;
	sample_rate: number;
	engine_time_ns: number;
	decks: EngineDeckState[];
	mixer: { crossfader: number; master_volume: number };
	master: { muted: boolean };
}

export interface EngineResult {
	type: 'result';
	id: string | null;
	ok: boolean;
	error?: { code: string; message: string };
}

/** A command in the engine's vocabulary: the audio subset of `PerformanceCommand`. */
export type EngineCommand = { type: string } & Record<string, unknown>;

export const PROTOCOL_VERSION = 1;
export const AUDIO_ENGINE_PATH = '/api/v1/audio-engine';

/** Thrown when the engine refuses a command; `code` is the engine's reason. */
export class EngineCommandError extends Error {
	constructor(
		readonly code: string,
		message: string
	) {
		super(message);
		this.name = 'EngineCommandError';
	}
}

/** Why the client cannot reach the engine right now. */
export class EngineUnreachableError extends Error {
	constructor(
		readonly state: AudioEngineStatus['state'] | 'http_error' | 'closed',
		message: string
	) {
		super(message);
		this.name = 'EngineUnreachableError';
	}
}

type FetchLike = (url: string) => Promise<{ ok: boolean; status: number; json(): Promise<unknown> }>;

/** The subset of the browser `WebSocket` this client uses. */
export interface SocketLike {
	onopen: ((ev: unknown) => void) | null;
	onmessage: ((ev: { data: unknown }) => void) | null;
	onclose: ((ev: unknown) => void) | null;
	onerror: ((ev: unknown) => void) | null;
	send(data: string): void;
	close(): void;
}

/** Read the origin route. Resolves to the URL to open, token included. */
export async function resolveEngineSocket(
	fetchFn: FetchLike,
	apiBase = ''
): Promise<{ url: string; generation: number }> {
	const res = await fetchFn(`${apiBase}${AUDIO_ENGINE_PATH}`);
	if (!res.ok) {
		throw new EngineUnreachableError('http_error', `${AUDIO_ENGINE_PATH} answered ${res.status}`);
	}
	const s = (await res.json()) as AudioEngineStatus;
	if (s.state !== 'running' || !s.ws_url || !s.token) {
		const why = s.error ? `: ${s.error}` : '';
		throw new EngineUnreachableError(s.state, `the audio engine is ${s.state}${why}`);
	}
	if (s.protocol !== PROTOCOL_VERSION) {
		throw new EngineUnreachableError(
			'failed',
			`the audio engine speaks protocol ${s.protocol}; this page speaks ${PROTOCOL_VERSION}`
		);
	}
	return { url: `${s.ws_url}?token=${encodeURIComponent(s.token)}`, generation: s.generation };
}

/**
 * Where a deck's playhead is now, extrapolated from the engine's last report.
 *
 * Only ever from an acknowledged report (`docs/architecture.md`, "The transport
 * clock invariant"): a command the page just sent does not move the prediction
 * until a state message shows the engine applied it. Clamped to the track.
 */
export function predictPositionMs(
	deck: EngineDeckState,
	receivedAtMs: number,
	nowMs: number
): number {
	if (!deck.loaded) return 0;
	const elapsed = Math.max(0, nowMs - receivedAtMs);
	const p = deck.position_ms + deck.rate * elapsed;
	return Math.min(Math.max(p, 0), deck.duration_ms);
}

export interface AudioEngineClientOptions {
	fetch: FetchLike;
	openSocket: (url: string) => SocketLike;
	now: () => number;
	apiBase?: string;
	/** Longest a command waits for its result. */
	commandTimeoutMs?: number;
	setTimer?: (fn: () => void, ms: number) => unknown;
	clearTimer?: (handle: unknown) => void;
}

interface Pending {
	resolve: (r: EngineResult) => void;
	reject: (e: Error) => void;
	timer: unknown;
}

/**
 * One connection to the engine. `connect()` resolves on the engine's hello.
 * `send()` resolves with the command's result, or rejects with the engine's
 * refusal code; it never resolves on a refusal. When the socket closes, every
 * waiting command rejects and `connected` goes false; call `connect()` again,
 * which re-reads the route, because a restarted engine has a new token.
 */
export class AudioEngineClient {
	state: EngineState | null = null;
	/** `now()` when `state` arrived, for `predictPositionMs`. */
	stateReceivedAt = 0;
	connected = false;
	generation = 0;

	private socket: SocketLike | null = null;
	private nextId = 1;
	private pending = new Map<string, Pending>();
	private listeners = new Set<(s: EngineState) => void>();
	private readonly opts: Required<Omit<AudioEngineClientOptions, 'apiBase'>> & { apiBase: string };

	constructor(opts: AudioEngineClientOptions) {
		this.opts = {
			apiBase: '',
			commandTimeoutMs: 5000,
			setTimer: (fn, ms) => setTimeout(fn, ms),
			clearTimer: (h) => clearTimeout(h as ReturnType<typeof setTimeout>),
			...opts
		};
	}

	async connect(): Promise<void> {
		this.close();
		const { url, generation } = await resolveEngineSocket(this.opts.fetch, this.opts.apiBase);
		const socket = this.opts.openSocket(url);
		this.socket = socket;
		this.generation = generation;
		await new Promise<void>((resolve, reject) => {
			let greeted = false;
			socket.onmessage = (ev) => {
				const msg = parse(ev.data);
				if (!msg) return;
				if (!greeted) {
					if (msg.type !== 'hello' || msg.protocol !== PROTOCOL_VERSION) {
						reject(new EngineUnreachableError('failed', `expected a v1 hello, got ${String(ev.data)}`));
						socket.close();
						return;
					}
					greeted = true;
					this.connected = true;
					resolve();
					return;
				}
				this.onMessage(msg);
			};
			socket.onclose = () => {
				this.onClosed(socket);
				if (!greeted) reject(new EngineUnreachableError('closed', 'the engine closed the socket before its hello'));
			};
			socket.onerror = () => {};
		});
	}

	/** Send one command and wait for its result. */
	send(cmd: EngineCommand): Promise<EngineResult> {
		const socket = this.socket;
		if (!socket || !this.connected) {
			return Promise.reject(new EngineUnreachableError('closed', 'not connected to the audio engine'));
		}
		const id = `p${this.nextId++}`;
		return new Promise<EngineResult>((resolve, reject) => {
			const timer = this.opts.setTimer(() => {
				this.pending.delete(id);
				reject(new EngineUnreachableError('closed', `no result for ${cmd.type} in ${this.opts.commandTimeoutMs} ms`));
			}, this.opts.commandTimeoutMs);
			this.pending.set(id, { resolve, reject, timer });
			socket.send(JSON.stringify({ id, cmd }));
		});
	}

	/** Called with every state message. Returns an unsubscribe function. */
	onState(fn: (s: EngineState) => void): () => void {
		this.listeners.add(fn);
		return () => this.listeners.delete(fn);
	}

	/** A deck's playhead now, from the last state message. Null before one arrives. */
	positionMs(deck: number): number | null {
		const d = this.state?.decks.find((x) => x.deck === deck);
		return d ? predictPositionMs(d, this.stateReceivedAt, this.opts.now()) : null;
	}

	close(): void {
		const s = this.socket;
		if (s) {
			s.close();
			this.onClosed(s);
		}
	}

	private onMessage(msg: Record<string, unknown>): void {
		if (msg.type === 'state') {
			this.state = msg as unknown as EngineState;
			this.stateReceivedAt = this.opts.now();
			for (const fn of this.listeners) fn(this.state);
			return;
		}
		if (msg.type === 'result') {
			const r = msg as unknown as EngineResult;
			const p = r.id === null ? undefined : this.pending.get(r.id);
			if (!p) return;
			this.pending.delete(r.id as string);
			this.opts.clearTimer(p.timer);
			if (r.ok) p.resolve(r);
			else p.reject(new EngineCommandError(r.error?.code ?? 'unknown', r.error?.message ?? 'refused'));
		}
	}

	private onClosed(socket: SocketLike): void {
		if (this.socket !== socket) return;
		this.socket = null;
		this.connected = false;
		for (const [id, p] of this.pending) {
			this.opts.clearTimer(p.timer);
			p.reject(new EngineUnreachableError('closed', 'the audio engine socket closed'));
			this.pending.delete(id);
		}
	}
}

function parse(data: unknown): Record<string, unknown> | null {
	if (typeof data !== 'string') return null;
	try {
		const v = JSON.parse(data);
		return v && typeof v === 'object' ? (v as Record<string, unknown>) : null;
	} catch {
		return null;
	}
}
