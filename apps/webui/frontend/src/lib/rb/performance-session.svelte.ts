/**
 * /performance session restore and throttled localStorage snapshots.
 * Main-thread only: setInterval + pagehide/visibilitychange, never audio worklet.
 */

import type { DeckId } from '$lib/rb/deck-id';
import {
	formatReplaceStateUrl,
	parseLv2Ids,
	writeLv2Ids,
	type DeckId as DeeplinkDeckId
} from '$lib/rb/performance-deeplink';
import {
	activePerformanceCommandSession,
	dispatchPerformanceCommand,
	operatorMasterVolume,
	queryPerformanceState,
	type PerformanceCommand,
	type PerformanceState
} from '$lib/rb/performance-ipc.svelte';
import {
	PERFORMANCE_SESSION_STORAGE_KEY,
	parsePerformanceSession,
	serializePerformanceSession,
	type PerformanceSessionSnapshot,
	type PerformanceSessionSnapshotInput
} from '$lib/rb/performance-session-snapshot';
import { STEM_CONTROL_IDS } from '$lib/rb/stem-types';
import { restoreStemControls } from '$lib/rb/stem-restore';
import {
	consumeLibraryModeExitFlag,
	shouldSkipPerformanceSessionRestore
} from '$lib/rb/library-mode-runtime';
import { pushToast } from '$lib/stores.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { planReloadResumeOffer } from '$lib/rb/reload-resume';
import { offerReloadResume } from '$lib/rb/reload-resume.svelte';

/** AC allows <=30s; 10s is the ship value for crash insurance between refreshes. */
export const SESSION_SNAPSHOT_THROTTLE_MS = 10_000;
/** RESCUE-01: simultaneous play restore only within ten minutes of capture. */
export const RESCUE_RESTORE_MAX_AGE_MS = 600_000;

const DECK_IDS: DeckId[] = [1, 2, 3, 4];
const STEM_CONTROLS = STEM_CONTROL_IDS;

export interface PerformanceSessionRestoreOptions {
	now?: () => number;
	storage?: Storage;
	location?: { pathname: string; search: string; href: string };
	replaceState?: (url: string) => void;
	dispatch?: typeof dispatchPerformanceCommand;
	query?: typeof queryPerformanceState;
	document?: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>;
	window?: Pick<Window, 'addEventListener' | 'removeEventListener'>;
	setInterval?: typeof globalThis.setInterval;
	clearInterval?: typeof globalThis.clearInterval;
	skipDeckRestore?: boolean;
	/** The route command session this restore belongs to; the snapshot
	 * writer stops when it ends. Defaults to the live performance IPC. */
	commandSession?: () => number | null;
	operatorMaster?: () => number | null;
	/** RESCUE-07: whether AutoPlay is on, for the reload-resume log line. */
	autoPlayEnabled?: () => boolean;
}

function _snapshotInputFromState(
	state: PerformanceState,
	captured_at_ms: number,
	operatorMaster: number | null
): PerformanceSessionSnapshotInput {
	const playlist_id = state.browser.active_playlist;
	const decks = {} as PerformanceSessionSnapshotInput['decks'];
	const channels = {} as PerformanceSessionSnapshotInput['mixer']['channels'];
	const stems = {} as PerformanceSessionSnapshotInput['stems'];

	for (const deckId of DECK_IDS) {
		const deck = state.decks[deckId];
		decks[deckId] = {
			stable_id: deck.stable_id,
			position_ms: deck.position_ms,
			pitch: deck.pitch,
			pitch_range: deck.pitch_range,
			quantize_enabled: deck.quantize_enabled,
			beat_sync_enabled: deck.beat_sync_enabled,
			master_tempo_enabled: deck.master_tempo_enabled,
			key_sync_enabled: deck.key_sync_enabled,
			playing: deck.playing
		};
		const channel = state.mixer.channels[deckId];
		channels[deckId] = {
			trim: channel.trim,
			eq_high: channel.eq_high,
			eq_mid: channel.eq_mid,
			eq_low: channel.eq_low,
			filter: channel.filter,
			fader: channel.fader,
			assign: channel.assign,
			stem_eq_mode: channel.stem_eq_mode
		};
		const deckStems = {} as PerformanceSessionSnapshotInput['stems'][DeckId];
		for (const stem of STEM_CONTROLS) {
			if ((stem === 'bass' || stem === 'other') && !deck.stems.available_controls.includes(stem)) continue;
			deckStems[stem] = {
				muted: deck.stems.controls[stem].muted,
				solo: deck.stems.controls[stem].solo,
				gain: deck.stems.controls[stem].gain
			};
		}
		stems[deckId] = deckStems;
	}

	return {
		captured_at_ms,
		playlist_id,
		master_deck: state.master_deck,
		decks,
		mixer: {
			crossfader: state.mixer.crossfader,
			// The operator's own level, not the live gain: a safety mute that
			// zeroed the mixer must not become the level the next load restores.
			master: operatorMaster ?? state.mixer.master,
			master_set_by_operator: operatorMaster !== null,
			channels
		},
		stems
	};
}

