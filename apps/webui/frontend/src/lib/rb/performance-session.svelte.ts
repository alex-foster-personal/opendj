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
	dispatchPerformanceCommand,
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
import type { StemControl } from '$lib/rb/stem-types';
import { decodeBeatStamp } from '$lib/rb/rescue-beat-stamp';
import type { RescueSnapshot } from '$lib/rb/rescue-snapshot';
import { API_BASE } from '$lib/api';
import { pushToast } from '$lib/stores.svelte';

/** AC allows <=30s; 10s is the ship value for crash insurance between refreshes. */
export const SESSION_SNAPSHOT_THROTTLE_MS = 10_000;
/** RESCUE-01: simultaneous play restore only within ten minutes of capture. */
export const RESCUE_RESTORE_MAX_AGE_MS = 600_000;

const DECK_IDS: DeckId[] = [1, 2, 3, 4];
const STEM_CONTROLS: StemControl[] = ['vocal', 'instrumental', 'drums'];

export interface PerformanceSessionRestoreOptions {
	now?: () => number;
	storage?: Storage;
	location?: { pathname: string; search: string; href: string };
	replaceState?: (url: string) => void;
	dispatch?: typeof dispatchPerformanceCommand;
	query?: typeof queryPerformanceState;
	fetchRescue?: (url: string) => Promise<RescueSnapshot | null>;
	document?: Pick<Document, 'hidden' | 'addEventListener' | 'removeEventListener'>;
	window?: Pick<Window, 'addEventListener' | 'removeEventListener'>;
	setInterval?: typeof globalThis.setInterval;
	clearInterval?: typeof globalThis.clearInterval;
}

