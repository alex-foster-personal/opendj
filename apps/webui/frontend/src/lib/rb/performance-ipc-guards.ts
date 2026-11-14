/**
 * Pure performance command parsing and field validation, split out of
 * performance-ipc.svelte.ts. No $state, no engine writes, no command scheduler.
 */

import {
	assertHeadDelayMs,
	assertHeadphoneAlignmentMode,
	assertMasterDelayMs
} from '$lib/player/constants';
import { assertBoothSignalMode } from '$lib/player/booth-output';
import { assertHeadphoneOutputMode } from '$lib/player/headphones';
import { assertCalibrationProbe } from '$lib/player/cue-latency';
import {
	COMPATIBLE_KEY_SHIFT_MAX,
	isKeyNudgeStep,
	isKeySyncMode,
	KEY_NUDGE_STEPS,
	type KeySyncMode
} from '$lib/player/key/camelot';
import { VERDICTS, type Verdict } from '$lib/rb/autoplay-why';
import { DECIDE_AFTER_MAX_S, DECIDE_AFTER_MIN_S } from '$lib/rb/auto-play-early';
import { TRANSITION_INTENTS, type TransitionIntent } from '$lib/rb/auto-play-next-plan';
import {
	ANALYSIS_SOURCE_FEATURES,
	type AnalysisSourceFeature
} from '$lib/rb/analysis-source-state.svelte';
import { parseBeatFxCommand, parseBeatFxReleaseCommand } from '$lib/rb/beat-fx';
import { parseColorFxCommand } from '$lib/rb/color-fx';
import { parseTrackFilterState } from '$lib/components/rb/browser/track-filter-state';
import { parseTypeAheadPrefix } from '$lib/components/rb/browser/track-table-type-ahead';
import { type DeckId } from '$lib/rb/deck-slots';
import type { HotCueSlot } from '$lib/rb/deck-state-types';
import { DECK_MOVE_MAX_CROSSFADE_BEATS } from '$lib/rb/deck-move';
import { parseGridEditIntent } from '$lib/rb/grid-edit-command';
import { parseLayoutPx, parseLayoutRegion } from '$lib/rb/layout-regions';
import { parseDeckLayoutChoice } from '$lib/rb/deck-layout-prefs';
import { MIXER_DECK_PAIR_DECKS } from '$lib/rb/mixer-selection.svelte';
import { parseLayoutPreset, parseLayoutSurface } from '$lib/rb/skin-layout';
import { assertBeatJumpSize } from '$lib/rb/beat-jump-size.svelte';
import { isPadModeId, PAD_MODE_IDS } from '$lib/rb/pad-mode-catalog';
import { parseTransitionPresetId } from '$lib/rb/performance-ipc-presets';
import { assertLoopGridBase } from '$lib/rb/loop-interval-view.svelte';
import { PERF_VIEW_IDS, isPerfViewId } from '$lib/rb/view-registry';
import {
	parseWavePlayheadMode,
	parseWaveZoom,
	requireDeckNumberArt
} from '$lib/rb/prefs.svelte';
import type { EdgeRegion } from '$lib/rb/technically-working.svelte';
import type { StemControl } from '$lib/rb/stem-types';
import { parseSkinSettings } from '$lib/rb/ui-skin';
import { isValidRelatedBpmPercent, RELATED_CRITERIA_FLAGS } from '$lib/rb/related-criteria-prefs';
import { parseWaveHeight } from '$lib/rb/wave-height';
import { parseWaveformDesignPref } from '$lib/rb/waveform-design';
import { parseWaveformBoolOverlay } from '$lib/rb/waveform-overlay-command';
import { requireWaveGridLines } from '$lib/rb/wave-grid-lines';
import { TRACK_LOAD_RESET_IDS, type TrackLoadResetId } from '$lib/rb/track-load-reset';
import { WINDOW_LABELS } from '$lib/shell/native-shell';
import { isMixerPlacement } from '$lib/rb/prefs.svelte';
import { assertRollBeats, ROLL_BEATS_LABEL } from '$lib/player/transport/roll-state';
import { MEMORY_CUE_CALL_DIRECTIONS, type MemoryCueCallDirection } from '$lib/rb/memory-cue-call';

import {
	isClassicSamplerCommandType,
	parseClassicSamplerCommand
} from '$lib/player/sampler/classic-sampler-ipc-guards';
import { isSamplerCommandType, parseSamplerCommand } from '$lib/player/sampler/sampler-ipc-guards';

import type { PerformanceCommand } from './performance-ipc-types';

type UnknownRecord = Record<string, unknown>;

// ---------------------------------------------------------------- validation

function _record(message: unknown): UnknownRecord {
	if (typeof message !== 'object' || message === null || Array.isArray(message)) {
		throw new TypeError('performance command must be a plain object');
	}
	return message as UnknownRecord;
}

function _exactKeys(record: UnknownRecord, keys: readonly string[]): void {
	const allowed = new Set(keys);
	const unexpected = Object.keys(record).filter((key) => !allowed.has(key));
	if (unexpected.length > 0) {
		throw new TypeError(`performance command has unexpected fields: ${unexpected.join(', ')}`);
	}
}

/** The range (-12..12 dB) is the setter's and the server's to refuse. */
function _autoGainOffset(value: unknown): number | null {
	if (value === null) return null;
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new TypeError(`offset_db must be null or a finite number of dB, got ${String(value)}`);
	}
	return value;
}

export function _deck(value: unknown): DeckId {
	if (value !== 1 && value !== 2 && value !== 3 && value !== 4) {
		throw new RangeError(`deck must be one of 1, 2, 3, 4; got ${String(value)}`);
	}
	return value;
}

/** A fold a launch may carry: half or double tempo (unity is never sent). */
function _beatSyncNormalization(value: unknown): 0.5 | 2 {
	if (value !== 0.5 && value !== 2)
		throw new RangeError(`normalization must be 0.5 or 2; got ${String(value)}`);
	return value;
}

function _boolean(name: string, value: unknown): boolean {
	if (typeof value !== 'boolean') throw new TypeError(`${name} must be boolean`);
	return value;
}

function _edge(value: unknown): EdgeRegion {
	if (value !== 'top' && value !== 'bottom' && value !== 'left' && value !== 'right') {
		throw new RangeError(`edge must be top, bottom, left, or right; got ${String(value)}`);
	}
	return value;
}

function _finite(name: string, value: unknown): number {
	if (typeof value !== 'number' || !Number.isFinite(value)) {
		throw new TypeError(`${name} must be a finite number`);
	}
	return value;
}

function _unit(name: string, value: unknown): number {
	const parsed = _finite(name, value);
	if (parsed < 0 || parsed > 1) throw new RangeError(`${name} must be within 0..1`);
	return parsed;
}

function _generation(value: unknown): number {
	if (typeof value !== 'number' || !Number.isSafeInteger(value) || value <= 0) {
		throw new TypeError('generation must be a positive safe integer');
	}
	return value;
}

function _stem(value: unknown): StemControl {
	if (
		value !== 'vocal' &&
		value !== 'instrumental' &&
		value !== 'drums' &&
		value !== 'bass' &&
		value !== 'other'
	) {
		throw new TypeError(`unknown stem control: ${String(value)}`);
	}
	return value;
}

function _hotCueSlot(value: unknown): HotCueSlot {
	if (
		value !== 'A' &&
		value !== 'B' &&
		value !== 'C' &&
		value !== 'D' &&
		value !== 'E' &&
		value !== 'F' &&
		value !== 'G' &&
		value !== 'H'
	) {
		throw new TypeError(`hot-cue slot must be A through H; got ${String(value)}`);
	}
	return value;
}