function _deckIdsFromState(state: PerformanceState): Partial<Record<DeeplinkDeckId, string>> {
	const ids: Partial<Record<DeeplinkDeckId, string>> = {};
	for (const deckId of DECK_IDS) {
		const stable_id = state.decks[deckId].stable_id;
		if (stable_id !== null && stable_id.length > 0) ids[deckId] = stable_id;
	}
	return ids;
}

function _idsMatchQuery(
	ids: Partial<Record<DeeplinkDeckId, string>>,
	search: string
): boolean {
	const current = parseLv2Ids(search);
	for (const deckId of DECK_IDS) {
		if ((current[deckId] ?? null) !== (ids[deckId] ?? null)) return false;
	}
	return true;
}

export function buildPerformanceSessionSnapshot(
	state: PerformanceState,
	captured_at_ms: number,
	operatorMaster: number | null
): string {
	return serializePerformanceSession(_snapshotInputFromState(state, captured_at_ms, operatorMaster));
}

export interface SessionSnapshotWriter {
	flush(force?: boolean): void;
	dispose(): void;
}

/** Test seam for throttle and flush behavior without a live browser. */
export function createSessionSnapshotWriter(opts: {
	now: () => number;
	storage: Storage;
	location: { pathname: string; search: string; href: string };
	replaceState: (url: string) => void;
	query: typeof queryPerformanceState;
	/** False once the route session that owns this writer has ended. Every
	 * write is refused from then on, so a snapshot taken during teardown
	 * (hard-muted master, stopped decks) is never persisted. */
	isLive: () => boolean;
	operatorMaster: () => number | null;
	throttle_ms?: number;
	document?: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>;
	window?: Pick<Window, 'addEventListener' | 'removeEventListener'>;
	setInterval?: typeof globalThis.setInterval;
	clearInterval?: typeof globalThis.clearInterval;
}): SessionSnapshotWriter {
	const throttle_ms = opts.throttle_ms ?? SESSION_SNAPSHOT_THROTTLE_MS;
	let lastSerialized: string | null = null;
	let lastWriteAt = 0;

	const writeSnapshot = (force: boolean): void => {
		if (opts.location?.pathname !== '/performance') return;
		if (!opts.isLive()) return;
		const now = opts.now();
		if (!force && now - lastWriteAt < throttle_ms) return;
		const serialized = buildPerformanceSessionSnapshot(opts.query(), now, opts.operatorMaster());
		if (!force && serialized === lastSerialized) return;
		opts.storage.setItem(PERFORMANCE_SESSION_STORAGE_KEY, serialized);
		lastSerialized = serialized;
		lastWriteAt = now;

		const locationSearch = opts.location?.search ?? '';
		const ids = _deckIdsFromState(opts.query());
		if (opts.location && !_idsMatchQuery(ids, locationSearch)) {
			const url = new URL(opts.location.href);
			const params = writeLv2Ids(url.searchParams, ids);
			url.search = params.toString();
			opts.replaceState(formatReplaceStateUrl(url));
			// Plain-object test fakes only: assigning window.location.search navigates.
			if (typeof window === 'undefined' || opts.location !== window.location) {
				opts.location.search = url.search;
				opts.location.href = url.href;
			}
		}
	};

	const onPageHide = (): void => writeSnapshot(true);
	const onVisibilityChange = (): void => {
		if (opts.document?.hidden) writeSnapshot(true);
	};

	const setIntervalFn = opts.setInterval ?? globalThis.setInterval;
	const clearIntervalFn = opts.clearInterval ?? globalThis.clearInterval;
	const intervalId = setIntervalFn(() => writeSnapshot(false), throttle_ms);

	opts.window?.addEventListener('pagehide', onPageHide);
	opts.document?.addEventListener('visibilitychange', onVisibilityChange);

	return {
		flush: (force = true) => writeSnapshot(force),
		dispose: () => {
			clearIntervalFn(intervalId);
			opts.window?.removeEventListener('pagehide', onPageHide);
			opts.document?.removeEventListener('visibilitychange', onVisibilityChange);
			writeSnapshot(true);
		}
	};
}

