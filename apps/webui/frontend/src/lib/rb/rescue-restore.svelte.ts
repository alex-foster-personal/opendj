/**
 * Gig rescue restore orchestration: layout-only beyond 10 min, play within 10 min.
 */

import type { DeckId } from '$lib/rb/deck-id';
import {
	dispatchPerformanceCommand,
	queryPerformanceState,
	type PerformanceCommand,
	type PerformanceState
} from '$lib/rb/performance-ipc.svelte';
import { planRescueSimultaneousPlay } from '$lib/rb/performance-rescue-play';
import { decodeBeatStamp } from '$lib/rb/rescue-beat-stamp';
import {
	RESCUE_LAYOUT_WINDOW_MS,
	RESCUE_PLAY_WINDOW_MS,
	parseRescueSnapshot,
	type RescueSnapshot
} from '$lib/rb/rescue-snapshot';
import { engine } from '$lib/rb/audio-engine.svelte';
import { uiPrefs } from '$lib/rb/prefs.svelte';
import { pushToast } from '$lib/stores.svelte';
import { API_BASE } from '$lib/api';

const DECK_IDS: DeckId[] = [1, 2, 3, 4];
import { STEM_CONTROL_IDS as STEM_CONTROLS } from '$lib/rb/stem-types';

export type RescueDeckOutcome = 'resumed' | 'paused' | 'missing';

export interface RescueRestorePlan {
	snapshot: RescueSnapshot;
	mode: 'layout' | 'play';
}

export interface RescueRestoreResult {
	snapshot_id: string;
	captured_at_ms: number;
	mode: 'layout' | 'play';
	decks: Record<string, { outcome: RescueDeckOutcome; stable_id: string | null }>;
}

export interface PerformanceRescueOptions {
	now?: () => number;
	fetch?: typeof globalThis.fetch;
	dispatch?: typeof dispatchPerformanceCommand;
	query?: typeof queryPerformanceState;
	location?: { pathname: string };
}

export function formatRescueAgeToast(captured_at_ms: number, nowMs: number): string {
	const ageMs = Math.max(0, nowMs - captured_at_ms);
	const hours = Math.floor(ageMs / (60 * 60 * 1000));
	if (hours >= 1) {
		return `Restored layout from ${hours} h ago`;
	}
	const minutes = Math.max(1, Math.floor(ageMs / (60 * 1000)));
	if (minutes === 1) {
		return 'Restored layout from 1 min ago';
	}
	return `Restored layout from ${minutes} min ago`;
}

function _rescueSeekMs(
	query: typeof queryPerformanceState,
	deckId: DeckId,
	snapshot: RescueSnapshot
): number {
	const deck = snapshot.decks[deckId];
	const beats =
		query().decks[deckId].beatgrid.map((beat) => ({ t: beat.time_ms / 1000 })) ?? [];
	const decoded = decodeBeatStamp(deck.beat_stamp, beats);
	if (decoded !== null) return decoded;
	return deck.cue_ms ?? deck.position_ms;
}

async function _restoreRescueDeckConfig(
	dispatch: typeof dispatchPerformanceCommand,
	deckId: DeckId,
	snapshot: RescueSnapshot
): Promise<void> {
	const deck = snapshot.decks[deckId];
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
		if (control === undefined) continue; // legacy snapshot: child controls remain neutral
		commands.push({ type: 'stem_mute', deck: deckId, stem, muted: control.muted });
		commands.push({ type: 'stem_solo', deck: deckId, stem, solo: control.solo });
		commands.push({ type: 'stem_gain', deck: deckId, stem, value: control.gain });
	}
	for (const command of commands) {
		await dispatch(command);
	}
}

async function _restoreSinks(
	dispatch: typeof dispatchPerformanceCommand,
	snapshot: RescueSnapshot
): Promise<void> {
	const headphones = snapshot.mixer.headphones;
	await dispatch({
		type: 'output_mode',
		mode: headphones.output_mode
	});
	if (headphones.selected_output_device_id !== null) {
		await dispatch({
			type: 'headphone_output_select',
			device_id: headphones.selected_output_device_id
		});
	}
	if (headphones.selected_master_output_device_id !== null) {
		await dispatch({
			type: 'headphone_master_select',
			device_id: headphones.selected_master_output_device_id
		});
	}
}

async function _restoreLayoutOnlyDeck(
	dispatch: typeof dispatchPerformanceCommand,
	query: typeof queryPerformanceState,
	deckId: DeckId,
	snapshot: RescueSnapshot
): Promise<'paused' | 'missing'> {
	const deck = snapshot.decks[deckId];
	const stable_id = deck.stable_id;
	if (stable_id === null || stable_id.length === 0) return 'paused';
	try {
		await dispatch({ type: 'load', deck: deckId, stable_id });
		const seekMs = _rescueSeekMs(query, deckId, snapshot);
		if (seekMs > 0) {
			await dispatch({ type: 'seek', deck: deckId, position_ms: seekMs });
		}
		await _restoreRescueDeckConfig(dispatch, deckId, snapshot);
		return 'paused';
	} catch {
		return 'missing';
	}
}