function _snapshotInputFromState(
	state: PerformanceState,
	captured_at_ms: number
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
			key_sync_enabled: deck.key_sync_enabled
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
		const deckStems = {} as Record<StemControl, { muted: boolean; solo: boolean; gain: number }>;
		for (const stem of STEM_CONTROLS) {
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
		decks,
		mixer: {
			crossfader: state.mixer.crossfader,
			master: state.mixer.master,
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
	captured_at_ms: number
): string {
	return serializePerformanceSession(_snapshotInputFromState(state, captured_at_ms));
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
		const now = opts.now();
		if (!force && now - lastWriteAt < throttle_ms) return;
		const serialized = buildPerformanceSessionSnapshot(opts.query(), now);
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
	deckId: DeckId,
	stable_id: string,
	position_ms: number,
	snapshot: PerformanceSessionSnapshot | null
): Promise<void> {
	try {
		await dispatch({ type: 'load', deck: deckId, stable_id });
		if (position_ms > 0) {
			await dispatch({ type: 'seek', deck: deckId, position_ms });
		}
		if (snapshot === null) return;
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
		for (const stem of STEM_CONTROLS) {
			const control = snapshot.stems[deckId][stem];
			commands.push({ type: 'stem_mute', deck: deckId, stem, muted: control.muted });
			commands.push({ type: 'stem_solo', deck: deckId, stem, solo: control.solo });
			if (control.gain !== undefined) {
				commands.push({ type: 'stem_gain', deck: deckId, stem, value: control.gain });
			}
		}
		for (const command of commands) {
			await dispatch(command);
		}
	} catch (exc) {
		const message = exc instanceof Error ? exc.message : String(exc);
		pushToast(`session restore deck ${deckId} failed: ${message}`, 'error');
	}
}

async function _fetchLatestRescueSnapshot(
	fetchRescue: (url: string) => Promise<RescueSnapshot | null>
): Promise<RescueSnapshot | null> {
	return fetchRescue(`${API_BASE}/api/v1/performance/rescue-snapshots/latest`);
}

function _rescueSeekMs(
	query: typeof queryPerformanceState,
	deckId: DeckId,
	rescueDeck: RescueSnapshot['decks'][DeckId]
): number {
	const beats =
		query().decks[deckId].beatgrid.map((beat) => ({ t: beat.time_ms / 1000 })) ?? [];
	const decoded = decodeBeatStamp(rescueDeck.beat_stamp, beats);
	return decoded ?? rescueDeck.position_ms;
}

async function _restoreFromRescueSnapshot(
	dispatch: typeof dispatchPerformanceCommand,
	query: typeof queryPerformanceState,
	snapshot: RescueSnapshot,
	now: number
): Promise<void> {
	const ageMs = now - snapshot.captured_at_ms;
	const restorePlaying = ageMs >= 0 && ageMs <= RESCUE_RESTORE_MAX_AGE_MS;
	await dispatch({ type: 'crossfader', value: snapshot.mixer.crossfader });
	await dispatch({ type: 'master_volume', value: snapshot.mixer.master });
	const playingDecks: DeckId[] = [];
	for (const deckId of DECK_IDS) {
		const deck = snapshot.decks[deckId];
		if (deck.stable_id === null || deck.stable_id.length === 0) continue;
		try {
			await dispatch({ type: 'load', deck: deckId, stable_id: deck.stable_id });
			const seekMs = _rescueSeekMs(query, deckId, deck);
			if (seekMs > 0) await dispatch({ type: 'seek', deck: deckId, position_ms: seekMs });
			const channel = deck.mixer_channel;
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
				{ type: 'channel_cue', deck: deckId, enabled: channel.cue_enabled }
			];
			for (const stem of STEM_CONTROLS) {
				const control = deck.stems[stem];
				commands.push({ type: 'stem_mute', deck: deckId, stem, muted: control.muted });
				commands.push({ type: 'stem_solo', deck: deckId, stem, solo: control.solo });
				commands.push({ type: 'stem_gain', deck: deckId, stem, value: control.gain });
			}
			for (const command of commands) {
				await dispatch(command);
			}
			if (restorePlaying && deck.playing) playingDecks.push(deckId);
		} catch (exc) {
			const message = exc instanceof Error ? exc.message : String(exc);
			pushToast(`rescue restore deck ${deckId} failed: ${message}`, 'error');
		}
	}
	if (playingDecks.length > 0) {
		await Promise.all(
			playingDecks.map((deckId) => dispatch({ type: 'play', deck: deckId, playing: true }))
		);
		pushToast('rescue restored playing decks together', 'warn');
	}
}

async function _restoreSession(
	dispatch: typeof dispatchPerformanceCommand,
	snapshot: PerformanceSessionSnapshot | null,
	urlDeckIds: Partial<Record<DeeplinkDeckId, string>>
): Promise<void> {
	if (snapshot !== null) {
		await dispatch({ type: 'crossfader', value: snapshot.mixer.crossfader });
		await dispatch({ type: 'master_volume', value: snapshot.mixer.master });
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
		await _restoreDeck(dispatch, deckId, stable_id, position_ms, snapshot);
	}
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
	const replaceState =
		opts.replaceState ??
		((url: string) => {
			window.history.replaceState(null, '', url);
		});
	const dispatch = opts.dispatch ?? dispatchPerformanceCommand;
	const query = opts.query ?? queryPerformanceState;
	const documentRef = opts.document ?? document;
	const windowRef = opts.window ?? window;

	if (location === undefined || location.pathname !== '/performance') {
		return () => {};
	}

	const nowFn = opts.now ?? (() => Date.now());
	const fetchRescue =
		opts.fetchRescue ??
		(async (url: string): Promise<RescueSnapshot | null> => {
			try {
				const response = await fetch(url);
				if (!response.ok) return null;
				const body = (await response.json()) as RescueSnapshot;
				if (body?.schema !== 1 || body.app_posture !== 'gig') return null;
				return body;
			} catch {
				return null;
			}
		});

	const snapshot = parsePerformanceSession(storage.getItem(PERFORMANCE_SESSION_STORAGE_KEY));
	const urlDeckIds = parseLv2Ids(location.search ?? '');
	let writer: SessionSnapshotWriter | null = null;
	activeSessionWriter = null;

	const restore = async (): Promise<void> => {
		const now = nowFn();
		const rescue = await _fetchLatestRescueSnapshot(fetchRescue);
		const rescueFresh =
			rescue !== null &&
			now - rescue.captured_at_ms >= 0 &&
			now - rescue.captured_at_ms <= RESCUE_RESTORE_MAX_AGE_MS;
		const localFresh =
			snapshot !== null &&
			now - snapshot.captured_at_ms >= 0 &&
			now - snapshot.captured_at_ms <= RESCUE_RESTORE_MAX_AGE_MS;
		const preferRescue =
			rescueFresh &&
			(!localFresh ||
				(snapshot !== null ? rescue.captured_at_ms > snapshot.captured_at_ms : true));
		if (preferRescue && rescue !== null) {
			await _restoreFromRescueSnapshot(dispatch, query, rescue, now);
			return;
		}
		await _restoreSession(dispatch, snapshot, urlDeckIds);
	};

	void restore().finally(() => {
		writer = createSessionSnapshotWriter({
			now: nowFn,
			storage,
			location,
			replaceState,
			query,
			document: documentRef,
			window: windowRef,
			setInterval: opts.setInterval,
			clearInterval: opts.clearInterval
		});
		writer.flush(true);
		activeSessionWriter = writer;
	});

	return () => {
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
	const now = Date.now();
	const serialized = buildPerformanceSessionSnapshot(queryPerformanceState(), now);
	window.localStorage.setItem(PERFORMANCE_SESSION_STORAGE_KEY, serialized);
}