async function _restoreDeck(
	dispatch: typeof dispatchPerformanceCommand,
	query: typeof queryPerformanceState,
	deckId: DeckId,
	stable_id: string,
	position_ms: number,
	snapshot: PerformanceSessionSnapshot | null,
	skipSeek = false
): Promise<void> {
	try {
		await dispatch({ type: 'load', deck: deckId, stable_id });
		if (!skipSeek && position_ms > 0) {
			await dispatch({ type: 'seek', deck: deckId, position_ms });
		}
		if (snapshot === null) return;
		await restoreDeckConfigFromSnapshot(dispatch, deckId, snapshot, query);
	} catch (exc) {
		const message = exc instanceof Error ? exc.message : String(exc);
		pushToast(`session restore deck ${deckId} failed: ${message}`, 'error');
	}
}

export async function restoreDeckConfigFromSnapshot(
	dispatch: typeof dispatchPerformanceCommand,
	deckId: DeckId,
	snapshot: PerformanceSessionSnapshot | PerformanceRescueDeckConfigSource,
	query: typeof queryPerformanceState = queryPerformanceState
): Promise<void> {
	const deck = snapshot.decks[deckId];
	const channel = snapshot.mixer.channels[deckId];
	const commands: PerformanceCommand[] = [
		{ type: 'pitch_range', deck: deckId, range: deck.pitch_range },
		{ type: 'tempo', deck: deckId, ratio: deck.pitch },
		{ type: 'quantize', deck: deckId, enabled: deck.quantize_enabled },
		{ type: 'beat_sync', deck: deckId, enabled: deck.beat_sync_enabled },
		{ type: 'master_tempo', deck: deckId, enabled: deck.master_tempo_enabled },
		{ type: 'key_sync', deck: deckId, enabled: deck.key_sync_enabled },
		{ type: 'trim', deck: deckId, value: channel.trim },
		{ type: 'eq', deck: deckId, band: 'high', value: channel.eq_high },
		{ type: 'eq', deck: deckId, band: 'mid', value: channel.eq_mid },
		{ type: 'eq', deck: deckId, band: 'low', value: channel.eq_low },
		{ type: 'filter', deck: deckId, value: channel.filter },
		{ type: 'fader', deck: deckId, value: channel.fader },
		{ type: 'assign', deck: deckId, assign: channel.assign },
		{ type: 'stem_eq_mode', deck: deckId, enabled: channel.stem_eq_mode ?? false }
	];
	for (const command of commands) {
		await dispatch(command);
	}
	await restoreStemControls(dispatch, query, deckId, snapshot.stems[deckId]);
}

type PerformanceRescueDeckConfigSource = Pick<
	PerformanceSessionSnapshot,
	'decks' | 'mixer' | 'stems'
>;

/** A master of 0 is replayed only when an operator chose it. Anything else
 * at 0 is a safety mute captured by accident, and replaying it is what left
 * the preview silent after a restart (demon-llama, Thu 1 Oct 2026 05:49:53Z). */
export function shouldRestoreSessionMaster(snapshot: PerformanceSessionSnapshot): boolean {
	return snapshot.mixer.master > 0 || snapshot.mixer.master_set_by_operator === true;
}

async function _restoreSession(
	dispatch: typeof dispatchPerformanceCommand,
	query: typeof queryPerformanceState,
	snapshot: PerformanceSessionSnapshot | null,
	urlDeckIds: Partial<Record<DeeplinkDeckId, string>>,
	skipDeckRestore: boolean,
	now: () => number = () => Date.now(),
	autoPlayEnabled: () => boolean = () => uiPrefs.auto_play_enabled
): Promise<void> {
	if (skipDeckRestore) return;
	if (snapshot !== null) {
		await dispatch({ type: 'crossfader', value: snapshot.mixer.crossfader });
		if (shouldRestoreSessionMaster(snapshot)) {
			await dispatch({ type: 'master_volume', value: snapshot.mixer.master });
		}
	}

	for (const deckId of DECK_IDS) {
		const urlId = urlDeckIds[deckId] ?? null;
		const snapshotDeck = snapshot?.decks[deckId] ?? null;
		const stable_id = urlId ?? snapshotDeck?.stable_id ?? null;
		if (stable_id === null || stable_id.length === 0) continue;
		const position_ms =
			snapshotDeck !== null && snapshotDeck.stable_id === stable_id
				? snapshotDeck.position_ms
				: 0;
		await _restoreDeck(dispatch, query, deckId, stable_id, position_ms, snapshot);
	}
	// RESCUE-07: decks that were playing come back stopped (a reload is not a
	// Gig rescue, and the browser will not start audio before a click), so say
	// so and offer the click, rather than landing silently stopped.
	offerReloadResume(
		planReloadResumeOffer({ snapshot, decks: query().decks, now_ms: now() }),
		autoPlayEnabled()
	);
}

