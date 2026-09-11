/**
 * Dispatcher helpers for the Load-to-CHn intro blend (issue #286).
 *
 * Takes a `run` callback so this module never imports performance-ipc.
 * FILTER is a live engine write, not a stub.
 */
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { createLoadBlendSession } from '$lib/rb/load-blend-gesture';
import {
	LOAD_BLEND_INCOMING_START,
	LOAD_BLEND_MASTER_START,
	snapshotMixerChannel,
	type MixerChannelSnapshot
} from '$lib/rb/load-blend-math';
import { phraseBar1TargetMs } from '$lib/rb/load-blend-quantize';

type DeckId = 1 | 2 | 3 | 4;
type LoadBlendSession = ReturnType<typeof createLoadBlendSession>;

type LoadBlendCommand =
	| { type: 'fader'; deck: DeckId; value: number }
	| { type: 'eq'; deck: DeckId; band: 'low' | 'mid' | 'high'; value: number }
	| { type: 'filter'; deck: DeckId; value: number }
	| { type: 'channel_cue'; deck: DeckId; enabled: boolean }
	| { type: 'seek'; deck: DeckId; position_ms: number }
	| { type: 'play'; deck: DeckId; playing: boolean };

type LoadBlendRun = (cmd: LoadBlendCommand) => Promise<void>;

type LoadBlendHudState = {
	visible: boolean;
	t: number;
	fader: number;
	scrubDeltaMs: number;
};

type LoadBlendDeckRead = {
	stable_id: string | null;
	position_ms: number;
	duration_ms: number | null;
	playing: boolean;
	anlz: {
		beatgrid: { beats: readonly AnlzBeat[] };
		phrases: ReadonlyArray<{ start_s: number; end_s: number }>;
	} | null;
};

type LoadBlendControllerDeps = {
	run: LoadBlendRun;
	getDeck: (deck: DeckId) => LoadBlendDeckRead;
	getChannel: (deck: DeckId) => MixerChannelSnapshot;
	toast: (message: string, kind: 'info' | 'warn' | 'error') => void;
	hud: LoadBlendHudState;
};

