import {
	LAYOUT_SURFACES,
	layoutPresetChoices,
	type LibraryLayout,
	type PerformanceLayout
} from '$lib/rb/skin-layout';
import type { SettingDef } from './catalog-types';

const PERFORMANCE_LAYOUT_LABELS: Record<PerformanceLayout, string> = {
	'deck-grid': 'Deck grid',
	'replica-desk': 'Replica desk',
	'library-first': 'Library first'
};

const LIBRARY_LAYOUT_LABELS: Record<LibraryLayout, string> = {
	'crate-desk': 'Crate desk',
	'three-panel': 'Three panel'
};

function _layoutEnumOptions(surface: (typeof LAYOUT_SURFACES)[number]): Extract<SettingDef['control'], { kind: 'enum' }> {
	const choices = layoutPresetChoices(surface);
	return {
		kind: 'enum',
		options: choices.map((value) => ({
			value,
			label:
				value === 'auto'
					? 'Auto (follows skin)'
					: surface === 'performance'
						? PERFORMANCE_LAYOUT_LABELS[value as PerformanceLayout]
						: LIBRARY_LAYOUT_LABELS[value as LibraryLayout]
		}))
	};
}

/** Issue #4219: waveform band palette; Auto (follow the skin) is the default. */
export const WAVE_PALETTE_SETTING: SettingDef = {
	id: 'wave_palette',
	label: 'Waveform colors',
	group: 'performance',
	keywords: ['waveform', 'color', 'colors', 'palette', 'auto', 'skin', 'band', '3band', 'rekordbox', 'cdj', 'legacy', 'blue', 'classic', 'orange', 'amber'],
	title: 'Waveform frequency-band colors',
	detail:
		'Auto (default) follows the skin: Gothic uses Mono grayscale, Default and Light use rekordbox 3Band. rekordbox 3Band matches the CDJ: dark blue lows, amber mids, white highs. Legacy is the earlier Open DJ palette: orange lows, blue mids, near-white highs. Blue is the classic single-blue waveform, brighter at the peaks. Applies to the deck waveforms and the overview and preview strips, in both light and dark themes.',
	implemented: true,
	preview: 'waveform',
	control: {
		kind: 'enum',
		options: [
			{ value: 'auto', label: 'Auto (follows skin)' },
			{ value: 'rekordbox', label: 'rekordbox 3Band' },
			{ value: 'legacy', label: 'Legacy (orange lows)' },
			{ value: 'mono', label: 'Mono grayscale' },
			{ value: 'blue', label: 'Blue (classic)' }
		]
	}
};

/** Preview skin: near-black grayscale chrome (html[data-skin='mono-dev']). */
export const UI_SKIN_SETTING: SettingDef = {
	id: 'ui_skin',
	label: 'UI skin',
	group: 'performance',
	keywords: ['skin', 'theme', 'light', 'gothic', 'mono', 'grayscale', 'gray', 'minimal', 'dev', 'green', 'green room', 'rounded', 'streaming', 'practice', 'controller', 'hardware'],
	title: 'Chrome skin layered over the light/dark theme',
	detail:
		'Gothic (default) is a near-black grayscale skin with hairline borders, square controls and monospace type. Default keeps the rekordbox-style chrome. Light is the warm light skin. Green Room is a near-black skin with a green accent, rounded panels, pill controls and a geometric sans. Practice deck is the dark schematic palette of the hardware practice replica (open /practice/ddj-flx4). The top-bar skin button cycles Default, Gothic, Light, Green Room, Practice deck. With Waveform design and Waveform colors on Auto, the waveforms follow the skin.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'default', label: 'Default' },
			{ value: 'mono-dev', label: 'Gothic (mono dev preview)' },
			{ value: 'light', label: 'Light' },
			{ value: 'green-room', label: 'Green Room' },
			{ value: 'practice-ddj-flx4', label: 'Practice deck' }
		]
	}
};

/** Issue #10199: override the performance surface layout preset, or Auto follows the skin. */
export const PERFORMANCE_LAYOUT_SETTING: SettingDef = {
	id: 'layout_override.performance',
	label: 'Performance layout',
	group: 'performance',
	keywords: ['layout', 'preset', 'skin', 'arrangement', 'panel', 'performance', 'deck', 'grid'],
	title: 'Performance surface layout preset',
	detail:
		"Auto (default) follows the skin's SKIN_LAYOUT entry. An explicit preset wins on every skin. Same path as the set_layout_preset command (HTTP POST /api/v1/commands, opendj set_layout_preset).",
	implemented: true,
	control: _layoutEnumOptions('performance')
};

/** Issue #10199: override the library surface layout preset, or Auto follows the skin. */
export const LIBRARY_LAYOUT_SETTING: SettingDef = {
	id: 'layout_override.library',
	label: 'Library layout',
	group: 'performance',
	keywords: ['layout', 'preset', 'skin', 'arrangement', 'panel', 'library', 'crate', 'three'],
	title: 'Library surface layout preset',
	detail:
		"Auto (default) follows the skin's SKIN_LAYOUT entry. An explicit preset wins on every skin. Same path as the set_layout_preset command (HTTP POST /api/v1/commands, opendj set_layout_preset).",
	implemented: true,
	control: _layoutEnumOptions('library')
};

/** SKINLAYOUT-10207: where the decks sit under the library-first preset. */
export const LIBRARY_FIRST_VARIANT_SETTING: SettingDef = {
	id: 'layout_variant.library_first',
	label: 'Library-first look',
	group: 'performance',
	keywords: ['layout', 'library', 'prep', 'variant', 'decks', 'bottom', 'top'],
	title: 'Library-first preset: decks below or above the library',
	detail:
		'Applies when the performance layout is Library first. Decks at the bottom (default) keep the library on top; decks at the top put the decks and waves in a band under the top bar.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'decks-bottom', label: 'Decks at the bottom' },
			{ value: 'decks-top', label: 'Decks at the top' }
		]
	}
};

/** Split main waveform: master deck on top, this deck below. Opt-in. */
export const WAVE_SPLIT_MASTER_SETTING: SettingDef = {
	id: 'wave_split_master',
	label: 'Split waveform (master on top)',
	group: 'performance',
	keywords: ['split', 'master', 'waveform', 'beat', 'matching', 'phase', 'wavestack'],
	title: 'Deck waveform rows: master on the top half, this deck on the bottom half',
	detail:
		'Opt-in, intended for a future decks-only view. Off (and Auto, on every skin) paints the standard mirrored waveform. On: each deck row paints the MASTER deck above the centerline and this deck below it, each on its own playhead and tempo, so in-phase beats meet at the line; the master deck keeps its own mirrored waveform. Same path as the set_skin performance command.',
	implemented: true,
	control: {
		kind: 'enum',
		options: [
			{ value: 'auto', label: 'Auto (off)' },
			{ value: 'on', label: 'On' },
			{ value: 'off', label: 'Off' }
		]
	}
};