export function installPerformanceSessionRestore(
	opts: PerformanceSessionRestoreOptions = {}
): () => void {
	const location =
		opts.location ??
		(typeof window !== 'undefined' ? window.location : undefined);

	if (location === undefined || location.pathname !== '/performance') {
		return () => {};
	}

	const storage = opts.storage ?? window.localStorage;
	// The router owns history entries, so the caller injects its helper. This
	// module is loaded by node unit tests, where the framework's virtual module
	// does not resolve, so it cannot import that helper itself.
	const replaceState = opts.replaceState;
	if (replaceState === undefined) {
		throw new TypeError('installPerformanceSessionRestore needs a replaceState from the router');
	}
	const dispatch = opts.dispatch ?? dispatchPerformanceCommand;
	const query = opts.query ?? queryPerformanceState;
	const commandSession = opts.commandSession ?? activePerformanceCommandSession;
	const operatorMaster = opts.operatorMaster ?? operatorMasterVolume;
	// Bound now, not at write time: a restore installed after its route has
	// already unmounted sees no session and never writes, and one that
	// outlives its route stops the instant the next mount starts a new one.
	const ownSession = commandSession();
	const isLive = (): boolean => ownSession !== null && commandSession() === ownSession;
	const documentRef = opts.document ?? document;
	const windowRef = opts.window ?? window;

	if (location === undefined || location.pathname !== '/performance') {
		return () => {};
	}

	const nowFn = opts.now ?? (() => Date.now());
	const snapshot = parsePerformanceSession(storage.getItem(PERFORMANCE_SESSION_STORAGE_KEY));
	const urlDeckIds = parseLv2Ids(location.search ?? '');
	let writer: SessionSnapshotWriter | null = null;
	activeSessionWriter = null;
	const skipFromLibrary = shouldSkipPerformanceSessionRestore();
	if (skipFromLibrary) consumeLibraryModeExitFlag();
	const skipDeckRestore = (opts.skipDeckRestore ?? false) || skipFromLibrary;
	let disposed = false;
	const assertActive = (): void => {
		if (disposed) throw new Error('performance session restore was disposed');
	};

	void _restoreSession(
		(command) => { assertActive(); return dispatch(command); },
		() => { assertActive(); return query(); },
		snapshot, urlDeckIds, skipDeckRestore,
		nowFn, opts.autoPlayEnabled ?? (() => uiPrefs.auto_play_enabled)
	).finally(() => {
		if (disposed) return;
		writer = createSessionSnapshotWriter({
			now: nowFn,
			storage,
			location,
			replaceState,
			query,
			isLive,
			operatorMaster,
			document: documentRef,
			window: windowRef,
			...(opts.setInterval !== undefined ? { setInterval: opts.setInterval } : {}),
			...(opts.clearInterval !== undefined ? { clearInterval: opts.clearInterval } : {})
		});
		writer.flush(true);
		activeSessionWriter = writer;
	});

	return () => {
		disposed = true;
		writer?.dispose();
		writer = null;
		activeSessionWriter = null;
	};
}

let activeSessionWriter: SessionSnapshotWriter | null = null;

/** INSTALL-21 / RESCUE-01: final session snapshot before a confirmed shell quit. */
export function flushPerformanceSessionSnapshot(): void {
	if (activeSessionWriter !== null) {
		activeSessionWriter.flush(true);
		return;
	}
	if (typeof window === 'undefined' || window.location.pathname !== '/performance') return;
	if (activePerformanceCommandSession() === null) return;
	const now = Date.now();
	const serialized = buildPerformanceSessionSnapshot(queryPerformanceState(), now, operatorMasterVolume());
	window.localStorage.setItem(PERFORMANCE_SESSION_STORAGE_KEY, serialized);
}
