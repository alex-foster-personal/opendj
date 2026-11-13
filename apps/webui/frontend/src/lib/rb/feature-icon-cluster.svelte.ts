/**
 * Performance top-bar feature icons (pin 87d7dac0a9b8).
 *
 * The row toggles components on and off. By default the whole strip is one
 * button (a menu lists every component). `feature_icon_single_click` makes
 * each icon its own click. Core icons stay on row 1 in the top bar; the
 * rest fill row 2, then row 3. The core set is the controls the bar already
 * carried before the strip (list view, FX, 2-deck, 4-waveform); the phase
 * scope sits on row 2 and the phase meter on row 3 (decided Wed 28 Oct 2026,
 * PR #6228, from the pin's row order). A new component is added to a row here.
 *
 * The single-click choice is per browser (localStorage), not a library pref.
 * The four view icons are live (TOPVIEW-01..03): they are one choice,
 * uiPrefs.perf_view, so this module only maps each icon to its view and the
 * top bar reads and sets the pref. FX opens the beat FX panel (BEATFX-14) once
 * `fx.beat_rack` is on; with it off its icon and menu entry render inert with
 * the planned-control tooltip and toggle nothing.
 */

import { reportSettingSaveError } from '$lib/settings/setting-save-errors';
import type { PerfView } from './topbar-view-prefs';

const SINGLE_CLICK_KEY = 'mdt.rb.feature-icon-single-click';

export type PlannedFeatureIconId = 'fx';

export type ViewFeatureIconId = 'list-view' | '4-waveform' | 'scope-1' | 'scope-2';

export type FeatureIconId = PlannedFeatureIconId | ViewFeatureIconId | '2-deck';

/** The perf_view each live view icon selects. */
export const FEATURE_ICON_VIEW: Readonly<Record<ViewFeatureIconId, PerfView>> = {
	'list-view': 'list',
	'4-waveform': 'waveform',
	'scope-1': 'phase-scope',
	'scope-2': 'phase-meter'
};

export function featureIconView(id: FeatureIconId): PerfView | null {
	return Object.hasOwn(FEATURE_ICON_VIEW, id) ? FEATURE_ICON_VIEW[id as ViewFeatureIconId] : null;
}

export interface FeatureMenuIcon {
	id: FeatureIconId;
	short: string;
}

export interface FeatureMenuRow {
	row: 1 | 2 | 3;
	label: string;
	icons: readonly FeatureMenuIcon[];
}

/** Row 1 is the top bar. Rows 2 and 3 are the overflow under it. */
export const FEATURE_ICON_ROWS: readonly FeatureMenuRow[] = [
	{
		row: 1,
		label: 'Top bar',
		icons: [
			{ id: 'list-view', short: 'List' },
			{ id: 'fx', short: 'FX' },
			{ id: '2-deck', short: '2-deck' },
			{ id: '4-waveform', short: '4-wave' }
		]
	},
	{
		row: 2,
		label: 'Row 2',
		icons: [{ id: 'scope-1', short: 'Scope' }]
	},
	{
		row: 3,
		label: 'Row 3',
		icons: [{ id: 'scope-2', short: 'Meter' }]
	}
];

// Server-side evaluation has no window and nothing to report. A browser
// without the storage API is a real failure and is reported (Sol P1, PR #6228).
function _storageMissingInBrowser(): boolean {
	return typeof window !== 'undefined' && typeof localStorage === 'undefined';
}

function _readSingleClick(): boolean {
	try {
		if (_storageMissingInBrowser()) {
			reportSettingSaveError(
				'Feature icon click mode cannot be read: this browser has no storage; showing the one-button strip',
				new Error('localStorage is unavailable')
			);
			return false;
		}
		if (typeof localStorage === 'undefined') return false;
		const stored = localStorage.getItem(SINGLE_CLICK_KEY);
		if (stored === null || stored === '0') return false;
		if (stored === '1') return true;
		// A value this module never writes is reported, not read as off (Sol P1, PR #6228).
		reportSettingSaveError(
			`Feature icon click mode in this browser's storage is ${JSON.stringify(stored)}, not 0 or 1; showing the one-button strip`,
			new Error(`${SINGLE_CLICK_KEY} must be "0" or "1"`)
		);
		return false;
	} catch (exc) {
		reportSettingSaveError(
			"Feature icon click mode could not be read from this browser's storage; showing the one-button strip",
			exc
		);
		return false;
	}
}

let singleClick = $state(_readSingleClick());

export function featureIconSingleClick(): boolean {
	return singleClick;
}

export function setFeatureIconSingleClick(next: boolean): void {
	if (typeof next !== 'boolean') {
		throw new Error(`feature_icon_single_click must be a boolean, got ${String(next)}`);
	}
	singleClick = next;
	try {
		if (_storageMissingInBrowser()) {
			reportSettingSaveError(
				'Feature icon click mode cannot be saved: this browser has no storage; it applies until you reload',
				new Error('localStorage is unavailable')
			);
			return;
		}
		if (typeof localStorage === 'undefined') return;
		localStorage.setItem(SINGLE_CLICK_KEY, next ? '1' : '0');
	} catch (exc) {
		// Private mode or a full quota: the choice applies, but only until reload.
		reportSettingSaveError(
			"Feature icon click mode could not be saved in this browser's storage; it applies until you reload",
			exc
		);
	}
}
