// Typed quick-draw action catalog. YAML (quick-draw-menu.yaml) owns labels/order;
// this module owns PerformanceCommand builders so menu ids cannot drift from IPC/CLI.
// Source inventory: explore agent review of PerformanceCommand + QuickDrawMenu.
import type { PerformanceCommand } from '$lib/rb/performance-ipc.svelte';
import type { DeckId } from '$lib/rb/deck-slots';
import type { SyncMode } from '$lib/rb/deck-state-types';

/** Stable action ids referenced by quick-draw-menu.yaml. */
export type QuickDrawActionId =
	| 'unload'
	| 'loop.start_4'
	| 'loop.start_8'
	| 'loop.start_16'
	| 'loop.exit'
	| 'load'
	| 'play.toggle'
	| 'cue'
	| 'seek'
	| 'quantize.toggle'
	| 'beat_sync.toggle'
	| 'master'
	| 'slip.toggle'
	| 'master_tempo.toggle'
	| 'key_sync.toggle'
	| 'key_nudge.up'
	| 'key_nudge.down'
	| 'sync_mode.bar'
	| 'sync_mode.beat'
	| 'tempo.reset';

export type QuickDrawPriority = 'P0' | 'P1' | 'P2';

/** Runtime context gathered from the click target + live deck state. */
export type QuickDrawCtx = {
	deck: DeckId;
	playing?: boolean;
	quantize_enabled?: boolean;
	beat_sync_enabled?: boolean;
	slip_enabled?: boolean;
	master_tempo_enabled?: boolean;
	key_sync_enabled?: boolean;
	/** Browser row / load target. */
	stable_id?: string;
	/** Waveform pointer position. */
	position_ms?: number;
};

export type QuickDrawAction = {
	id: QuickDrawActionId;
	label: string;
	priority: QuickDrawPriority;
	group: 'quick-draw' | 'contextual' | 'submenu';
	/** Build IPC command; null when required ctx fields are missing. */
	build: (ctx: QuickDrawCtx) => PerformanceCommand | null;
};

function _deckOnly(
	id: QuickDrawActionId,
	label: string,
	priority: QuickDrawPriority,
	group: QuickDrawAction['group'],
	command: (deck: DeckId) => PerformanceCommand
): QuickDrawAction {
	return {
		id,
		label,
		priority,
		group,
		build: (ctx) => command(ctx.deck)
	};
}

function _toggle(
	id: QuickDrawActionId,
	label: string,
	priority: QuickDrawPriority,
	read: (ctx: QuickDrawCtx) => boolean | undefined,
	command: (deck: DeckId, enabled: boolean) => PerformanceCommand
): QuickDrawAction {
	return {
		id,
		label,
		priority,
		group: 'contextual',
		build: (ctx) => {
			const cur = read(ctx);
			if (cur === undefined) return null;
			return command(ctx.deck, !cur);
		}
	};
}

export const QUICK_DRAW_ACTIONS: Record<QuickDrawActionId, QuickDrawAction> = {
	unload: _deckOnly('unload', 'Unload', 'P0', 'quick-draw', (deck) => ({
		type: 'unload',
		deck
	})),
	'loop.start_4': _deckOnly('loop.start_4', 'Start Loop 4B', 'P1', 'submenu', (deck) => ({
		type: 'beat_loop',
		deck,
		beats: 4
	})),
	'loop.start_8': _deckOnly('loop.start_8', 'Start Loop 8B', 'P0', 'submenu', (deck) => ({
		type: 'beat_loop',
		deck,
		beats: 8
	})),
	'loop.start_16': _deckOnly('loop.start_16', 'Start Loop 16B', 'P1', 'submenu', (deck) => ({
		type: 'beat_loop',
		deck,
		beats: 16
	})),
	'loop.exit': _deckOnly('loop.exit', 'Exit Loop', 'P0', 'submenu', (deck) => ({
		type: 'loop',
		deck,
		loop: null
	})),
	load: {
		id: 'load',
		label: 'Load',
		priority: 'P0',
		group: 'contextual',
		build: (ctx) =>
			ctx.stable_id === undefined
				? null
				: { type: 'load', deck: ctx.deck, stable_id: ctx.stable_id }
	},
	'play.toggle': {
		id: 'play.toggle',
		label: 'Play / Pause',
		priority: 'P1',
		group: 'contextual',
		build: (ctx) =>
			ctx.playing === undefined
				? null
				: { type: 'play', deck: ctx.deck, playing: !ctx.playing }
	},
	cue: _deckOnly('cue', 'Cue', 'P1', 'contextual', (deck) => ({ type: 'cue', deck })),
	seek: {
		id: 'seek',
		label: 'Seek here',
		priority: 'P1',
		group: 'contextual',
		build: (ctx) =>
			ctx.position_ms === undefined
				? null
				: { type: 'seek', deck: ctx.deck, position_ms: ctx.position_ms }
	},
	'quantize.toggle': _toggle(
		'quantize.toggle',
		'Quantize',
		'P1',
		(c) => c.quantize_enabled,
		(deck, enabled) => ({ type: 'quantize', deck, enabled, by_user: true })
	),
	'beat_sync.toggle': _toggle(
		'beat_sync.toggle',
		'Beat Sync',
		'P1',
		(c) => c.beat_sync_enabled,
		(deck, enabled) => ({ type: 'beat_sync', deck, enabled })
	),
	master: _deckOnly('master', 'Set Master', 'P1', 'contextual', (deck) => ({
		type: 'master',
		deck
	})),
	'slip.toggle': _toggle(
		'slip.toggle',
		'Slip',
		'P2',
		(c) => c.slip_enabled,
		(deck, enabled) => ({ type: 'slip', deck, enabled })
	),
	'master_tempo.toggle': _toggle(
		'master_tempo.toggle',
		'Master Tempo',
		'P2',
		(c) => c.master_tempo_enabled,
		(deck, enabled) => ({ type: 'master_tempo', deck, enabled })
	),
	'key_sync.toggle': _toggle(
		'key_sync.toggle',
		'Key Sync',
		'P2',
		(c) => c.key_sync_enabled,
		(deck, enabled) => ({ type: 'key_sync', deck, enabled })
	),
	'key_nudge.up': _deckOnly('key_nudge.up', 'Key +1', 'P2', 'contextual', (deck) => ({
		type: 'key_nudge',
		deck,
		semitones: 1
	})),
	'key_nudge.down': _deckOnly('key_nudge.down', 'Key -1', 'P2', 'contextual', (deck) => ({
		type: 'key_nudge',
		deck,
		semitones: -1
	})),
	'sync_mode.bar': _deckOnly('sync_mode.bar', 'Sync BAR', 'P2', 'submenu', (deck) => ({
		type: 'sync_mode',
		deck,
		mode: 'bar' satisfies SyncMode
	})),
	'sync_mode.beat': _deckOnly('sync_mode.beat', 'Sync BEAT', 'P2', 'submenu', (deck) => ({
		type: 'sync_mode',
		deck,
		mode: 'beat' satisfies SyncMode
	})),
	'tempo.reset': _deckOnly('tempo.reset', 'Reset Tempo', 'P2', 'contextual', (deck) => ({
		type: 'tempo',
		deck,
		ratio: 1
	}))
};

/** Deck-only helper used by the current quick-draw CH fan-out. */
export function quickDrawCommand(id: QuickDrawActionId, deck: DeckId): PerformanceCommand {
	const cmd = QUICK_DRAW_ACTIONS[id].build({ deck });
	if (cmd === null) {
		throw new Error(`quick-draw ${id}: missing context for deck-only dispatch`);
	}
	return cmd;
}