function _revision(name: string, value: unknown): string {
	if (typeof value !== 'string' || value.length === 0) {
		throw new TypeError(`${name} must be a non-empty revision`);
	}
	return value;
}

function _optionalStringOrNull(name: string, value: unknown): string | null | undefined {
	if (value === undefined || value === null) return value;
	if (typeof value !== 'string') throw new TypeError(`${name} must be a string or null`);
	return value;
}

/**
 * Q1: the press stamp crosses the browser IPC boundary like any other input,
 * so it gets the same fail-fast validation every command field gets. A string
 * or a NaN here would silently disable the two press stages on a row that
 * still looks complete, which is worse than a rejected command.
 */
export function _validatedPressStamp(pressT0Ms: unknown): number | undefined {
	if (pressT0Ms === undefined) return undefined;
	if (typeof pressT0Ms !== 'number' || !Number.isFinite(pressT0Ms) || pressT0Ms < 0) {
		throw new TypeError(
			`press stamp must be a finite non-negative performance.now() reading, got ` +
				`${String(pressT0Ms)}`
		);
	}
	return pressT0Ms;
}

/**
 * AGENT-30: an optional AudioContext start time for a step set (absent means now), and the caller's `step_id`, which
 * a scheduled step requires and a plain set never carries: the engine reports that step's state under it.
 */
function _atContextSec(record: Record<string, unknown>): {
	at_context_sec?: number;
	step_id?: string;
} {
	if (record.at_context_sec === undefined) {
		if (record.step_id !== undefined)
			throw new TypeError('step_id is only for a step with at_context_sec');
		return {};
	}
	const at = _finite('at_context_sec', record.at_context_sec);
	if (at < 0) throw new RangeError('at_context_sec must be >= 0');
	if (typeof record.step_id !== 'string' || record.step_id === '')
		throw new TypeError('a step with at_context_sec needs a step_id');
	return { at_context_sec: at, step_id: record.step_id };
}

/** DECKGAP-7691h: `parts` is a non-empty list of distinct reset items. */
function _resetParts(value: unknown): TrackLoadResetId[] {
	if (!Array.isArray(value) || value.length === 0) {
		throw new TypeError(
			`reset_deck_now parts must be a non-empty array of ${TRACK_LOAD_RESET_IDS.join(', ')}`
		);
	}
	const parts: TrackLoadResetId[] = [];
	for (const item of value) {
		if (!(TRACK_LOAD_RESET_IDS as readonly unknown[]).includes(item)) {
			throw new TypeError(
				`reset_deck_now parts: unknown item ${String(item)}; expected ${TRACK_LOAD_RESET_IDS.join(', ')}`
			);
		}
		if (parts.includes(item as TrackLoadResetId))
			throw new TypeError(`reset_deck_now parts: ${String(item)} named twice`);
		parts.push(item as TrackLoadResetId);
	}
	return parts;
}