export function createLoadBlendController(deps: LoadBlendControllerDeps) {
	let session: LoadBlendSession | null = null;
	let incomingSnap: MixerChannelSnapshot | null = null;
	let masterSnap: MixerChannelSnapshot | null = null;
	let loadWork: Promise<void> | null = null;
	let mixTail: Promise<void> = Promise.resolve();
	let wantPlay = false;
	let playStarted = false;
	let pointerId: number | null = null;
	let epoch = 0;
	let seek = createSeekCoalesce(deps.run, () => session?.incomingDeck ?? null);

	function chain(fn: () => Promise<void>): Promise<void> {
		const next = mixTail.then(fn, fn);
		mixTail = next.then(
			() => undefined,
			() => undefined
		);
		return next;
	}

	function isActive(): boolean {
		const phase = session?.phase;
		return phase === 'down' || phase === 'morph';
	}

	function hideHud(): void {
		deps.hud.visible = false;
	}

	function syncHud(): void {
		if (session === null || session.phase !== 'morph') {
			hideHud();
			return;
		}
		deps.hud.visible = true;
		deps.hud.t = session.t;
		deps.hud.fader = session.fader;
		deps.hud.scrubDeltaMs = session.scrubDeltaMs;
	}

	function reset(): void {
		session = null;
		incomingSnap = null;
		masterSnap = null;
		loadWork = null;
		wantPlay = false;
		playStarted = false;
		pointerId = null;
		hideHud();
	}

	function down(
		x: number,
		y: number,
		incomingDeck: DeckId,
		stableId: string,
		masterDeck: DeckId | null,
		id: number,
		load: () => Promise<void>
	): void {
		session = createLoadBlendSession({ incomingDeck, stableId, masterDeck });
		session.pointerDown(x, y);
		incomingSnap = snapshotMixerChannel(deps.getChannel(incomingDeck));
		masterSnap = masterDeck === null ? null : snapshotMixerChannel(deps.getChannel(masterDeck));
		pointerId = id;
		wantPlay = false;
		playStarted = false;
		epoch += 1;
		loadWork = load();
		mixTail = Promise.resolve();
		seek = createSeekCoalesce(deps.run, () => session?.incomingDeck ?? null);
	}

	function samePointer(id: number): boolean {
		return pointerId !== null && pointerId === id;
	}

	function move(x: number, y: number, id: number): void {
		if (session === null || !samePointer(id)) return;
		const result = session.pointerMove(x, y);
		if (result.kind === 'refuse') {
			deps.toast(`Load-to-CH blend needs a MASTER deck (${result.reason})`, 'warn');
			return;
		}
		if (result.kind !== 'morph') return;
		const incoming = session.incomingDeck;
		const master = session.masterDeck;
		if (master === null) return;
		session.setDurationMs(deps.getDeck(incoming).duration_ms ?? 0);
		const live = session;
		const my = epoch;
		if (result.entered) {
			session.setScrubOrigin(deps.getDeck(incoming).position_ms);
			wantPlay = true;
			void chain(async () => {
				if (epoch !== my || session !== live) return;
				await applyIntro(deps.run, incoming, master);
			});
			void playWhenReady(my, live);
		}
		void chain(async () => {
			if (epoch !== my || session !== live) return;
			await applySample(deps.run, live, master, seek);
			if (epoch === my && session === live) syncHud();
		});
	}

	function up(id: number): void {
		if (session === null || !samePointer(id)) return;
		const result = session.pointerUp();
		hideHud();
		pointerId = null;
		if (result.kind === 'morph') {
			const live = session;
			const my = epoch;
			void finishMorph(live, my);
		} else {
			reset();
		}
	}

	function abort(): void {
		if (session === null) return;
		const result = session.abort();
		hideHud();
		pointerId = null;
		wantPlay = false;
		epoch += 1;
		if (result.kind === 'restore') {
			void restoreOrToast(session);
		} else {
			reset();
		}
	}

	function lostCapture(id: number): void {
		if (!isActive() || !samePointer(id)) return;
		abort();
	}

	async function playWhenReady(my: number, live: LoadBlendSession): Promise<void> {
		if (loadWork !== null) await loadWork;
		if (epoch !== my || !wantPlay || session !== live) return;
		const deck = deps.getDeck(live.incomingDeck);
		if (deck.stable_id !== live.stableId) return;
		if (!deck.playing) {
			await deps.run({ type: 'play', deck: live.incomingDeck, playing: true });
			if (epoch !== my || session !== live || !wantPlay) {
				await deps.run({ type: 'play', deck: live.incomingDeck, playing: false });
				return;
			}
			playStarted = true;
		}
	}

	async function finishMorph(live: LoadBlendSession, my: number): Promise<void> {
		try {
			await chain(async () => {
				if (loadWork !== null) await loadWork;
				if (epoch !== my || session !== live) return;
				await quantizeBoth(deps, live);
			});
		} finally {
			if (session === live) reset();
		}
	}

	async function restoreOrToast(live: LoadBlendSession): Promise<void> {
		try {
			await chain(async () => {
				await restoreSnapshot(deps.run, live, incomingSnap, masterSnap, playStarted);
			});
		} catch (error) {
			deps.toast(restoreFailedMessage(live, incomingSnap, masterSnap, error), 'error');
		} finally {
			if (session === live) reset();
		}
	}

	return { isActive, down, move, up, abort, lostCapture };
}

async function applyIntro(run: LoadBlendRun, incoming: DeckId, master: DeckId): Promise<void> {
	await run({ type: 'fader', deck: incoming, value: LOAD_BLEND_INCOMING_START.fader });
	await run({ type: 'eq', deck: incoming, band: 'low', value: LOAD_BLEND_INCOMING_START.low });
	await run({ type: 'eq', deck: incoming, band: 'mid', value: LOAD_BLEND_INCOMING_START.mid });
	await run({ type: 'filter', deck: incoming, value: LOAD_BLEND_INCOMING_START.filter });
	await run({ type: 'channel_cue', deck: incoming, enabled: true });
	await run({ type: 'eq', deck: master, band: 'low', value: LOAD_BLEND_MASTER_START.low });
	await run({ type: 'eq', deck: master, band: 'mid', value: LOAD_BLEND_MASTER_START.mid });
	await run({ type: 'channel_cue', deck: master, enabled: false });
}

