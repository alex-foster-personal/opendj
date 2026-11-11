import type { PerformanceCommand } from './performance-ipc-types';
import type { EqBand } from './mixer-types';
import { STEM_CONTROL_IDS, type StemControl } from './stem-types';

const DECKS = [1, 2, 3, 4] as const;
const EQ_BANDS: readonly EqBand[] = ['low', 'mid', 'high'];

function deckValid(deck: number): deck is 1 | 2 | 3 | 4 {
	return Number.isInteger(deck) && deck >= 1 && deck <= 4;
}

/** SETREC-12067: the dotted control id (apps/sets/control_ids.py) a command drives, or null. */
export function replayControlId(command: PerformanceCommand): string | null {
	switch (command.type) {
		case 'trim':
			return deckValid(command.deck) ? `deck.${command.deck}.trim` : null;
		case 'auto_gain_offset':
			return deckValid(command.deck) ? `deck.${command.deck}.auto_gain` : null;
		case 'eq': {
			if (!deckValid(command.deck)) return null;
			const band = command.band;
			if (band !== 'low' && band !== 'mid' && band !== 'high') return null;
			return `deck.${command.deck}.eq.${band}`;
		}
		case 'filter':
			return deckValid(command.deck) ? `deck.${command.deck}.filter` : null;
		case 'fader':
			return deckValid(command.deck) ? `deck.${command.deck}.fader` : null;
		case 'assign':
			return deckValid(command.deck) ? `deck.${command.deck}.assign` : null;
		case 'crossfader':
			return 'mixer.crossfader';
		case 'master_volume':
			return 'mixer.master_volume';
		case 'master_mute':
			return 'mixer.master_mute';
		case 'tempo':
			return deckValid(command.deck) ? `deck.${command.deck}.tempo` : null;
		case 'stem_gain': {
			if (!deckValid(command.deck)) return null;
			const stem = command.stem;
			if (!STEM_CONTROL_IDS.includes(stem as StemControl)) return null;
			return `deck.${command.deck}.stem.${stem}.gain`;
		}
		default:
			return null;
	}
}

function buildReplayControlIds(): string[] {
	const ids: string[] = [];
	for (const deck of DECKS) {
		ids.push(
			`deck.${deck}.trim`,
			`deck.${deck}.auto_gain`,
			`deck.${deck}.filter`,
			`deck.${deck}.fader`
		);
		ids.push(`deck.${deck}.assign`, `deck.${deck}.tempo`);
		for (const band of EQ_BANDS) ids.push(`deck.${deck}.eq.${band}`);
		for (const stem of STEM_CONTROL_IDS) ids.push(`deck.${deck}.stem.${stem}.gain`);
	}
	ids.push('mixer.crossfader', 'mixer.master_volume', 'mixer.master_mute');
	return ids;
}

/** Every id replayControlId can return, for validating a CLI/MCP take-over target. */
export const REPLAY_CONTROL_IDS: readonly string[] = Object.freeze(buildReplayControlIds());

export function isReplayControlId(value: string): boolean {
	return (REPLAY_CONTROL_IDS as readonly string[]).includes(value);
}