export function _parseCommand(message: unknown): PerformanceCommand {
	const record = _record(message);
	if (typeof record.type !== 'string')
		throw new TypeError('performance command type must be string');
	const type = record.type;
	if (
		type === 'crossfader' ||
		type === 'master_volume' ||
		type === 'headphone_mix' ||
		type === 'headphone_level' ||
		type === 'headphone_booth_level' ||
		type === 'booth_level'
	) {
		_exactKeys(record, ['type', 'value']);
		return { type, value: _unit('value', record.value) };
	}
	if (type === 'master_mute') {
		if (record.persist === undefined) {
			_exactKeys(record, ['type', 'muted']);
			return { type, muted: _boolean('muted', record.muted) };
		}
		_exactKeys(record, ['type', 'muted', 'persist']);
		return {
			type,
			muted: _boolean('muted', record.muted),
			persist: _boolean('persist', record.persist)
		};
	}
	if (type === 'preview_cue') {
		// CUEOUT-15. `ratio` is validated here rather than clamped, because a
		// caller that sent 1.6 meant something this cannot guess; the pointer
		// path clamps because a pixel outside the strip DOES have an obvious
		// intent.
		_exactKeys(record, ['type', 'stable_id', 'ratio', 'bpm']);
		if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
			throw new TypeError('stable_id must be a non-empty string');
		}
		// `bpm` is the caller's own copy of the track BPM, for the CUEOUT-15 R6
		// tempo match. Optional: the pointer path always has it from the row,
		// and an agent that does not send one gets a preview at its own tempo
		// rather than a metadata request it did not ask for.
		if (record.bpm !== undefined && (typeof record.bpm !== 'number' || !(record.bpm > 0))) {
			throw new TypeError('bpm must be a positive number when given');
		}
		return {
			type,
			stable_id: record.stable_id,
			ratio: _unit('ratio', record.ratio),
			...(record.bpm === undefined ? {} : { bpm: record.bpm })
		};
	}
	if (type === 'preview_stop') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'browser_related') {
		_exactKeys(record, ['type', 'active']);
		if (typeof record.active !== 'boolean') throw new TypeError('active must be a boolean');
		return { type, active: record.active };
	}
	if (type === 'browser_related_criteria') {
		_exactKeys(record, [
			'type',
			'bpm_percent',
			'key',
			'genre',
			'color',
			'rating',
			'mytag',
			'artist'
		]);
		const out: Extract<PerformanceCommand, { type: 'browser_related_criteria' }> = { type };
		let any = false;
		if (record.bpm_percent !== undefined) {
			if (!isValidRelatedBpmPercent(record.bpm_percent)) {
				throw new TypeError('bpm_percent must be an integer 1..20');
			}
			out.bpm_percent = record.bpm_percent;
			any = true;
		}
		for (const { name } of RELATED_CRITERIA_FLAGS) {
			if (record[name] === undefined) continue;
			if (typeof record[name] !== 'boolean') throw new TypeError(`${name} must be a boolean`);
			out[name] = record[name];
			any = true;
		}
		if (!any) throw new TypeError('browser_related_criteria needs at least one criterion');
		return out;
	}
	if (type === 'library_filter') {
		_exactKeys(record, ['type', 'op', 'state']);
		if (record.op === 'get') {
			if (record.state !== undefined) throw new TypeError('library_filter get takes no state');
			return { type, op: 'get' };
		}
		if (record.op !== 'set') throw new TypeError('op must be get or set');
		return { type, op: 'set', state: parseTrackFilterState(record.state) };
	}
	if (type === 'browser_type_ahead') {
		_exactKeys(record, ['type', 'prefix']);
		return { type, prefix: parseTypeAheadPrefix(record.prefix) };
	}
	if (type === 'tag_list_add') {
		_exactKeys(record, ['type', 'stable_ids']);
		const ids = record.stable_ids;
		if (!Array.isArray(ids) || ids.length === 0 || !ids.every((id) => typeof id === 'string' && id !== '')) {
			throw new TypeError('stable_ids must be a non-empty array of non-empty strings');
		}
		return { type, stable_ids: [...ids] as string[] };
	}
	if (type === 'browser_step_selection') {
		_exactKeys(record, ['type', 'delta']);
		if (record.delta !== -1 && record.delta !== 1) throw new TypeError('delta must be -1 or 1');
		return { type, delta: record.delta };
	}
	if (type === 'browser_related_criteria_reset') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'browser_tree_search') {
		_exactKeys(record, ['type', 'query']);
		if (typeof record.query !== 'string' || record.query.trim() === '') {
			throw new TypeError('query must be a non-empty string; browser_tree_search_clear empties it');
		}
		return { type, query: record.query };
	}
	if (type === 'browser_tree_search_clear') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'browser_select_playlist') {
		_exactKeys(record, ['type', 'playlist_id', 'stable_id']);
		if (typeof record.playlist_id !== 'string' || record.playlist_id.trim() === '') {
			throw new TypeError('playlist_id must be a non-empty string');
		}
		if (record.stable_id === undefined) return { type, playlist_id: record.playlist_id };
		if (typeof record.stable_id !== 'string' || record.stable_id === '') {
			throw new TypeError('stable_id must be a non-empty string when given');
		}
		return { type, playlist_id: record.playlist_id, stable_id: record.stable_id };
	}
	if (type === 'midi_takeover_mode') {
		_exactKeys(record, ['type', 'mode']);
		if (record.mode !== 'pickup' && record.mode !== 'jump') {
			throw new TypeError(`midi takeover mode must be pickup or jump; got ${String(record.mode)}`);
		}
		return { type, mode: record.mode };
	}
	if (type === 'midi_displaced_warning') {
		_exactKeys(record, ['type', 'enabled']);
		if (typeof record.enabled !== 'boolean') {
			throw new TypeError(
				`midi_displaced_warning enabled must be a boolean; got ${String(record.enabled)}`
			);
		}
		return { type, enabled: record.enabled };
	}
	if (type === 'midi_learn_retry' || type === 'midi_learn_cancel') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'headphone_outputs_refresh') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'headphone_output_acquire') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'headphone_output_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (typeof record.device_id !== 'string' || record.device_id.trim() === '') {
			throw new TypeError('device_id must be a non-empty string');
		}
		return { type, device_id: record.device_id };
	}
	if (type === 'headphone_master_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (typeof record.device_id !== 'string' || record.device_id.trim() === '') {
			throw new TypeError('device_id must be a non-empty string');
		}
		return { type, device_id: record.device_id };
	}
	if (type === 'headphone_booth_select' || type === 'booth_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (
			typeof record.device_id !== 'string' ||
			(record.device_id !== '' && record.device_id.trim() === '')
		) {
			throw new TypeError('device_id must be empty or a non-blank string');
		}
		return { type, device_id: record.device_id };
	}
	if (type === 'booth_signal_mode') {
		_exactKeys(record, ['type', 'mode']);
		assertBoothSignalMode(record.mode);
		return { type, mode: record.mode };
	}
	if (type === 'headphone_input_select') {
		_exactKeys(record, ['type', 'device_id']);
		if (typeof record.device_id !== 'string' || record.device_id.trim() === '') {
			throw new TypeError('device_id must be a non-empty string');
		}
		return { type, device_id: record.device_id };
	}
	if (type === 'output_mode') {
		_exactKeys(record, ['type', 'mode']);
		assertHeadphoneOutputMode(record.mode);
		return { type, mode: record.mode };
	}
	if (type === 'head_delay_ms') {
		_exactKeys(record, ['type', 'value']);
		assertHeadDelayMs(record.value);
		return { type, value: record.value };
	}
	if (type === 'headphone_alignment_mode') {
		_exactKeys(record, ['type', 'value']);
		assertHeadphoneAlignmentMode(record.value);
		return { type, value: record.value };
	}
	if (type === 'headphone_calibration_probe') {
		_exactKeys(record, ['type', 'value']);
		assertCalibrationProbe(record.value);
		return { type, value: record.value };
	}
	if (type === 'master_delay_ms') {
		_exactKeys(record, ['type', 'value']);
		assertMasterDelayMs(record.value);
		return { type, value: record.value };
	}
	if (type === 'headphone_calibrate') {
		// `interactive` is the modal's flag (wait for the ear-cup step); HTTP and
		// CLI callers omit it and get the headless run.
		_exactKeys(record, ['type', 'interactive']);
		if (record.interactive === undefined) return { type };
		if (typeof record.interactive !== 'boolean') {
			throw new TypeError(
				`headphone_calibrate interactive must be boolean; got ${String(record.interactive)}`
			);
		}
		return { type, interactive: record.interactive };
	}
	if (type === 'headphone_calibrate_abort') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'analysis_source') {
		_exactKeys(record, ['type', 'feature', 'source']);
		if (!(ANALYSIS_SOURCE_FEATURES as readonly unknown[]).includes(record.feature)) {
			throw new TypeError(
				`analysis-source feature must be one of ${ANALYSIS_SOURCE_FEATURES.join(', ')}; got ${String(record.feature)}`
			);
		}
		if (record.source !== 'rekordbox' && record.source !== 'own') {
			throw new TypeError(
				`analysis-source source must be rekordbox or own; got ${String(record.source)}`
			);
		}
		return { type, feature: record.feature as AnalysisSourceFeature, source: record.source };
	}
	if (type === 'auto_play_two_track') {
		_exactKeys(record, ['type', 'enabled']);
		return { type, enabled: _boolean('enabled', record.enabled) };
	}
	if (type === 'toggle_large_library') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'set_deck_layout') {
		_exactKeys(record, ['type', 'layout']);
		return { type, layout: parseDeckLayoutChoice(record.layout) };
	}
	if (type === 'set_perf_view') {
		_exactKeys(record, ['type', 'view']);
		if (!isPerfViewId(record.view)) {
			throw new TypeError(
				`set_perf_view view must be one of ${PERF_VIEW_IDS.join(', ')}; got ${String(record.view)}`
			);
		}
		return { type, view: record.view };
	}
	if (type === 'show_pads' || type === 'set_lyrics_global') {
		_exactKeys(record, ['type', 'enabled']);
		return { type, enabled: _boolean('enabled', record.enabled) };
	}
	if (type === 'key_nudge_step') {
		_exactKeys(record, ['type', 'step']);
		if (!isKeyNudgeStep(record.step)) {
			throw new TypeError(
				`key_nudge_step step must be one of ${KEY_NUDGE_STEPS.join(', ')}; got ${String(record.step)}`
			);
		}
		return { type, step: record.step };
	}
	if (type === 'reset_deck_now') {
		_exactKeys(record, ['type', 'deck', 'parts']);
		const deck = _deck(record.deck);
		if (record.parts === undefined) return { type, deck };
		return { type, deck, parts: _resetParts(record.parts) };
	}
	if (type === 'track_load_reset') {
		_exactKeys(record, ['type', ...TRACK_LOAD_RESET_IDS]);
		const [level, stems, eq, trim, filter] = TRACK_LOAD_RESET_IDS.map((id) =>
			_boolean(id, record[id])
		);
		return { type, level, stems, eq, trim, filter };
	}
	if (type === 'auto_play_next_arm') {
		_exactKeys(record, ['type', 'intent', 'out_deck', 'in_deck', 'from_ms']);
		if (
			record.intent !== undefined &&
			!TRANSITION_INTENTS.includes(record.intent as TransitionIntent)
		) {
			throw new Error(`auto_play_next_arm: unknown intent ${String(record.intent)}`);
		}
		if ((record.out_deck === undefined) !== (record.in_deck === undefined)) {
			throw new Error('auto_play_next_arm: out_deck and in_deck come together');
		}
		if (
			record.from_ms !== undefined &&
			(typeof record.from_ms !== 'number' || !Number.isFinite(record.from_ms) || record.from_ms < 0)
		) {
			throw new Error('auto_play_next_arm: from_ms must be a finite ms >= 0');
		}
		return {
			type,
			...(record.intent === undefined ? {} : { intent: record.intent as TransitionIntent }),
			...(record.out_deck === undefined
				? {}
				: { out_deck: _deck(record.out_deck), in_deck: _deck(record.in_deck) }),
			...(record.from_ms === undefined ? {} : { from_ms: record.from_ms as number })
		};
	}
	if (type === 'auto_play_next_cancel') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'auto_play_decide_now') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'autoplay_verdict') {
		_exactKeys(record, ['type', 'out', 'in', 'verdict']);
		if (
			typeof record.out !== 'string' ||
			record.out === '' ||
			typeof record.in !== 'string' ||
			record.in === ''
		) {
			throw new Error('autoplay_verdict: out and in must be stable ids');
		}
		if (!VERDICTS.includes(record.verdict as Verdict)) {
			throw new Error(`autoplay_verdict: unknown verdict ${String(record.verdict)}`);
		}
		return { type, out: record.out, in: record.in, verdict: record.verdict as Verdict };
	}
	if (type === 'auto_play_next_intent') {
		_exactKeys(record, ['type', 'intent']);
		if (!TRANSITION_INTENTS.includes(record.intent as TransitionIntent)) {
			throw new Error(`auto_play_next_intent: unknown intent ${String(record.intent)}`);
		}
		return { type, intent: record.intent as TransitionIntent };
	}
	if (type === 'auto_play_early') {
		_exactKeys(record, ['type', 'enabled', 'decide_after_s']);
		const after = record.decide_after_s;
		if (
			after !== undefined &&
			!(typeof after === 'number' && after >= DECIDE_AFTER_MIN_S && after <= DECIDE_AFTER_MAX_S)
		) {
			throw new Error(
				`auto_play_early: decide_after_s must be ${DECIDE_AFTER_MIN_S}..${DECIDE_AFTER_MAX_S}, got ${String(after)}`
			);
		}
		return {
			type,
			...(record.enabled === undefined ? {} : { enabled: _boolean('enabled', record.enabled) }),
			...(after === undefined ? {} : { decide_after_s: after as number })
		};
	}
	if (type === 'pins_show_other_users') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (
		type === 'tech_mode_toggle' ||
		type === 'track_info_toggle' ||
		type === 'track_info_close' ||
		type === 'oj_toggle'
	) {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'mixer_deck_pair_select') {
		const pair = record.pair;
		if (pair !== '1-3' && pair !== '2-4') {
			throw new TypeError(`mixer_deck_pair_select pair must be 1-3 or 2-4, got ${String(pair)}`);
		}
		if (record.deck === undefined) {
			_exactKeys(record, ['type', 'pair']);
			return { type, pair };
		}
		_exactKeys(record, ['type', 'pair', 'deck']);
		const deck = _deck(record.deck);
		if (!MIXER_DECK_PAIR_DECKS[pair].includes(deck)) {
			throw new TypeError(`mixer_deck_pair_select deck ${deck} is not in pair ${pair}`);
		}
		return { type, pair, deck };
	}
	if (type === 'transition_preset') {
		_exactKeys(record, ['type', 'preset']);
		return { type, preset: parseTransitionPresetId(record.preset) };
	}
	if (type === 'track_info_open') {
		if (record.stable_id !== undefined) {
			_exactKeys(record, ['type', 'stable_id']);
			if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
				throw new TypeError('track_info_open stable_id must be a non-empty string');
			}
			return { type, stable_id: record.stable_id };
		}
		_exactKeys(record, ['type', 'deck']);
		return { type, deck: _deck(record.deck) };
	}
	if (type === 'tech_mode_peek') {
		_exactKeys(record, ['type', 'peeking']);
		return { type, peeking: _boolean('peeking', record.peeking) };
	}
	if (type === 'tech_mode_opt_reveal') {
		_exactKeys(record, ['type', 'revealed']);
		return { type, revealed: _boolean('revealed', record.revealed) };
	}
	if (type === 'tech_mode_eq_raised') {
		_exactKeys(record, ['type', 'raised']);
		return { type, raised: _boolean('raised', record.raised) };
	}
	if (type === 'tech_mode_edge_hover') {
		_exactKeys(record, ['type', 'edge', 'hovered']);
		return { type, edge: _edge(record.edge), hovered: _boolean('hovered', record.hovered) };
	}
	if (type === 'hotkeys_overlay') {
		_exactKeys(record, ['type', 'open']);
		return { type, open: _boolean('open', record.open) };
	}
	if (type === 'cmdk_open') {
		_exactKeys(record, record.query === undefined ? ['type'] : ['type', 'query']);
		if (record.query === undefined) return { type };
		if (typeof record.query !== 'string') throw new TypeError('cmdk_open query must be a string');
		return { type, query: record.query };
	}
	if (type === 'cmdk_close') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'drawer_open' || type === 'drawer_close') {
		_exactKeys(record, ['type', 'drawer']);
		if (record.drawer !== 'jobs') {
			throw new RangeError(`drawer must be one of jobs; got ${String(record.drawer)}`);
		}
		return { type, drawer: record.drawer };
	}
	if (type === 'cmdk_pick') {
		_exactKeys(record, [
			'type',
			'index',
			...(record.id === undefined ? [] : ['id']),
			...(record.deck === undefined ? [] : ['deck']),
			...(record.target === undefined ? [] : ['target']),
			...(record.confirm === undefined ? [] : ['confirm']),
			...(record.vocals_only === undefined ? [] : ['vocals_only'])
		]);
		if (typeof record.index !== 'number' || !Number.isInteger(record.index) || record.index < 0) {
			throw new TypeError('cmdk_pick index must be a non-negative integer');
		}
		if (record.id !== undefined && (typeof record.id !== 'string' || record.id === '')) {
			throw new TypeError(
				'cmdk_pick id must be a non-empty string (the row id query().cmdk reported)'
			);
		}
		return {
			type,
			index: record.index,
			...(record.id === undefined ? {} : { id: record.id }),
			...(record.deck === undefined ? {} : { deck: _deck(record.deck) }),
			...(record.target === undefined ? {} : { target: _deck(record.target) }),
			...(record.confirm === undefined ? {} : { confirm: _boolean('confirm', record.confirm) }),
			...(record.vocals_only === undefined
				? {}
				: { vocals_only: _boolean('vocals_only', record.vocals_only) })
		};
	}
	if (type === 'cmdk_mode') {
		_exactKeys(record, ['type', 'mode']);
		if (record.mode !== 'plain' && record.mode !== 'ai')
			throw new TypeError("cmdk_mode mode must be 'plain' or 'ai'");
		return { type, mode: record.mode };
	}
	if (type === 'window') {
		_exactKeys(record, ['type', 'action', 'label']);
		if (record.action !== 'open' && record.action !== 'close' && record.action !== 'focus') {
			throw new TypeError(`window action must be open, close or focus, got ${String(record.action)}`);
		}
		const label =
			typeof record.label === 'string' ? WINDOW_LABELS.find((known) => known === record.label) : undefined;
		if (label === undefined) {
			throw new TypeError(
				`unknown window label ${JSON.stringify(record.label)}; known: ${WINDOW_LABELS.join(', ')}`
			);
		}
		return { type, action: record.action, label };
	}
	if (type === 'playlist_undo' || type === 'playlist_redo' || type === 'rescue_stop_all') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'playlist_history_jump') {
		_exactKeys(record, ['type', 'command_id']);
		if (typeof record.command_id !== 'string' || record.command_id === '') {
			throw new TypeError('playlist_history_jump requires a non-empty command_id');
		}
		return { type, command_id: record.command_id };
	}
	if (type === 'deck_move') {
		_exactKeys(record, ['type', 'from', 'to', 'crossfade_beats', 'mode', 'force']);
		const from = _deck(record.from),
			to = _deck(record.to);
		if (from === to) throw new RangeError(`deck_move from and to are both deck ${from}`);
		if (record.mode !== undefined && record.mode !== 'move' && record.mode !== 'copy')
			throw new TypeError("deck_move mode must be 'move' or 'copy'");
		const beats =
			record.crossfade_beats === undefined
				? undefined
				: _finite('crossfade_beats', record.crossfade_beats);
		if (beats !== undefined && (beats < 0 || beats > DECK_MOVE_MAX_CROSSFADE_BEATS))
			throw new RangeError(
				`crossfade_beats must be 0..${DECK_MOVE_MAX_CROSSFADE_BEATS}; got ${beats}`
			);
		return {
			type,
			from,
			to,
			...(beats === undefined ? {} : { crossfade_beats: beats }),
			...(record.mode === undefined ? {} : { mode: record.mode }),
			...(record.force === undefined ? {} : { force: _boolean('force', record.force) })
		};
	}
	if (type === 'rescue_resume') {
		_exactKeys(record, ['type', 'decks', 'master_deck']);
		if (!Array.isArray(record.decks) || record.decks.length === 0) {
			throw new RangeError('rescue_resume requires at least one deck');
		}
		const decks = record.decks.map((entry) => {
			const row = _record(entry);
			_exactKeys(row, ['deck', 'position_ms']);
			const position_ms = _finite('position_ms', row.position_ms);
			if (position_ms < 0) throw new RangeError('position_ms must be >= 0');
			return { deck: _deck(row.deck), position_ms };
		});
		if (record.master_deck === undefined) {
			throw new TypeError('rescue_resume requires master_deck (RESCUE-06)');
		}
		const master_deck = _deck(record.master_deck);
		if (!decks.some((entry) => entry.deck === master_deck)) {
			throw new RangeError(
				`rescue_resume master_deck ${master_deck} is not one of the resumed decks`
			);
		}
		return { type, decks, master_deck };
	}
	if (type === 'pairing_snapshot_open') {
		_exactKeys(record, ['type']);
		return { type };
	}
	if (type === 'pairing_snapshot_remove_eq_adjuster') {
		_exactKeys(record, ['type', 'deck', 'band']);
		const deck = _deck(record.deck);
		if (record.band !== 'low' && record.band !== 'mid' && record.band !== 'high') {
			throw new TypeError(`band must be low, mid, or high; got ${String(record.band)}`);
		}
		return { type, deck, band: record.band };
	}
	if (type === 'pairing_snapshot_save') {
		_exactKeys(record, ['type', 'from_deck', 'to_deck']);
		const from_deck = _deck(record.from_deck);
		const to_deck = _deck(record.to_deck);
		if (from_deck === to_deck) throw new RangeError('pairing snapshot requires two distinct decks');
		return { type, from_deck, to_deck };
	}
	if (type === 'library_panels') {
		_exactKeys(record, ['type', 'panel', 'collapsed']);
		if (record.panel !== 'next' && record.panel !== 'recommended') {
			throw new TypeError(`library panel must be next or recommended; got ${String(record.panel)}`);
		}
		return { type, panel: record.panel, collapsed: _boolean('collapsed', record.collapsed) };
	}
	if (type === 'library_panels_toggle') {
		_exactKeys(record, ['type', 'collapsed']);
		return {
			type,
			...(record.collapsed === undefined
				? {}
				: { collapsed: _boolean('collapsed', record.collapsed) })
		};
	}
	if (type === 'library_mytag_pane') {
		_exactKeys(record, ['type', 'enabled']);
		return {
			type,
			...(record.enabled === undefined ? {} : { enabled: _boolean('enabled', record.enabled) })
		};
	}
	if (type === 'library_split_column') {
		_exactKeys(record, ['type', 'enabled']);
		return {
			type,
			...(record.enabled === undefined ? {} : { enabled: _boolean('enabled', record.enabled) })
		};
	}
	if (type === 'library_single_column') {
		_exactKeys(record, ['type', 'enabled']);
		return {
			type,
			...(record.enabled === undefined ? {} : { enabled: _boolean('enabled', record.enabled) })
		};
	}
	if (type === 'mixer_placement') {
		_exactKeys(record, ['type', 'placement']);
		if (!isMixerPlacement(record.placement)) {
			throw new TypeError(`mixer_placement placement must be center|sides, got ${String(record.placement)}`);
		}
		return { type, placement: record.placement };
	}
	if (type === 'color_fx') return parseColorFxCommand(record);
	if (type === 'beat_fx') return parseBeatFxCommand(record);
	if (type === 'beat_fx_release') return parseBeatFxReleaseCommand(record);
	if (
		type === 'show_stems' ||
		type === 'wave_eq_reflect' ||
		type === 'mixer_stems' ||
		type === 'deck_autocue'
	) {
		_exactKeys(record, ['type', 'enabled']);
		return { type, enabled: _boolean('enabled', record.enabled) };
	}
	if (type === 'autoplay') {
		if (record.by_user === undefined) {
			_exactKeys(record, ['type', 'enabled']);
			return { type, enabled: _boolean('enabled', record.enabled) };
		}
		_exactKeys(record, ['type', 'enabled', 'by_user']);
		return {
			type,
			enabled: _boolean('enabled', record.enabled),
			by_user: _boolean('by_user', record.by_user)
		};
	}
	if (type === 'set_skin') {
		_exactKeys(record, ['type', 'ui_skin', 'wave_palette', 'wave_split_master']);
		return { type, ...parseSkinSettings(record) };
	}
	if (type === 'set_layout_preset') {
		_exactKeys(record, ['type', 'surface', 'preset']);
		const surface = parseLayoutSurface(record.surface);
		return { type, surface, preset: parseLayoutPreset(surface, record.preset) };
	}
	if (type === 'set_layout_size') {
		_exactKeys(record, ['type', 'region', 'px']);
		const region = parseLayoutRegion(record.region);
		return { type, region, px: parseLayoutPx(region, record.px) };
	}
	if (type === 'reset_layout') {
		_exactKeys(record, record.region === undefined ? ['type'] : ['type', 'region']);
		return record.region === undefined
			? { type }
			: { type, region: parseLayoutRegion(record.region) };
	}
	if (type === 'set_wave_zoom' || type === 'wave_zoom_step') {
		const valueKey = type === 'set_wave_zoom' ? 'zoom' : 'direction';
		_exactKeys(record, record.deck === undefined ? ['type', valueKey] : ['type', valueKey, 'deck']);
		const deck = record.deck === undefined ? undefined : _deck(record.deck);
		if (type === 'set_wave_zoom') {
			const zoom = parseWaveZoom(record.zoom);
			if (zoom === undefined) throw new TypeError('zoom is required');
			return deck === undefined ? { type, zoom } : { type, zoom, deck };
		}
		if (record.direction !== 'in' && record.direction !== 'out') {
			throw new TypeError(`direction must be in or out; got ${String(record.direction)}`);
		}
		return deck === undefined
			? { type, direction: record.direction }
			: { type, direction: record.direction, deck };
	}
	if (type === 'set_wave_zoom_options') {
		_exactKeys(record, ['type', 'playhead', 'linked']);
		const playhead = parseWavePlayheadMode(record.playhead);
		if (playhead === undefined) throw new TypeError('playhead is required');
		return { type, playhead, linked: _boolean('linked', record.linked) };
	}
	if (type === 'set_deck_number_art') {
		_exactKeys(record, ['type', 'mode']);
		return { type, mode: requireDeckNumberArt(record.mode, 'mode') };
	}
	if (type === 'set_waveform_overlay') {
		if (record.overlay === 'grid_lines') {
			_exactKeys(record, ['type', 'overlay', 'value']);
			return { type, overlay: 'grid_lines', value: requireWaveGridLines(record.value, 'value') };
		}
		_exactKeys(record, ['type', 'overlay', 'enabled']);
		return {
			type,
			overlay: parseWaveformBoolOverlay(record.overlay),
			enabled: _boolean('enabled', record.enabled)
		};
	}
	// SAMPLER: edge sampler parity commands (sampler-ipc-guards.ts).
	if (isSamplerCommandType(type)) return parseSamplerCommand(record);
	// SAMPLER: classic sampler unit parity commands (classic-sampler-ipc-guards.ts).
	if (isClassicSamplerCommandType(type)) return parseClassicSamplerCommand(record);
	if (type === 'set_waveform_design') {
		_exactKeys(record, ['type', 'design', 'height']);
		const design = parseWaveformDesignPref(record.design);
		const height = record.height === undefined ? undefined : parseWaveHeight(record.height);
		if (record.height !== undefined && height === undefined) {
			throw new TypeError(`height must be full|half; got ${String(record.height)}`);
		}
		if (design === undefined && height === undefined) {
			throw new TypeError('set_waveform_design needs design or height');
		}
		return {
			type,
			...(design === undefined ? {} : { design }),
			...(height === undefined ? {} : { height })
		};
	}
	if (type === 'feedback_mark') {
		_exactKeys(record, ['type', 'vote']);
		if (record.vote !== 'bad' && record.vote !== 'good' && record.vote !== 'great') {
			throw new TypeError(`feedback vote must be bad, good, or great; got ${String(record.vote)}`);
		}
		return { type, vote: record.vote };
	}
	const deck = _deck(record.deck);
	if (type === 'load') {
		_exactKeys(record, [
			'type',
			'deck',
			'stable_id',
			'edit_id',
			'in_ms',
			'out_ms',
			'refuseIfMaster',
			'stems',
			'suppressCommandErrorToast',
			'key_shift',
			'replace_playing'
		]);
		if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
			throw new TypeError('stable_id must be a non-empty string');
		}
		const editId = record.edit_id;
		if (editId !== undefined && (typeof editId !== 'string' || editId.trim() === '')) {
			throw new TypeError('edit_id must be a non-empty string when provided');
		}
		const inMs = record.in_ms === undefined ? undefined : _finite('in_ms', record.in_ms);
		const outMs = record.out_ms === undefined ? undefined : _finite('out_ms', record.out_ms);
		if (
			(inMs === undefined) !== (outMs === undefined) ||
			(inMs !== undefined &&
				(!Number.isInteger(inMs) ||
					!Number.isInteger(outMs) ||
					inMs < 0 ||
					inMs >= (outMs as number)))
		) {
			throw new RangeError('edit window must satisfy integer 0 <= in_ms < out_ms');
		}
		const refuseIfMaster =
			record.refuseIfMaster === undefined
				? undefined
				: _boolean('refuseIfMaster', record.refuseIfMaster);
		const stems = record.stems === undefined ? undefined : _boolean('stems', record.stems);
		const suppressCommandErrorToast =
			record.suppressCommandErrorToast === undefined
				? undefined
				: _boolean('suppressCommandErrorToast', record.suppressCommandErrorToast);
		const replacePlaying =
			record.replace_playing === undefined
				? undefined
				: _boolean('replace_playing', record.replace_playing);
		// KEYSHIFT-03: a compatible-filter row loads shifted into the key it plays in.
		const keyShift = record.key_shift;
		if (
			keyShift !== undefined &&
			(typeof keyShift !== 'number' ||
				!Number.isInteger(keyShift) ||
				Math.abs(keyShift) > COMPATIBLE_KEY_SHIFT_MAX)
		) {
			throw new RangeError(
				`key_shift must be an integer within ±${COMPATIBLE_KEY_SHIFT_MAX}; got ${String(keyShift)}`
			);
		}
		return {
			type,
			deck,
			stable_id: record.stable_id,
			...(editId === undefined ? {} : { edit_id: editId }),
			...(inMs === undefined ? {} : { in_ms: inMs, out_ms: outMs as number }),
			...(keyShift === undefined || keyShift === 0 ? {} : { key_shift: keyShift }),
			...(refuseIfMaster === undefined ? {} : { refuseIfMaster }),
			...(stems === undefined ? {} : { stems }),
			...(suppressCommandErrorToast === undefined ? {} : { suppressCommandErrorToast }),
			...(replacePlaying === true ? { replace_playing: true } : {})
		};
	} else if (type === 'load_play_intent') {
		_exactKeys(record, ['type', 'deck', 'generation', 'desired_play']);
		return {
			type,
			deck,
			generation: _generation(record.generation),
			desired_play: _boolean('desired_play', record.desired_play)
		};
	} else if (type === 'unload') {
		_exactKeys(record, ['type', 'deck', 'refuseIfMaster']);
		if (record.refuseIfMaster === undefined) return { type, deck };
		return { type, deck, refuseIfMaster: _boolean('refuseIfMaster', record.refuseIfMaster) };
	} else if (type === 'cue') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'memory_cue_call') {
		_exactKeys(record, ['type', 'deck', 'direction']);
		if (!(MEMORY_CUE_CALL_DIRECTIONS as readonly unknown[]).includes(record.direction)) {
			throw new TypeError(
				`memory_cue_call direction must be prev or next; got ${String(record.direction)}`
			);
		}
		return { type, deck, direction: record.direction as MemoryCueCallDirection };
	} else if (type === 'master') {
		_exactKeys(record, ['type', 'deck', 'lock']);
		if (record.lock === undefined) return { type, deck };
		return { type, deck, lock: _boolean('lock', record.lock) };
	} else if (type === 'play') {
		_exactKeys(record, ['type', 'deck', 'playing', 'quantize', 'start_at_context_sec']);
		const playing = _boolean('playing', record.playing);
		const start_at_context_sec =
			record.start_at_context_sec === undefined
				? undefined
				: _finite('start_at_context_sec', record.start_at_context_sec);
		if (start_at_context_sec !== undefined && start_at_context_sec < 0) {
			throw new RangeError('start_at_context_sec must be >= 0');
		}
		if (record.quantize === undefined && start_at_context_sec === undefined) {
			return { type, deck, playing };
		}
		return {
			type,
			deck,
			playing,
			...(record.quantize === undefined ? {} : { quantize: _boolean('quantize', record.quantize) }),
			...(start_at_context_sec === undefined ? {} : { start_at_context_sec })
		};
	} else if (type === 'safety_loop_save' || type === 'safety_loop_clear') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'safety_loop_arm') {
		_exactKeys(record, ['type', 'deck', 'armed']);
		return { type, deck, armed: _boolean('armed', record.armed) };
	} else if (type === 'key_sync') {
		_exactKeys(record, ['type', 'deck', 'enabled', 'mode', 'target']);
		const command: Extract<PerformanceCommand, { type: 'key_sync' }> = {
			type,
			deck,
			enabled: _boolean('enabled', record.enabled)
		};
		if (record.mode !== undefined) {
			if (!isKeySyncMode(record.mode))
				throw new RangeError(
					`key_sync mode must be exact, relative or compatible; got ${String(record.mode)}`
				);
			command.mode = record.mode as KeySyncMode;
		}
		if (record.target !== undefined) {
			if (record.target !== 'master' && ![1, 2, 3, 4].includes(record.target as number))
				throw new RangeError(
					`key_sync target must be a deck 1..4 or master; got ${String(record.target)}`
				);
			command.target = record.target as DeckId | 'master';
		}
		return command;
	} else if (type === 'seek') {
		_exactKeys(record, ['type', 'deck', 'position_ms', 'exact']);
		const position_ms = _finite('position_ms', record.position_ms);
		if (position_ms < 0) throw new RangeError('position_ms must be >= 0');
		if (record.exact !== undefined && typeof record.exact !== 'boolean')
			throw new TypeError('seek exact must be a boolean');
		return record.exact === true
			? { type, deck, position_ms, exact: true }
			: { type, deck, position_ms };
	} else if (type === 'waveform_seek') {
		_exactKeys(record, ['type', 'deck', 'position_ms', 'snap']);
		const position_ms = _finite('position_ms', record.position_ms);
		if (position_ms < 0) throw new RangeError('position_ms must be >= 0');
		const snap = record.snap;
		if (snap !== 'downbeat' && snap !== 'beat' && snap !== 'exact') {
			throw new TypeError(`waveform_seek snap must be downbeat|beat|exact; got ${String(snap)}`);
		}
		return { type, deck, position_ms, snap };
	} else if (type === 'loop') {
		_exactKeys(record, ['type', 'deck', 'loop', 'quantize', 'if_load_generation']);
		const condition =
			record.if_load_generation === undefined
				? {}
				: { if_load_generation: _generation(record.if_load_generation) };
		// Validated before the loop-exit return, so a bad quantize fails for null too (Sol P1, #6235).
		if (record.quantize !== undefined && record.quantize !== false) {
			throw new TypeError(`loop quantize may only be false; got ${String(record.quantize)}`);
		}
		if (record.loop === null) return { type, deck, loop: null, ...condition };
		const loop = _record(record.loop);
		_exactKeys(loop, ['in_ms', 'out_ms']);
		return {
			type,
			deck,
			...condition,
			...(record.quantize === false ? { quantize: false as const } : {}),
			loop: {
				in_ms: _finite('loop.in_ms', loop.in_ms),
				out_ms: _finite('loop.out_ms', loop.out_ms)
			}
		};
	} else if (type === 'beat_loop') {
		_exactKeys(record, ['type', 'deck', 'beats', 'start_ms', 'if_load_generation']);
		const condition =
			record.if_load_generation === undefined
				? {}
				: { if_load_generation: _generation(record.if_load_generation) };
		const beats = _finite('beats', record.beats);
		if (beats <= 0) {
			throw new RangeError(`beats must be positive; got ${beats}`);
		}
		if (record.start_ms === undefined) return { type, deck, beats, ...condition };
		const start_ms = _finite('start_ms', record.start_ms);
		if (start_ms < 0) throw new RangeError('start_ms must be >= 0');
		return { type, deck, beats, start_ms, ...condition };
	} else if (type === 'loop_in' || type === 'loop_out' || type === 'reloop') {
		_exactKeys(record, ['type', 'deck']);
		return { type, deck };
	} else if (type === 'beat_jump') {
		_exactKeys(record, ['type', 'deck', 'beats']);
		const beats = _finite('beats', record.beats);
		if (beats === 0 || (!Number.isInteger(beats) && Math.abs(beats) !== 0.5)) {
			throw new RangeError(`beats must be a non-zero integer or +-0.5; got ${beats}`);
		}
		return { type, deck, beats };
	} else if (type === 'beat_jump_size') {
		_exactKeys(record, ['type', 'deck', 'beats']);
		const beats = _finite('beats', record.beats);
		assertBeatJumpSize(beats);
		return { type, deck, beats };
	} else if (type === 'grid_jump') {
		_exactKeys(record, ['type', 'deck', 'bars']);
		const bars = _finite('bars', record.bars);
		if (!Number.isInteger(bars) || bars === 0) {
			throw new RangeError(`bars must be a non-zero integer; got ${bars}`);
		}
		return { type, deck, bars };
	} else if (type === 'section_jump') {
		_exactKeys(record, ['type', 'deck', 'direction']);
		if (record.direction !== 'next' && record.direction !== 'previous') {
			throw new RangeError(`direction must be next or previous; got ${String(record.direction)}`);
		}
		return { type, deck, direction: record.direction };
	} else if (type === 'loop_interval_mode') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'pad_mode') {
		_exactKeys(record, ['type', 'deck', 'mode']);
		if (!isPadModeId(record.mode))
			throw new TypeError(`pad mode must be one of ${PAD_MODE_IDS.join('|')}; got ${String(record.mode)}`);
		return { type, deck, mode: record.mode };
	} else if (type === 'loop_interval_base') {
		_exactKeys(record, ['type', 'deck', 'base']);
		const base = _finite('base', record.base);
		// assertLoopGridBase owns the bounds; parsing only guarantees a number
		// reaches it, so the legal-window rule lives in exactly one place.
		assertLoopGridBase(base);
		return { type, deck, base };
	} else if (type === 'tempo') {
		_exactKeys(record, ['type', 'deck', 'ratio']);
		return { type, deck, ratio: _finite('ratio', record.ratio) };
	} else if (type === 'pitch_range') {
		_exactKeys(record, ['type', 'deck', 'range']);
		if (record.range !== 8 && record.range !== 16 && record.range !== 100) {
			throw new RangeError(`range must be 8, 16, or 100; got ${String(record.range)}`);
		}
		return { type, deck, range: record.range };
	} else if (type === 'quantize' && record.by_user !== undefined) {
		_exactKeys(record, ['type', 'deck', 'enabled', 'by_user']);
		return {
			type,
			deck,
			enabled: _boolean('enabled', record.enabled),
			by_user: _boolean('by_user', record.by_user)
		};
	} else if (
		type === 'beat_sync' &&
		(record.normalization !== undefined || record.master !== undefined)
	) {
		_exactKeys(record, ['type', 'deck', 'enabled', 'normalization', 'master']);
		return {
			type,
			deck,
			enabled: _boolean('enabled', record.enabled),
			normalization: _beatSyncNormalization(record.normalization),
			master: _deck(record.master)
		};
	} else if (
		type === 'quantize' ||
		type === 'beat_sync' ||
		type === 'master_tempo' ||
		type === 'slip' ||
		type === 'channel_cue'
	) {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) } as PerformanceCommand;
	} else if (type === 'level_calibration') {
		_exactKeys(record, ['type', 'deck', 'kind', 'enabled']);
		if (record.kind !== 'red' && record.kind !== 'ceiling') {
			throw new TypeError(`level_calibration kind must be red|ceiling, got ${String(record.kind)}`);
		}
		return { type, deck, kind: record.kind, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'roll') {
		_exactKeys(record, ['type', 'deck', 'beats']);
		if (!('beats' in record)) {
			throw new TypeError(`roll beats is required: one of ${ROLL_BEATS_LABEL}, or null to release`);
		}
		if (record.beats === null) return { type, deck, beats: null };
		return { type, deck, beats: assertRollBeats(record.beats) };
	} else if (type === 'quantize_grid') {
		_exactKeys(record, ['type', 'deck', 'beats']);
		if (
			record.beats !== 1 &&
			record.beats !== 4 &&
			record.beats !== 8 &&
			record.beats !== 'phase'
		) {
			throw new RangeError(`beats must be 1, 4, 8, or "phase"; got ${String(record.beats)}`);
		}
		return { type, deck, beats: record.beats };
	} else if (type === 'stem_mute') {
		_exactKeys(record, ['type', 'deck', 'stem', 'muted']);
		return { type, deck, stem: _stem(record.stem), muted: _boolean('muted', record.muted) };
	} else if (type === 'stem_solo') {
		_exactKeys(record, ['type', 'deck', 'stem', 'solo', 'exclusive']);
		return {
			type,
			deck,
			stem: _stem(record.stem),
			solo: _boolean('solo', record.solo),
			...(record.exclusive === undefined
				? {}
				: { exclusive: _boolean('exclusive', record.exclusive) })
		};
	} else if (type === 'stem_eq_mode') {
		_exactKeys(record, ['type', 'deck', 'enabled']);
		return { type, deck, enabled: _boolean('enabled', record.enabled) };
	} else if (type === 'stem_load' || type === 'stem_separate') {
		_exactKeys(record, ['type', 'deck', 'stable_id']);
		if (record.stable_id === undefined) return { type, deck };
		if (typeof record.stable_id !== 'string' || record.stable_id.trim() === '') {
			throw new TypeError('stable_id must be a non-empty string');
		}
		return { type, deck, stable_id: record.stable_id };
	} else if (type === 'stem_gain') {
		_exactKeys(record, ['type', 'deck', 'stem', 'value', 'at_context_sec', 'step_id']);
		return { type, deck, stem: _stem(record.stem), value: _unit('value', record.value), ..._atContextSec(record) };
	} else if (type === 'stem_fx_send') {
		_exactKeys(record, ['type', 'deck', 'stem', 'value']);
		return { type, deck, stem: _stem(record.stem), value: _unit('value', record.value) };
	} else if (type === 'key_shift') {
		_exactKeys(record, ['type', 'deck', 'semitones']);
		const semitones = record.semitones;
		if (
			typeof semitones !== 'number' ||
			!Number.isInteger(semitones) ||
			semitones < -12 ||
			semitones > 12
		) {
			throw new RangeError(
				`key_shift semitones must be an integer from -12 to 12; got ${String(semitones)}`
			);
		}
		return { type, deck, semitones };
	} else if (type === 'key_nudge') {
		_exactKeys(record, ['type', 'deck', 'semitones']);
		const semitones = record.semitones;
		if (
			typeof semitones !== 'number' ||
			!Number.isInteger(semitones) ||
			semitones === 0 ||
			semitones < -12 ||
			semitones > 12
		) {
			throw new RangeError(
				`semitones must be a non-zero integer from -12 to 12, got ${String(semitones)}`
			);
		}
		return { type, deck, semitones };
	} else if (type === 'grid_edit') {
		_exactKeys(record, ['type', 'deck', 'edit']);
		return { type, deck, edit: parseGridEditIntent(record.edit) };
	} else if (type === 'sync_mode') {
		_exactKeys(record, ['type', 'deck', 'mode']);
		if (record.mode !== 'beat' && record.mode !== 'bar') {
			throw new TypeError(`mode must be "beat" or "bar"; got ${String(record.mode)}`);
		}
		return { type, deck, mode: record.mode };
	} else if (type === 'auto_gain_offset') {
		_exactKeys(record, ['type', 'deck', 'offset_db', 'stable_id']);
		if (typeof record.stable_id !== 'string' || record.stable_id.length === 0) {
			throw new TypeError(
				`auto_gain_offset stable_id must name the track meant; got ${String(record.stable_id)}`
			);
		}
		return {
			type,
			deck,
			offset_db: _autoGainOffset(record.offset_db),
			stable_id: record.stable_id
		};
	} else if (type === 'fader') {
		_exactKeys(record, ['type', 'deck', 'value', 'at_context_sec', 'step_id']);
		return { type, deck, value: _unit('value', record.value), ..._atContextSec(record) };
	} else if (type === 'trim' || type === 'filter') {
		_exactKeys(record, ['type', 'deck', 'value']);
		return { type, deck, value: _unit('value', record.value) };
	} else if (type === 'eq') {
		_exactKeys(record, ['type', 'deck', 'band', 'value', 'at_context_sec', 'step_id']);
		if (record.band !== 'low' && record.band !== 'mid' && record.band !== 'high') {
			throw new TypeError(`band must be low, mid, or high; got ${String(record.band)}`);
		}
		return {
			type,
			deck,
			band: record.band,
			value: _unit('value', record.value),
			..._atContextSec(record)
		};
	} else if (type === 'assign') {
		_exactKeys(record, ['type', 'deck', 'assign']);
		if (record.assign !== 'A' && record.assign !== 'B' && record.assign !== 'THRU') {
			throw new TypeError(`assign must be A, B, or THRU; got ${String(record.assign)}`);
		}
		return { type, deck, assign: record.assign };
	} else if (type === 'hot_cue_save') {
		_exactKeys(record, ['type', 'deck', 'slot', 'in_ms', 'revision', 'comment', 'out_ms']);
		const in_ms = _finite('in_ms', record.in_ms);
		if (!Number.isInteger(in_ms) || in_ms < 0)
			throw new RangeError('in_ms must be a non-negative integer');
		const base: Extract<PerformanceCommand, { type: 'hot_cue_save' }> = {
			type: 'hot_cue_save',
			deck,
			slot: _hotCueSlot(record.slot),
			in_ms,
			revision: _revision('revision', record.revision)
		};
		const comment = _optionalStringOrNull('comment', record.comment);
		const withComment = comment === undefined ? base : { ...base, comment };
		// HOTCUE-LOOP-01: out_ms makes it a loop hot cue (start and end).
		if (record.out_ms === undefined || record.out_ms === null) return withComment;
		const out_ms = _finite('out_ms', record.out_ms);
		if (!Number.isInteger(out_ms) || out_ms <= in_ms) {
			throw new RangeError(`out_ms must be an integer after in_ms ${in_ms}, got ${out_ms}`);
		}
		return { ...withComment, out_ms };
	} else if (type === 'hot_cue_clear') {
		_exactKeys(record, ['type', 'deck', 'slot', 'revision']);
		return {
			type,
			deck,
			slot: _hotCueSlot(record.slot),
			revision: _revision('revision', record.revision)
		};
	} else if (type === 'active_memory_loop') {
		_exactKeys(record, ['type', 'deck', 'in_ms', 'out_ms', 'active']);
		const in_ms = _finite('in_ms', record.in_ms);
		const out_ms = _finite('out_ms', record.out_ms);
		if (!Number.isInteger(in_ms) || in_ms < 0) throw new RangeError('in_ms must be a non-negative integer');
		if (!Number.isInteger(out_ms) || out_ms <= in_ms) {
			throw new RangeError(`out_ms must be an integer after in_ms ${in_ms}, got ${out_ms}`);
		}
		return { type, deck, in_ms, out_ms, active: _boolean('active', record.active) };
	} else if (type === 'hot_cue_restore') {
		_exactKeys(record, ['type', 'deck', 'slot', 'revision', 'reversal_id']);
		return {
			type,
			deck,
			slot: _hotCueSlot(record.slot),
			revision: _revision('revision', record.revision),
			reversal_id: _revision('reversal_id', record.reversal_id)
		};
	} else if (type === 'hot_cue_trigger') {
		_exactKeys(record, ['type', 'deck', 'slot']);
		return { type, deck, slot: _hotCueSlot(record.slot) };
	}
	throw new TypeError(`unknown performance command type: ${type}`);
}