async function applySample(
	run: LoadBlendRun,
	session: LoadBlendSession,
	master: DeckId,
	seek: { request: (positionMs: number) => Promise<void> }
): Promise<void> {
	const incoming = session.incomingDeck;
	await run({ type: 'fader', deck: incoming, value: session.fader });
	await run({ type: 'eq', deck: incoming, band: 'low', value: session.eq.incoming.low });
	await run({ type: 'eq', deck: incoming, band: 'mid', value: session.eq.incoming.mid });
	await run({ type: 'filter', deck: incoming, value: session.eq.incoming.filter });
	await run({ type: 'eq', deck: master, band: 'low', value: session.eq.master.low });
	await run({ type: 'eq', deck: master, band: 'mid', value: session.eq.master.mid });
	await seek.request(session.positionMs);
}

async function quantizeBoth(
	deps: LoadBlendControllerDeps,
	session: LoadBlendSession
): Promise<void> {
	const master = session.masterDeck;
	await snapDeck(deps, session, session.incomingDeck);
	if (master !== null) await snapDeck(deps, session, master);
}

function createSeekCoalesce(
	run: LoadBlendRun,
	deckOf: () => DeckId | null
): { request: (positionMs: number) => Promise<void> } {
	let queued: number | null = null;
	let drain: Promise<void> | null = null;
	return {
		request(positionMs: number): Promise<void> {
			queued = positionMs;
			if (drain === null) {
				drain = (async () => {
					try {
						while (queued !== null) {
							const ms = queued;
							queued = null;
							const deck = deckOf();
							if (deck === null) return;
							await run({ type: 'seek', deck, position_ms: ms });
						}
					} finally {
						drain = null;
					}
				})();
			}
			return drain;
		}
	};
}

async function snapDeck(
	deps: LoadBlendControllerDeps,
	session: LoadBlendSession,
	deck: DeckId
): Promise<void> {
	const read = deps.getDeck(deck);
	const positionMs = deck === session.incomingDeck ? session.positionMs : read.position_ms;
	const target = phraseBar1TargetMs({
		positionMs,
		beats: read.anlz?.beatgrid.beats ?? null,
		phrases: (read.anlz?.phrases ?? []).map((phrase) => ({
			start_ms: phrase.start_s * 1000,
			end_ms: phrase.end_s * 1000
		}))
	});
	if (target === null) {
		deps.toast(`CH${deck} has no beatgrid or phrase grid to snap`, 'warn');
		return;
	}
	await deps.run({ type: 'seek', deck, position_ms: target });
}

async function restoreSnapshot(
	run: LoadBlendRun,
	session: LoadBlendSession,
	incomingSnap: MixerChannelSnapshot | null,
	masterSnap: MixerChannelSnapshot | null,
	playStarted: boolean
): Promise<void> {
	if (incomingSnap !== null) await restoreChannel(run, session.incomingDeck, incomingSnap);
	if (masterSnap !== null && session.masterDeck !== null) {
		await restoreChannel(run, session.masterDeck, masterSnap);
	}
	if (playStarted) {
		await run({ type: 'play', deck: session.incomingDeck, playing: false });
	}
}

async function restoreChannel(
	run: LoadBlendRun,
	deck: DeckId,
	snap: MixerChannelSnapshot
): Promise<void> {
	await run({ type: 'fader', deck, value: snap.fader });
	await run({ type: 'eq', deck, band: 'low', value: snap.eq_low });
	await run({ type: 'eq', deck, band: 'mid', value: snap.eq_mid });
	await run({ type: 'eq', deck, band: 'high', value: snap.eq_high });
	await run({ type: 'filter', deck, value: snap.filter });
	await run({ type: 'channel_cue', deck, enabled: snap.cue_enabled });
}

function restoreFailedMessage(
	session: LoadBlendSession,
	incomingSnap: MixerChannelSnapshot | null,
	masterSnap: MixerChannelSnapshot | null,
	error: unknown
): string {
	const detail = error instanceof Error ? error.message : String(error);
	const incoming = formatSnap(session.incomingDeck, incomingSnap);
	const master =
		session.masterDeck === null ? 'master none' : formatSnap(session.masterDeck, masterSnap);
	return `CH${session.incomingDeck} fader/EQ restore failed: ${incoming}; ${master}: ${detail}`;
}

function formatSnap(deck: DeckId, snap: MixerChannelSnapshot | null): string {
	if (snap === null) return `CH${deck} (no snapshot)`;
	return (
		`CH${deck} fader=${snap.fader} low=${snap.eq_low} mid=${snap.eq_mid} ` +
		`high=${snap.eq_high} filter=${snap.filter} cue=${snap.cue_enabled}`
	);
}
