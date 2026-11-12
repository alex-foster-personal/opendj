/**
 * Catalogue of every Open DJ keyboard command (#10351 / KEYB-11488).
 */
import type { CommandScope } from './command-registry';
import type { DeckId } from '$lib/rb/deck-id';
import type { HotCueSlot } from '$lib/rb/hot-cue-types';
import type { MidiAction } from '$lib/rb/midi/midi-types';
import type { EqBand } from '$lib/rb/mixer-types';

export interface KeymapCommand {
	id: string;
	label: string;
	group: string;
	scope: CommandScope;
}

export type KeymapLegacyName =
	| 'transport.toggle'
	| 'transport.toggle_quantized'
	| 'loop.double_last'
	| 'loop.halve_last'
	| 'loop.exit_last'
	| 'wave.zoom_out'
	| 'wave.zoom_in'
	| 'comment.pin'
	| 'layout.deck_less'
	| 'layout.deck_more'
	| 'grid.jump_back_4'
	| 'grid.jump_forward_4'
	| 'grid.jump_back_8'
	| 'grid.jump_forward_8';

export type KeymapCommandTarget =
	| { kind: 'midi'; action: MidiAction }
	| {
			kind: 'perf';
			deck: DeckId;
			op:
				| 'seek_start'
				| 'master'
				| 'keylock'
				| 'loop_in'
				| 'loop_out'
				| 'reloop_exit'
				| 'instant_double';
	  }
	| {
			kind: 'hot_cue';
			deck: DeckId;
			slot: HotCueSlot;
			mode: 'set_or_trigger' | 'set' | 'clear';
	  }
	| {
			kind: 'mixer';
			target: 'fader' | 'eq' | 'crossfader';
			deck?: DeckId;
			band?: EqBand;
			delta: number;
	  }
	| { kind: 'legacy'; name: KeymapLegacyName };