export async function executeRescueRestore(
	plan: RescueRestorePlan,
	opts: {
		dispatch?: typeof dispatchPerformanceCommand;
		query?: typeof queryPerformanceState;
		audioContextTime?: () => number;
	} = {}
): Promise<RescueRestoreResult> {
	const dispatch = opts.dispatch ?? dispatchPerformanceCommand;
	const query = opts.query ?? queryPerformanceState;
	const snapshot = plan.snapshot;
	const outcomes: RescueRestoreResult['decks'] = {};

	await dispatch({ type: 'crossfader', value: snapshot.mixer.crossfader });
	await dispatch({ type: 'master_volume', value: snapshot.mixer.master });
	if (snapshot.deck_layout !== undefined) {
		uiPrefs.deck_layout = snapshot.deck_layout;
	}

	for (const deckId of DECK_IDS) {
		const outcome = await _restoreLayoutOnlyDeck(dispatch, query, deckId, snapshot);
		outcomes[String(deckId)] = {
			outcome,
			stable_id: snapshot.decks[deckId].stable_id
		};
	}
	await _restoreSinks(dispatch, snapshot);

	if (plan.mode === 'layout') {
		for (const deckId of DECK_IDS) {
			if (outcomes[String(deckId)].outcome === 'missing') continue;
			await dispatch({ type: 'play', deck: deckId, playing: false });
			outcomes[String(deckId)] = {
				outcome: 'paused',
				stable_id: snapshot.decks[deckId].stable_id
			};
		}
		return {
			snapshot_id: 'ui',
			captured_at_ms: snapshot.captured_at_ms,
			mode: plan.mode,
			decks: outcomes
		};
	}

	const playTargets = DECK_IDS.filter(
		(deckId) => snapshot.decks[deckId].playing && outcomes[String(deckId)].outcome !== 'missing'
	);
	const contextTime =
		opts.audioContextTime?.() ??
		(typeof engine.contextTimeNowSec === 'function' ? engine.contextTimeNowSec() : 0);
	const schedule = planRescueSimultaneousPlay(
		playTargets.map((deck) => ({
			deck,
			beat_phase_ms: null
		})),
		contextTime
	);
	await Promise.all(
		schedule.map(({ deck, startAtContextTime }) =>
			dispatch({
				type: 'play',
				deck,
				playing: true,
				start_at_context_sec: startAtContextTime
			})
		)
	);

	for (const deckId of DECK_IDS) {
		const deck = snapshot.decks[deckId];
		const prior = outcomes[String(deckId)];
		if (prior.outcome === 'missing') continue;
		if (deck.playing) {
			const state = query().decks[deckId];
			outcomes[String(deckId)] = {
				outcome: state.playing || state.audible ? 'resumed' : 'paused',
				stable_id: deck.stable_id
			};
		} else {
			outcomes[String(deckId)] = { outcome: 'paused', stable_id: deck.stable_id };
		}
	}

	return {
		snapshot_id: 'ui',
		captured_at_ms: snapshot.captured_at_ms,
		mode: plan.mode,
		decks: outcomes
	};
}

async function _fetchRestorePlan(
	fetchFn: typeof globalThis.fetch,
	play: boolean,
	snapshot_id?: string
): Promise<{ meta: { id: string; captured_at_ms: number; age_ms: number }; snapshot: RescueSnapshot; mode: 'layout' | 'play' } | null> {
	const listResponse = await fetchFn(`${API_BASE}/api/v1/rescue/snapshots`);
	if (!listResponse.ok) return null;
	const listBody = (await listResponse.json()) as {
		snapshots: Array<{ id: string; captured_at_ms: number; age_ms: number }>;
	};
	const newest = listBody.snapshots[0];
	if (newest === undefined) return null;
	const restoreResponse = await fetchFn(`${API_BASE}/api/v1/rescue/restore`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify({ play, snapshot_id: snapshot_id ?? newest.id })
	});
	if (!restoreResponse.ok) return null;
	const restoreBody = (await restoreResponse.json()) as {
		mode: 'layout' | 'play';
		payload: unknown;
	};
	const snapshot = parseRescueSnapshot(restoreBody.payload);
	if (snapshot === null) return null;
	return { meta: newest, snapshot, mode: restoreBody.mode };
}

export async function runPerformanceRescueAutoRestore(
	opts: PerformanceRescueOptions = {}
): Promise<boolean> {
	const location =
		opts.location ??
		(typeof window !== 'undefined' ? window.location : undefined);
	if (location === undefined || location.pathname !== '/performance') {
		return false;
	}
	const fetchFn = opts.fetch ?? globalThis.fetch.bind(globalThis);
	const now = opts.now ?? (() => Date.now());
	const dispatch = opts.dispatch ?? dispatchPerformanceCommand;
	const query = opts.query ?? queryPerformanceState;
	const listResponse = await fetchFn(`${API_BASE}/api/v1/rescue/snapshots`);
	if (!listResponse.ok) return false;
	const listBody = (await listResponse.json()) as {
		snapshots: Array<{ id: string; captured_at_ms: number; age_ms: number }>;
	};
	const newest = listBody.snapshots[0];
	if (newest === undefined || newest.age_ms > RESCUE_LAYOUT_WINDOW_MS) return false;
	const play = newest.age_ms <= RESCUE_PLAY_WINDOW_MS;
	const planRow = await _fetchRestorePlan(fetchFn, play, newest.id);
	if (planRow === null) return false;
	await executeRescueRestore(
		{ snapshot: planRow.snapshot, mode: planRow.mode },
		{
			dispatch,
			query,
			audioContextTime: () => engine.contextTimeNowSec()
		}
	);
	if (!play) {
		pushToast(formatRescueAgeToast(planRow.snapshot.captured_at_ms, now()), 'info');
	}
	return true;
}

export async function postRescueRestore(
	body: { play: boolean; snapshot_id?: string },
	fetchFn: typeof globalThis.fetch = globalThis.fetch.bind(globalThis)
): Promise<RescueRestoreResult> {
	const response = await fetchFn(`${API_BASE}/api/v1/rescue/restore`, {
		method: 'POST',
		headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
		body: JSON.stringify(body)
	});
	if (!response.ok) {
		throw new Error(`rescue restore failed: HTTP ${response.status}`);
	}
	return (await response.json()) as RescueRestoreResult;
}