const HOT_CUE_SLOTS: readonly HotCueSlot[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

function instantDoublePartner(deck: DeckId): DeckId {
	if (deck === 1) return 2;
	if (deck === 2) return 1;
	if (deck === 3) return 4;
	return 3;
}

const GLOBAL_COMMANDS: KeymapCommand[] = [
	{ id: 'transport.toggle', label: 'Toggle play', group: 'Transport', scope: 'global' },
	{
		id: 'transport.toggle_quantized',
		label: 'Quantized play toggle',
		group: 'Transport',
		scope: 'global'
	},
	{ id: 'loop.double_last', label: 'Double last loop', group: 'Transport', scope: 'global' },
	{ id: 'loop.halve_last', label: 'Halve last loop', group: 'Transport', scope: 'global' },
	{ id: 'loop.exit_last', label: 'Exit last loop', group: 'Transport', scope: 'global' },
	{ id: 'wave.zoom_out', label: 'Waveform zoom out', group: 'View', scope: 'global' },
	{ id: 'wave.zoom_in', label: 'Waveform zoom in', group: 'View', scope: 'global' },
	{ id: 'comment.pin', label: 'Place comment pin', group: 'Comments', scope: 'global' },
	{ id: 'layout.deck_less', label: 'Fewer visible decks', group: 'View', scope: 'global' },
	{ id: 'layout.deck_more', label: 'More visible decks', group: 'View', scope: 'global' },
	{ id: 'grid.jump_back_4', label: 'Grid jump back 4 bars', group: 'Transport', scope: 'global' },
	{
		id: 'grid.jump_forward_4',
		label: 'Grid jump forward 4 bars',
		group: 'Transport',
		scope: 'global'
	},
	{ id: 'grid.jump_back_8', label: 'Grid jump back 8 bars', group: 'Transport', scope: 'global' },
	{
		id: 'grid.jump_forward_8',
		label: 'Grid jump forward 8 bars',
		group: 'Transport',
		scope: 'global'
	}
];

const DECK_COMMANDS: KeymapCommand[] = [
	{ id: 'deck.1.play', label: 'Deck 1 play toggle', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.cue', label: 'Deck 1 cue', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.jump_start', label: 'Deck 1 jump to start', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.beat_sync', label: 'Deck 1 beat sync', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.sync_master', label: 'Deck 1 sync master', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.keylock', label: 'Deck 1 keylock', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.tempo_down', label: 'Deck 1 tempo down', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.tempo_up', label: 'Deck 1 tempo up', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.loop_in', label: 'Deck 1 loop in', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.loop_out', label: 'Deck 1 loop out', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.reloop_exit', label: 'Deck 1 reloop / exit', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.loop_halve', label: 'Deck 1 halve loop', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.loop_double', label: 'Deck 1 double loop', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.load', label: 'Deck 1 load selection', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.instant_double', label: 'Deck 1 instant double', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.A', label: 'Deck 1 hot cue A', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.A', label: 'Deck 1 set hot cue A', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.A', label: 'Deck 1 clear hot cue A', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.B', label: 'Deck 1 hot cue B', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.B', label: 'Deck 1 set hot cue B', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.B', label: 'Deck 1 clear hot cue B', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.C', label: 'Deck 1 hot cue C', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.C', label: 'Deck 1 set hot cue C', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.C', label: 'Deck 1 clear hot cue C', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.D', label: 'Deck 1 hot cue D', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.D', label: 'Deck 1 set hot cue D', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.D', label: 'Deck 1 clear hot cue D', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.E', label: 'Deck 1 hot cue E', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.E', label: 'Deck 1 set hot cue E', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.E', label: 'Deck 1 clear hot cue E', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.F', label: 'Deck 1 hot cue F', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.F', label: 'Deck 1 set hot cue F', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.F', label: 'Deck 1 clear hot cue F', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.G', label: 'Deck 1 hot cue G', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.G', label: 'Deck 1 set hot cue G', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.G', label: 'Deck 1 clear hot cue G', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue.H', label: 'Deck 1 hot cue H', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_set.H', label: 'Deck 1 set hot cue H', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.1.hot_cue_clear.H', label: 'Deck 1 clear hot cue H', group: 'Deck 1', scope: 'deck' },
	{ id: 'deck.2.play', label: 'Deck 2 play toggle', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.cue', label: 'Deck 2 cue', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.jump_start', label: 'Deck 2 jump to start', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.beat_sync', label: 'Deck 2 beat sync', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.sync_master', label: 'Deck 2 sync master', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.keylock', label: 'Deck 2 keylock', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.tempo_down', label: 'Deck 2 tempo down', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.tempo_up', label: 'Deck 2 tempo up', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.loop_in', label: 'Deck 2 loop in', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.loop_out', label: 'Deck 2 loop out', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.reloop_exit', label: 'Deck 2 reloop / exit', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.loop_halve', label: 'Deck 2 halve loop', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.loop_double', label: 'Deck 2 double loop', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.load', label: 'Deck 2 load selection', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.instant_double', label: 'Deck 2 instant double', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.A', label: 'Deck 2 hot cue A', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.A', label: 'Deck 2 set hot cue A', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.A', label: 'Deck 2 clear hot cue A', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.B', label: 'Deck 2 hot cue B', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.B', label: 'Deck 2 set hot cue B', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.B', label: 'Deck 2 clear hot cue B', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.C', label: 'Deck 2 hot cue C', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.C', label: 'Deck 2 set hot cue C', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.C', label: 'Deck 2 clear hot cue C', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.D', label: 'Deck 2 hot cue D', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.D', label: 'Deck 2 set hot cue D', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.D', label: 'Deck 2 clear hot cue D', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.E', label: 'Deck 2 hot cue E', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.E', label: 'Deck 2 set hot cue E', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.E', label: 'Deck 2 clear hot cue E', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.F', label: 'Deck 2 hot cue F', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.F', label: 'Deck 2 set hot cue F', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.F', label: 'Deck 2 clear hot cue F', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.G', label: 'Deck 2 hot cue G', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.G', label: 'Deck 2 set hot cue G', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.G', label: 'Deck 2 clear hot cue G', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue.H', label: 'Deck 2 hot cue H', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_set.H', label: 'Deck 2 set hot cue H', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.2.hot_cue_clear.H', label: 'Deck 2 clear hot cue H', group: 'Deck 2', scope: 'deck' },
	{ id: 'deck.3.play', label: 'Deck 3 play toggle', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.cue', label: 'Deck 3 cue', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.jump_start', label: 'Deck 3 jump to start', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.beat_sync', label: 'Deck 3 beat sync', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.sync_master', label: 'Deck 3 sync master', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.keylock', label: 'Deck 3 keylock', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.tempo_down', label: 'Deck 3 tempo down', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.tempo_up', label: 'Deck 3 tempo up', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.loop_in', label: 'Deck 3 loop in', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.loop_out', label: 'Deck 3 loop out', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.reloop_exit', label: 'Deck 3 reloop / exit', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.loop_halve', label: 'Deck 3 halve loop', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.loop_double', label: 'Deck 3 double loop', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.load', label: 'Deck 3 load selection', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.instant_double', label: 'Deck 3 instant double', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.A', label: 'Deck 3 hot cue A', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.A', label: 'Deck 3 set hot cue A', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.A', label: 'Deck 3 clear hot cue A', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.B', label: 'Deck 3 hot cue B', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.B', label: 'Deck 3 set hot cue B', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.B', label: 'Deck 3 clear hot cue B', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.C', label: 'Deck 3 hot cue C', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.C', label: 'Deck 3 set hot cue C', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.C', label: 'Deck 3 clear hot cue C', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.D', label: 'Deck 3 hot cue D', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.D', label: 'Deck 3 set hot cue D', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.D', label: 'Deck 3 clear hot cue D', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.E', label: 'Deck 3 hot cue E', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.E', label: 'Deck 3 set hot cue E', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.E', label: 'Deck 3 clear hot cue E', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.F', label: 'Deck 3 hot cue F', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.F', label: 'Deck 3 set hot cue F', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.F', label: 'Deck 3 clear hot cue F', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.G', label: 'Deck 3 hot cue G', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.G', label: 'Deck 3 set hot cue G', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.G', label: 'Deck 3 clear hot cue G', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue.H', label: 'Deck 3 hot cue H', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_set.H', label: 'Deck 3 set hot cue H', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.3.hot_cue_clear.H', label: 'Deck 3 clear hot cue H', group: 'Deck 3', scope: 'deck' },
	{ id: 'deck.4.play', label: 'Deck 4 play toggle', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.cue', label: 'Deck 4 cue', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.jump_start', label: 'Deck 4 jump to start', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.beat_sync', label: 'Deck 4 beat sync', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.sync_master', label: 'Deck 4 sync master', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.keylock', label: 'Deck 4 keylock', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.tempo_down', label: 'Deck 4 tempo down', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.tempo_up', label: 'Deck 4 tempo up', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.loop_in', label: 'Deck 4 loop in', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.loop_out', label: 'Deck 4 loop out', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.reloop_exit', label: 'Deck 4 reloop / exit', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.loop_halve', label: 'Deck 4 halve loop', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.loop_double', label: 'Deck 4 double loop', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.load', label: 'Deck 4 load selection', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.instant_double', label: 'Deck 4 instant double', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.A', label: 'Deck 4 hot cue A', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.A', label: 'Deck 4 set hot cue A', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.A', label: 'Deck 4 clear hot cue A', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.B', label: 'Deck 4 hot cue B', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.B', label: 'Deck 4 set hot cue B', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.B', label: 'Deck 4 clear hot cue B', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.C', label: 'Deck 4 hot cue C', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.C', label: 'Deck 4 set hot cue C', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.C', label: 'Deck 4 clear hot cue C', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.D', label: 'Deck 4 hot cue D', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.D', label: 'Deck 4 set hot cue D', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.D', label: 'Deck 4 clear hot cue D', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.E', label: 'Deck 4 hot cue E', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.E', label: 'Deck 4 set hot cue E', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.E', label: 'Deck 4 clear hot cue E', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.F', label: 'Deck 4 hot cue F', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.F', label: 'Deck 4 set hot cue F', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.F', label: 'Deck 4 clear hot cue F', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.G', label: 'Deck 4 hot cue G', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.G', label: 'Deck 4 set hot cue G', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.G', label: 'Deck 4 clear hot cue G', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue.H', label: 'Deck 4 hot cue H', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_set.H', label: 'Deck 4 set hot cue H', group: 'Deck 4', scope: 'deck' },
	{ id: 'deck.4.hot_cue_clear.H', label: 'Deck 4 clear hot cue H', group: 'Deck 4', scope: 'deck' }
];

const MIXER_COMMANDS: KeymapCommand[] = [
	{ id: 'mixer.fader_up.1', label: 'Channel 1 fader up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.fader_down.1', label: 'Channel 1 fader down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_up.1', label: 'Channel 1 EQ low up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_down.1', label: 'Channel 1 EQ low down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_up.1', label: 'Channel 1 EQ mid up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_down.1', label: 'Channel 1 EQ mid down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_up.1', label: 'Channel 1 EQ high up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_down.1', label: 'Channel 1 EQ high down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.fader_up.2', label: 'Channel 2 fader up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.fader_down.2', label: 'Channel 2 fader down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_up.2', label: 'Channel 2 EQ low up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_down.2', label: 'Channel 2 EQ low down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_up.2', label: 'Channel 2 EQ mid up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_down.2', label: 'Channel 2 EQ mid down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_up.2', label: 'Channel 2 EQ high up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_down.2', label: 'Channel 2 EQ high down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.fader_up.3', label: 'Channel 3 fader up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.fader_down.3', label: 'Channel 3 fader down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_up.3', label: 'Channel 3 EQ low up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_down.3', label: 'Channel 3 EQ low down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_up.3', label: 'Channel 3 EQ mid up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_down.3', label: 'Channel 3 EQ mid down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_up.3', label: 'Channel 3 EQ high up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_down.3', label: 'Channel 3 EQ high down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.fader_up.4', label: 'Channel 4 fader up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.fader_down.4', label: 'Channel 4 fader down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_up.4', label: 'Channel 4 EQ low up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_low_down.4', label: 'Channel 4 EQ low down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_up.4', label: 'Channel 4 EQ mid up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_mid_down.4', label: 'Channel 4 EQ mid down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_up.4', label: 'Channel 4 EQ high up', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.eq_high_down.4', label: 'Channel 4 EQ high down', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.crossfader_left', label: 'Crossfader left', group: 'Mixer', scope: 'global' },
	{ id: 'mixer.crossfader_right', label: 'Crossfader right', group: 'Mixer', scope: 'global' }
];

export const KEYMAP_COMMANDS: readonly KeymapCommand[] = [
	...GLOBAL_COMMANDS,
	...DECK_COMMANDS,
	...MIXER_COMMANDS
];

export function keymapCommandById(id: string): KeymapCommand | undefined {
	return KEYMAP_COMMANDS.find((cmd) => cmd.id === id);
}

export function keymapCommandTarget(id: string): KeymapCommandTarget | null {
	if (id === 'transport.toggle') return { kind: 'legacy', name: 'transport.toggle' };
	if (id === 'transport.toggle_quantized')
		return { kind: 'legacy', name: 'transport.toggle_quantized' };
	if (id === 'loop.double_last') return { kind: 'legacy', name: 'loop.double_last' };
	if (id === 'loop.halve_last') return { kind: 'legacy', name: 'loop.halve_last' };
	if (id === 'loop.exit_last') return { kind: 'legacy', name: 'loop.exit_last' };
	if (id === 'wave.zoom_out') return { kind: 'legacy', name: 'wave.zoom_out' };
	if (id === 'wave.zoom_in') return { kind: 'legacy', name: 'wave.zoom_in' };
	if (id === 'comment.pin') return { kind: 'legacy', name: 'comment.pin' };
	if (id === 'layout.deck_less') return { kind: 'legacy', name: 'layout.deck_less' };
	if (id === 'layout.deck_more') return { kind: 'legacy', name: 'layout.deck_more' };
	if (id === 'grid.jump_back_4') return { kind: 'legacy', name: 'grid.jump_back_4' };
	if (id === 'grid.jump_forward_4') return { kind: 'legacy', name: 'grid.jump_forward_4' };
	if (id === 'grid.jump_back_8') return { kind: 'legacy', name: 'grid.jump_back_8' };
	if (id === 'grid.jump_forward_8') return { kind: 'legacy', name: 'grid.jump_forward_8' };
	if (id === 'deck.1.play') return { kind: 'midi', action: { type: 'deck_play_toggle', deck: 1 } };
	if (id === 'deck.1.cue') return { kind: 'midi', action: { type: 'deck_cue', deck: 1 } };
	if (id === 'deck.1.jump_start') return { kind: 'perf', deck: 1, op: 'seek_start' };
	if (id === 'deck.1.beat_sync')
		return { kind: 'midi', action: { type: 'deck_sync_toggle', deck: 1 } };
	if (id === 'deck.1.sync_master') return { kind: 'perf', deck: 1, op: 'master' };
	if (id === 'deck.1.keylock') return { kind: 'perf', deck: 1, op: 'keylock' };
	if (id === 'deck.1.tempo_down')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 1, direction: -1 } };
	if (id === 'deck.1.tempo_up')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 1, direction: 1 } };
	if (id === 'deck.1.loop_in') return { kind: 'perf', deck: 1, op: 'loop_in' };
	if (id === 'deck.1.loop_out') return { kind: 'perf', deck: 1, op: 'loop_out' };
	if (id === 'deck.1.reloop_exit') return { kind: 'perf', deck: 1, op: 'reloop_exit' };
	if (id === 'deck.1.loop_halve')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 1, factor: 0.5 } };
	if (id === 'deck.1.loop_double')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 1, factor: 2 } };
	if (id === 'deck.1.load') return { kind: 'midi', action: { type: 'browse_load', deck: 1 } };
	if (id === 'deck.1.instant_double') return { kind: 'perf', deck: 1, op: 'instant_double' };
	if (id === 'deck.1.hot_cue.A')
		return { kind: 'hot_cue', deck: 1, slot: 'A', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.A') return { kind: 'hot_cue', deck: 1, slot: 'A', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.A')
		return { kind: 'hot_cue', deck: 1, slot: 'A', mode: 'clear' };
	if (id === 'deck.1.hot_cue.B')
		return { kind: 'hot_cue', deck: 1, slot: 'B', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.B') return { kind: 'hot_cue', deck: 1, slot: 'B', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.B')
		return { kind: 'hot_cue', deck: 1, slot: 'B', mode: 'clear' };
	if (id === 'deck.1.hot_cue.C')
		return { kind: 'hot_cue', deck: 1, slot: 'C', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.C') return { kind: 'hot_cue', deck: 1, slot: 'C', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.C')
		return { kind: 'hot_cue', deck: 1, slot: 'C', mode: 'clear' };
	if (id === 'deck.1.hot_cue.D')
		return { kind: 'hot_cue', deck: 1, slot: 'D', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.D') return { kind: 'hot_cue', deck: 1, slot: 'D', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.D')
		return { kind: 'hot_cue', deck: 1, slot: 'D', mode: 'clear' };
	if (id === 'deck.1.hot_cue.E')
		return { kind: 'hot_cue', deck: 1, slot: 'E', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.E') return { kind: 'hot_cue', deck: 1, slot: 'E', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.E')
		return { kind: 'hot_cue', deck: 1, slot: 'E', mode: 'clear' };
	if (id === 'deck.1.hot_cue.F')
		return { kind: 'hot_cue', deck: 1, slot: 'F', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.F') return { kind: 'hot_cue', deck: 1, slot: 'F', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.F')
		return { kind: 'hot_cue', deck: 1, slot: 'F', mode: 'clear' };
	if (id === 'deck.1.hot_cue.G')
		return { kind: 'hot_cue', deck: 1, slot: 'G', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.G') return { kind: 'hot_cue', deck: 1, slot: 'G', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.G')
		return { kind: 'hot_cue', deck: 1, slot: 'G', mode: 'clear' };
	if (id === 'deck.1.hot_cue.H')
		return { kind: 'hot_cue', deck: 1, slot: 'H', mode: 'set_or_trigger' };
	if (id === 'deck.1.hot_cue_set.H') return { kind: 'hot_cue', deck: 1, slot: 'H', mode: 'set' };
	if (id === 'deck.1.hot_cue_clear.H')
		return { kind: 'hot_cue', deck: 1, slot: 'H', mode: 'clear' };
	if (id === 'deck.2.play') return { kind: 'midi', action: { type: 'deck_play_toggle', deck: 2 } };
	if (id === 'deck.2.cue') return { kind: 'midi', action: { type: 'deck_cue', deck: 2 } };
	if (id === 'deck.2.jump_start') return { kind: 'perf', deck: 2, op: 'seek_start' };
	if (id === 'deck.2.beat_sync')
		return { kind: 'midi', action: { type: 'deck_sync_toggle', deck: 2 } };
	if (id === 'deck.2.sync_master') return { kind: 'perf', deck: 2, op: 'master' };
	if (id === 'deck.2.keylock') return { kind: 'perf', deck: 2, op: 'keylock' };
	if (id === 'deck.2.tempo_down')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 2, direction: -1 } };
	if (id === 'deck.2.tempo_up')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 2, direction: 1 } };
	if (id === 'deck.2.loop_in') return { kind: 'perf', deck: 2, op: 'loop_in' };
	if (id === 'deck.2.loop_out') return { kind: 'perf', deck: 2, op: 'loop_out' };
	if (id === 'deck.2.reloop_exit') return { kind: 'perf', deck: 2, op: 'reloop_exit' };
	if (id === 'deck.2.loop_halve')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 2, factor: 0.5 } };
	if (id === 'deck.2.loop_double')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 2, factor: 2 } };
	if (id === 'deck.2.load') return { kind: 'midi', action: { type: 'browse_load', deck: 2 } };
	if (id === 'deck.2.instant_double') return { kind: 'perf', deck: 2, op: 'instant_double' };
	if (id === 'deck.2.hot_cue.A')
		return { kind: 'hot_cue', deck: 2, slot: 'A', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.A') return { kind: 'hot_cue', deck: 2, slot: 'A', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.A')
		return { kind: 'hot_cue', deck: 2, slot: 'A', mode: 'clear' };
	if (id === 'deck.2.hot_cue.B')
		return { kind: 'hot_cue', deck: 2, slot: 'B', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.B') return { kind: 'hot_cue', deck: 2, slot: 'B', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.B')
		return { kind: 'hot_cue', deck: 2, slot: 'B', mode: 'clear' };
	if (id === 'deck.2.hot_cue.C')
		return { kind: 'hot_cue', deck: 2, slot: 'C', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.C') return { kind: 'hot_cue', deck: 2, slot: 'C', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.C')
		return { kind: 'hot_cue', deck: 2, slot: 'C', mode: 'clear' };
	if (id === 'deck.2.hot_cue.D')
		return { kind: 'hot_cue', deck: 2, slot: 'D', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.D') return { kind: 'hot_cue', deck: 2, slot: 'D', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.D')
		return { kind: 'hot_cue', deck: 2, slot: 'D', mode: 'clear' };
	if (id === 'deck.2.hot_cue.E')
		return { kind: 'hot_cue', deck: 2, slot: 'E', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.E') return { kind: 'hot_cue', deck: 2, slot: 'E', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.E')
		return { kind: 'hot_cue', deck: 2, slot: 'E', mode: 'clear' };
	if (id === 'deck.2.hot_cue.F')
		return { kind: 'hot_cue', deck: 2, slot: 'F', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.F') return { kind: 'hot_cue', deck: 2, slot: 'F', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.F')
		return { kind: 'hot_cue', deck: 2, slot: 'F', mode: 'clear' };
	if (id === 'deck.2.hot_cue.G')
		return { kind: 'hot_cue', deck: 2, slot: 'G', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.G') return { kind: 'hot_cue', deck: 2, slot: 'G', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.G')
		return { kind: 'hot_cue', deck: 2, slot: 'G', mode: 'clear' };
	if (id === 'deck.2.hot_cue.H')
		return { kind: 'hot_cue', deck: 2, slot: 'H', mode: 'set_or_trigger' };
	if (id === 'deck.2.hot_cue_set.H') return { kind: 'hot_cue', deck: 2, slot: 'H', mode: 'set' };
	if (id === 'deck.2.hot_cue_clear.H')
		return { kind: 'hot_cue', deck: 2, slot: 'H', mode: 'clear' };
	if (id === 'deck.3.play') return { kind: 'midi', action: { type: 'deck_play_toggle', deck: 3 } };
	if (id === 'deck.3.cue') return { kind: 'midi', action: { type: 'deck_cue', deck: 3 } };
	if (id === 'deck.3.jump_start') return { kind: 'perf', deck: 3, op: 'seek_start' };
	if (id === 'deck.3.beat_sync')
		return { kind: 'midi', action: { type: 'deck_sync_toggle', deck: 3 } };
	if (id === 'deck.3.sync_master') return { kind: 'perf', deck: 3, op: 'master' };
	if (id === 'deck.3.keylock') return { kind: 'perf', deck: 3, op: 'keylock' };
	if (id === 'deck.3.tempo_down')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 3, direction: -1 } };
	if (id === 'deck.3.tempo_up')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 3, direction: 1 } };
	if (id === 'deck.3.loop_in') return { kind: 'perf', deck: 3, op: 'loop_in' };
	if (id === 'deck.3.loop_out') return { kind: 'perf', deck: 3, op: 'loop_out' };
	if (id === 'deck.3.reloop_exit') return { kind: 'perf', deck: 3, op: 'reloop_exit' };
	if (id === 'deck.3.loop_halve')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 3, factor: 0.5 } };
	if (id === 'deck.3.loop_double')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 3, factor: 2 } };
	if (id === 'deck.3.load') return { kind: 'midi', action: { type: 'browse_load', deck: 3 } };
	if (id === 'deck.3.instant_double') return { kind: 'perf', deck: 3, op: 'instant_double' };
	if (id === 'deck.3.hot_cue.A')
		return { kind: 'hot_cue', deck: 3, slot: 'A', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.A') return { kind: 'hot_cue', deck: 3, slot: 'A', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.A')
		return { kind: 'hot_cue', deck: 3, slot: 'A', mode: 'clear' };
	if (id === 'deck.3.hot_cue.B')
		return { kind: 'hot_cue', deck: 3, slot: 'B', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.B') return { kind: 'hot_cue', deck: 3, slot: 'B', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.B')
		return { kind: 'hot_cue', deck: 3, slot: 'B', mode: 'clear' };
	if (id === 'deck.3.hot_cue.C')
		return { kind: 'hot_cue', deck: 3, slot: 'C', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.C') return { kind: 'hot_cue', deck: 3, slot: 'C', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.C')
		return { kind: 'hot_cue', deck: 3, slot: 'C', mode: 'clear' };
	if (id === 'deck.3.hot_cue.D')
		return { kind: 'hot_cue', deck: 3, slot: 'D', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.D') return { kind: 'hot_cue', deck: 3, slot: 'D', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.D')
		return { kind: 'hot_cue', deck: 3, slot: 'D', mode: 'clear' };
	if (id === 'deck.3.hot_cue.E')
		return { kind: 'hot_cue', deck: 3, slot: 'E', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.E') return { kind: 'hot_cue', deck: 3, slot: 'E', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.E')
		return { kind: 'hot_cue', deck: 3, slot: 'E', mode: 'clear' };
	if (id === 'deck.3.hot_cue.F')
		return { kind: 'hot_cue', deck: 3, slot: 'F', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.F') return { kind: 'hot_cue', deck: 3, slot: 'F', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.F')
		return { kind: 'hot_cue', deck: 3, slot: 'F', mode: 'clear' };
	if (id === 'deck.3.hot_cue.G')
		return { kind: 'hot_cue', deck: 3, slot: 'G', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.G') return { kind: 'hot_cue', deck: 3, slot: 'G', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.G')
		return { kind: 'hot_cue', deck: 3, slot: 'G', mode: 'clear' };
	if (id === 'deck.3.hot_cue.H')
		return { kind: 'hot_cue', deck: 3, slot: 'H', mode: 'set_or_trigger' };
	if (id === 'deck.3.hot_cue_set.H') return { kind: 'hot_cue', deck: 3, slot: 'H', mode: 'set' };
	if (id === 'deck.3.hot_cue_clear.H')
		return { kind: 'hot_cue', deck: 3, slot: 'H', mode: 'clear' };
	if (id === 'deck.4.play') return { kind: 'midi', action: { type: 'deck_play_toggle', deck: 4 } };
	if (id === 'deck.4.cue') return { kind: 'midi', action: { type: 'deck_cue', deck: 4 } };
	if (id === 'deck.4.jump_start') return { kind: 'perf', deck: 4, op: 'seek_start' };
	if (id === 'deck.4.beat_sync')
		return { kind: 'midi', action: { type: 'deck_sync_toggle', deck: 4 } };
	if (id === 'deck.4.sync_master') return { kind: 'perf', deck: 4, op: 'master' };
	if (id === 'deck.4.keylock') return { kind: 'perf', deck: 4, op: 'keylock' };
	if (id === 'deck.4.tempo_down')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 4, direction: -1 } };
	if (id === 'deck.4.tempo_up')
		return { kind: 'midi', action: { type: 'deck_tempo_nudge', deck: 4, direction: 1 } };
	if (id === 'deck.4.loop_in') return { kind: 'perf', deck: 4, op: 'loop_in' };
	if (id === 'deck.4.loop_out') return { kind: 'perf', deck: 4, op: 'loop_out' };
	if (id === 'deck.4.reloop_exit') return { kind: 'perf', deck: 4, op: 'reloop_exit' };
	if (id === 'deck.4.loop_halve')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 4, factor: 0.5 } };
	if (id === 'deck.4.loop_double')
		return { kind: 'midi', action: { type: 'deck_loop_scale', deck: 4, factor: 2 } };
	if (id === 'deck.4.load') return { kind: 'midi', action: { type: 'browse_load', deck: 4 } };
	if (id === 'deck.4.instant_double') return { kind: 'perf', deck: 4, op: 'instant_double' };
	if (id === 'deck.4.hot_cue.A')
		return { kind: 'hot_cue', deck: 4, slot: 'A', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.A') return { kind: 'hot_cue', deck: 4, slot: 'A', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.A')
		return { kind: 'hot_cue', deck: 4, slot: 'A', mode: 'clear' };
	if (id === 'deck.4.hot_cue.B')
		return { kind: 'hot_cue', deck: 4, slot: 'B', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.B') return { kind: 'hot_cue', deck: 4, slot: 'B', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.B')
		return { kind: 'hot_cue', deck: 4, slot: 'B', mode: 'clear' };
	if (id === 'deck.4.hot_cue.C')
		return { kind: 'hot_cue', deck: 4, slot: 'C', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.C') return { kind: 'hot_cue', deck: 4, slot: 'C', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.C')
		return { kind: 'hot_cue', deck: 4, slot: 'C', mode: 'clear' };
	if (id === 'deck.4.hot_cue.D')
		return { kind: 'hot_cue', deck: 4, slot: 'D', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.D') return { kind: 'hot_cue', deck: 4, slot: 'D', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.D')
		return { kind: 'hot_cue', deck: 4, slot: 'D', mode: 'clear' };
	if (id === 'deck.4.hot_cue.E')
		return { kind: 'hot_cue', deck: 4, slot: 'E', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.E') return { kind: 'hot_cue', deck: 4, slot: 'E', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.E')
		return { kind: 'hot_cue', deck: 4, slot: 'E', mode: 'clear' };
	if (id === 'deck.4.hot_cue.F')
		return { kind: 'hot_cue', deck: 4, slot: 'F', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.F') return { kind: 'hot_cue', deck: 4, slot: 'F', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.F')
		return { kind: 'hot_cue', deck: 4, slot: 'F', mode: 'clear' };
	if (id === 'deck.4.hot_cue.G')
		return { kind: 'hot_cue', deck: 4, slot: 'G', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.G') return { kind: 'hot_cue', deck: 4, slot: 'G', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.G')
		return { kind: 'hot_cue', deck: 4, slot: 'G', mode: 'clear' };
	if (id === 'deck.4.hot_cue.H')
		return { kind: 'hot_cue', deck: 4, slot: 'H', mode: 'set_or_trigger' };
	if (id === 'deck.4.hot_cue_set.H') return { kind: 'hot_cue', deck: 4, slot: 'H', mode: 'set' };
	if (id === 'deck.4.hot_cue_clear.H')
		return { kind: 'hot_cue', deck: 4, slot: 'H', mode: 'clear' };
	if (id === 'mixer.fader_up.1') return { kind: 'mixer', target: 'fader', deck: 1, delta: 0.1 };
	if (id === 'mixer.fader_down.1') return { kind: 'mixer', target: 'fader', deck: 1, delta: -0.1 };
	if (id === 'mixer.eq_low_up.1')
		return { kind: 'mixer', target: 'eq', deck: 1, band: 'low', delta: 0.1 };
	if (id === 'mixer.eq_low_down.1')
		return { kind: 'mixer', target: 'eq', deck: 1, band: 'low', delta: -0.1 };
	if (id === 'mixer.eq_mid_up.1')
		return { kind: 'mixer', target: 'eq', deck: 1, band: 'mid', delta: 0.1 };
	if (id === 'mixer.eq_mid_down.1')
		return { kind: 'mixer', target: 'eq', deck: 1, band: 'mid', delta: -0.1 };
	if (id === 'mixer.eq_high_up.1')
		return { kind: 'mixer', target: 'eq', deck: 1, band: 'high', delta: 0.1 };
	if (id === 'mixer.eq_high_down.1')
		return { kind: 'mixer', target: 'eq', deck: 1, band: 'high', delta: -0.1 };
	if (id === 'mixer.fader_up.2') return { kind: 'mixer', target: 'fader', deck: 2, delta: 0.1 };
	if (id === 'mixer.fader_down.2') return { kind: 'mixer', target: 'fader', deck: 2, delta: -0.1 };
	if (id === 'mixer.eq_low_up.2')
		return { kind: 'mixer', target: 'eq', deck: 2, band: 'low', delta: 0.1 };
	if (id === 'mixer.eq_low_down.2')
		return { kind: 'mixer', target: 'eq', deck: 2, band: 'low', delta: -0.1 };
	if (id === 'mixer.eq_mid_up.2')
		return { kind: 'mixer', target: 'eq', deck: 2, band: 'mid', delta: 0.1 };
	if (id === 'mixer.eq_mid_down.2')
		return { kind: 'mixer', target: 'eq', deck: 2, band: 'mid', delta: -0.1 };
	if (id === 'mixer.eq_high_up.2')
		return { kind: 'mixer', target: 'eq', deck: 2, band: 'high', delta: 0.1 };
	if (id === 'mixer.eq_high_down.2')
		return { kind: 'mixer', target: 'eq', deck: 2, band: 'high', delta: -0.1 };
	if (id === 'mixer.fader_up.3') return { kind: 'mixer', target: 'fader', deck: 3, delta: 0.1 };
	if (id === 'mixer.fader_down.3') return { kind: 'mixer', target: 'fader', deck: 3, delta: -0.1 };
	if (id === 'mixer.eq_low_up.3')
		return { kind: 'mixer', target: 'eq', deck: 3, band: 'low', delta: 0.1 };
	if (id === 'mixer.eq_low_down.3')
		return { kind: 'mixer', target: 'eq', deck: 3, band: 'low', delta: -0.1 };
	if (id === 'mixer.eq_mid_up.3')
		return { kind: 'mixer', target: 'eq', deck: 3, band: 'mid', delta: 0.1 };
	if (id === 'mixer.eq_mid_down.3')
		return { kind: 'mixer', target: 'eq', deck: 3, band: 'mid', delta: -0.1 };
	if (id === 'mixer.eq_high_up.3')
		return { kind: 'mixer', target: 'eq', deck: 3, band: 'high', delta: 0.1 };
	if (id === 'mixer.eq_high_down.3')
		return { kind: 'mixer', target: 'eq', deck: 3, band: 'high', delta: -0.1 };
	if (id === 'mixer.fader_up.4') return { kind: 'mixer', target: 'fader', deck: 4, delta: 0.1 };
	if (id === 'mixer.fader_down.4') return { kind: 'mixer', target: 'fader', deck: 4, delta: -0.1 };
	if (id === 'mixer.eq_low_up.4')
		return { kind: 'mixer', target: 'eq', deck: 4, band: 'low', delta: 0.1 };
	if (id === 'mixer.eq_low_down.4')
		return { kind: 'mixer', target: 'eq', deck: 4, band: 'low', delta: -0.1 };
	if (id === 'mixer.eq_mid_up.4')
		return { kind: 'mixer', target: 'eq', deck: 4, band: 'mid', delta: 0.1 };
	if (id === 'mixer.eq_mid_down.4')
		return { kind: 'mixer', target: 'eq', deck: 4, band: 'mid', delta: -0.1 };
	if (id === 'mixer.eq_high_up.4')
		return { kind: 'mixer', target: 'eq', deck: 4, band: 'high', delta: 0.1 };
	if (id === 'mixer.eq_high_down.4')
		return { kind: 'mixer', target: 'eq', deck: 4, band: 'high', delta: -0.1 };
	if (id === 'mixer.crossfader_left') return { kind: 'mixer', target: 'crossfader', delta: -0.1 };
	if (id === 'mixer.crossfader_right') return { kind: 'mixer', target: 'crossfader', delta: 0.1 };
	return null;
}
