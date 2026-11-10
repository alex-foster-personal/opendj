/**
 * SKINLAYOUT-8869, SKINLAYOUT-10489, SKINLAYOUT-11729: the LAYOUT part of a skin manifest ((private path)
 * section 2, ADR-0136). A skin names one preset per surface from a fixed
 * catalog; the library workspace grid and the /performance root read it as `data-layout`
 * and place the same components in different grid areas. No component forks per skin.
 *
 * Requirements (mini-PRD):
 *   ? SKIN_LAYOUT: every skin declares a library preset; only Green Room bends.
 *     [if] libraryLayoutFor('mono-dev') is not 'crate-desk' [then ⛔️]
 *     [if] libraryLayoutFor('green-room') is not 'three-panel' [then ⛔️]
 *   ✔︎ ✅ 🎯 SKIN_LAYOUT.performance: every skin declares deck-grid except practice-ddj-flx4, which declares replica-desk; each preset has a PERFORMANCE_LAYOUT_SPEC.
 *     [if] performanceLayoutFor('green-room') is not 'deck-grid' [then ⛔️]
 *     [if] performanceLayoutFor('default'|'mono-dev'|'light'|'green-room') is not 'deck-grid' [then ⛔️]
 *     [if] performanceLayoutFor('practice-ddj-flx4') is not 'replica-desk' [then ⛔️]
 *     [if] the deck-grid spec's areas differ from the .perf-root grid-template-areas [then ⛔️]
 *   ? parseLibraryLayout: unknown preset ids throw.
 *     [if] parseLibraryLayout('sidebar-left') returns instead of throwing [then ⛔️]
 *   ✔︎ ✅ 🎯 parsePerformanceLayout: unknown preset ids throw, naming the allowed list.
 *     [if] parsePerformanceLayout('four-across') returns instead of throwing [then ⛔️]
 *     [if] parsePerformanceLayout('sidebar-left') returns instead of throwing with the allowed ids [then ⛔️]
 *     [if] parsePerformanceLayout('replica-desk') does not return 'replica-desk' [then ⛔️]
 *   ? effectivePerformanceLayout / effectiveLibraryLayout: 'auto' follows the skin.
 *     [if] effectiveLibraryLayout('auto', 'green-room') is not the skin entry [then ⛔️]
 *     [if] an explicit preset does not win over the skin default [then ⛔️]
 *   ? parseLayoutOverride: unknown surface, preset or keys throw with the allowed list.
 *     [if] parseLayoutOverride clamps or defaults a bad preset [then ⛔️]
 *   ? nowPlayingId: master deck from the last Gig session, then the preview, then the selection.
 *     [if] a stored master deck with a track loses to the preview [then ⛔️]
 *   ? nextInQueueId: AutoPlay's first charted pick that is not the track now playing.
 *     [if] the now-playing id is shown as its own next pick [then ⛔️]
 *   ? aboutFacts: only fields the track already holds, empty fields dropped.
 *     [if] a null album renders as an About row [then ⛔️]
 *   ✔︎ ✅ 🎯 PERFORMANCE_LAYOUT_SPEC: every performance preset declares floors and a minWindow (#10201).
 *     [if] a preset's topbar + waverows + deck row exceed 800 px [then ⛔️]
 *   ✔︎ ✅ 🎯 presetFitsWindow: refuses a window shorter than the preset needs, with the reason.
 *     [if] the reason omits the preset id, window height or needed height [then ⛔️]
 *     [if] a NaN window height returns instead of throwing [then ⛔️]
 *   ✔︎ ✅ 🎯 presetContractOverflows: names the preset and the px over for every failing check.
 *     [if] a 900 px deck row fixture passes at 1280x800 [then ⛔️]
 */

import {
	COMPACT_WAVESTACK_BELOW_H_PX,
	TOPBAR_H_PX,
	WAVEROW_H_PX_COMPACT,
	WAVEROW_H_PX_TALLEST
} from './theme-tokens';
import type { UiSkin } from './ui-skin';

/** Performance surface presets. A new preset is a new grid map on /performance, never a forked page. */
export type PerformanceLayout = 'deck-grid' | 'replica-desk';

export const PERFORMANCE_LAYOUTS: readonly PerformanceLayout[] = ['deck-grid', 'replica-desk'];

/** Library surface presets. A new preset is a new grid map in CrateDeskView, never a new component. */
export type LibraryLayout = 'crate-desk' | 'three-panel';

export const LIBRARY_LAYOUTS: readonly LibraryLayout[] = ['crate-desk', 'three-panel'];

/** The manifest's `layout` block. Library and performance have presets;
 * `mixer` joins here when its first preset ships. */
export type SkinLayout = Readonly<{ library: LibraryLayout; performance: PerformanceLayout }>;

/** One entry per skin, so a new skin without a layout fails the type check. */
export const SKIN_LAYOUT: Readonly<Record<UiSkin, SkinLayout>> = {
	default: { library: 'crate-desk', performance: 'deck-grid' },
	'mono-dev': { library: 'crate-desk', performance: 'deck-grid' },
	light: { library: 'crate-desk', performance: 'deck-grid' },
	'green-room': { library: 'three-panel', performance: 'deck-grid' },
	// Hardware practice skin: the library keeps the standard desk; the performance route mounts the
	// replica desk when the preset is replica-desk (ADR-NEW-skin-performance-layout-preset).
	'practice-ddj-flx4': { library: 'crate-desk', performance: 'replica-desk' }
};

export type LayoutSurface = 'performance' | 'library';
export const LAYOUT_SURFACES: readonly LayoutSurface[] = ['performance', 'library'];

export type LayoutOverride = Readonly<{
	performance: 'auto' | PerformanceLayout;
	library: 'auto' | LibraryLayout;
}>;
export const LAYOUT_OVERRIDE_DEFAULT: LayoutOverride = { performance: 'auto', library: 'auto' };

/** Allowed values per surface, 'auto' first. Used by the refusal text and the Settings rows. */
export function layoutPresetChoices(surface: LayoutSurface): readonly string[] {
	return surface === 'performance'
		? (['auto', ...PERFORMANCE_LAYOUTS] as const)
		: (['auto', ...LIBRARY_LAYOUTS] as const);
}

export function parseLayoutSurface(raw: unknown): LayoutSurface {
	if (!LAYOUT_SURFACES.includes(raw as LayoutSurface)) {
		throw new Error(`layout surface must be ${LAYOUT_SURFACES.join('|')}, got ${String(raw)}`);
	}
	return raw as LayoutSurface;
}

export function parseLayoutPreset(
	surface: 'performance',
	raw: unknown
): LayoutOverride['performance'];
export function parseLayoutPreset(surface: 'library', raw: unknown): LayoutOverride['library'];
export function parseLayoutPreset(
	surface: LayoutSurface,
	raw: unknown
): LayoutOverride[LayoutSurface];
export function parseLayoutPreset(
	surface: LayoutSurface,
	raw: unknown
): LayoutOverride[LayoutSurface] {
	const allowed = layoutPresetChoices(surface);
	if (!allowed.includes(String(raw))) {
		throw new Error(`${surface} layout must be ${allowed.join('|')}, got ${String(raw)}`);
	}
	return raw as LayoutOverride[LayoutSurface];
}

export function parseLayoutOverride(raw: unknown): LayoutOverride {
	if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) {
		throw new Error('layout_override must be an object');
	}
	const obj = raw as Record<string, unknown>;
	const keys = Object.keys(obj);
	const expected = ['performance', 'library'] as const;
	for (const k of expected) {
		if (!keys.includes(k)) throw new Error(`layout_override missing key ${k}`);
	}
	for (const k of keys) {
		if (!expected.includes(k as (typeof expected)[number])) {
			throw new Error(`layout surface must be ${LAYOUT_SURFACES.join('|')}, got ${k}`);
		}
	}
	return {
		performance: parseLayoutPreset('performance', obj.performance),
		library: parseLayoutPreset('library', obj.library)
	};
}

export function effectivePerformanceLayout(
	pref: LayoutOverride['performance'],
	skin: UiSkin
): PerformanceLayout {
	return pref === 'auto' ? performanceLayoutFor(skin) : pref;
}

export function effectiveLibraryLayout(
	pref: LayoutOverride['library'],
	skin: UiSkin
): LibraryLayout {
	return pref === 'auto' ? libraryLayoutFor(skin) : pref;
}

export function libraryLayoutFor(skin: UiSkin): LibraryLayout {
	const layout = SKIN_LAYOUT[skin];
	if (layout === undefined) throw new Error(`no layout declared for skin ${String(skin)}`);
	return layout.library;
}

export function parseLibraryLayout(raw: unknown): LibraryLayout {
	if (!LIBRARY_LAYOUTS.includes(raw as LibraryLayout)) {
		throw new Error(`library layout must be ${LIBRARY_LAYOUTS.join('|')}, got ${String(raw)}`);
	}
	return raw as LibraryLayout;
}

export function performanceLayoutFor(skin: UiSkin): PerformanceLayout {
	const layout = SKIN_LAYOUT[skin];
	if (layout === undefined) throw new Error(`no layout declared for skin ${String(skin)}`);
	return layout.performance;
}

export function parsePerformanceLayout(raw: unknown): PerformanceLayout {
	if (!PERFORMANCE_LAYOUTS.includes(raw as PerformanceLayout)) {
		throw new Error(
			`performance layout must be ${PERFORMANCE_LAYOUTS.join('|')}, got ${String(raw)}`
		);
	}
	return raw as PerformanceLayout;
}

//----- Performance surface presets (LS-1 table shape, #10196; window-minimum contract, #10201)
//
// PLAN.md 2.4 rule 5: every preset must solve at 1280x800 and at 2035x1449.
// Clip need at window height H = topbar + rows * waverowPxAt(H) + deck row
// (`.rb-deck` is overflow: hidden, so below this the deck controls clip).
// The library floor only costs visible rows when short, so it joins the sum at 2035x1449.
//   deck-grid at 1280x800:  28 + 4 x 56 + 497 = 749 <= 800
//   deck-grid at 1280x720:  28 + 4 x 22 + 497 = 613 <= 720 (compact rows below 800, PERF-UI-01)
//   deck-grid at 2035x1449: 749 + 272 library = 1021 <= 1449; with Default 43 px rows = 969 (LIBUX-01)
// Known gap, out of model: stem-waveform rows (DECKUX-19) add 36 px per row and overflow 800 px.

export type WindowSize = Readonly<{ w: number; h: number }>;

export type PerformanceLayoutSpec = Readonly<{
	/** string, not PerformanceLayout, so a test fixture can name itself. */
	id: string;
	/** grid-template-areas rows, top to bottom. */
	areas: readonly string[];
	honours: Readonly<{ deckLayout: boolean; perfView: boolean; pads: boolean }>;
	floors: Readonly<{ wavestackRows: number; deckRowPx: number; libraryPx: number }>;
	/** Smallest window this preset is verified to hold. */
	minWindow: WindowSize;
}>;

export const PERFORMANCE_LAYOUT_SPEC: Readonly<Record<PerformanceLayout, PerformanceLayoutSpec>> = {
	'deck-grid': {
		id: 'deck-grid',
		areas: ['topbar', 'wavestack', 'deckarea', 'browser'],
		honours: { deckLayout: true, perfView: true, pads: true },
		floors: { wavestackRows: 4, deckRowPx: 497, libraryPx: 272 },
		minWindow: { w: 1280, h: 720 }
	},
	// Provisional floors (#10271): the replica takes the deck row's slot, so it keeps deck-grid's
	// envelope until the layout PR (#9646) sets them after the composition review.
	'replica-desk': {
		id: 'replica-desk',
		areas: ['topbar', 'wavestack', 'replica', 'browser'],
		honours: { deckLayout: false, perfView: true, pads: false },
		floors: { wavestackRows: 4, deckRowPx: 497, libraryPx: 272 },
		minWindow: { w: 1280, h: 720 }
	}
};

/** The two window sizes every preset must hold (PLAN.md 2.4 rule 5). */
export const CONTRACT_WINDOWS: readonly WindowSize[] = [
	{ w: 1280, h: 800 },
	{ w: 2035, h: 1449 }
];

/** Waverow height the /performance page uses at this window height. */
export function waverowPxAt(windowH: number): number {
	return windowH < COMPACT_WAVESTACK_BELOW_H_PX ? WAVEROW_H_PX_COMPACT : WAVEROW_H_PX_TALLEST;
}

/** Height the preset needs so no deck control clips: topbar + waverows + deck row. */
export function presetClipNeedPx(spec: PerformanceLayoutSpec, windowH: number): number {
	return TOPBAR_H_PX + spec.floors.wavestackRows * waverowPxAt(windowH) + spec.floors.deckRowPx;
}

/** Clip need plus the library floor, at a given waverow height. */
export function presetComfortNeedPx(spec: PerformanceLayoutSpec, waverowPx: number): number {
	return (
		TOPBAR_H_PX +
		spec.floors.wavestackRows * waverowPx +
		spec.floors.deckRowPx +
		spec.floors.libraryPx
	);
}

export type PresetFit = { ok: true } | { ok: false; reason: string };

function _assertWindowSize(size: WindowSize): void {
	for (const [k, v] of [
		['w', size.w],
		['h', size.h]
	] as const) {
		if (!Number.isFinite(v) || v <= 0)
			throw new Error(`window ${k} must be a positive number, got ${String(v)}`);
	}
}

/** Whether a window can hold the preset; on refusal the reason names preset, window and needed size. */
export function presetFitsWindow(spec: PerformanceLayoutSpec, size: WindowSize): PresetFit {
	_assertWindowSize(size);
	const needH = Math.max(spec.minWindow.h, presetClipNeedPx(spec, size.h));
	if (size.h < needH) {
		return {
			ok: false,
			reason:
				`layout preset ${spec.id} needs a window at least ${needH} px tall; ` +
				`this window is ${size.h} px tall (${needH - size.h} px short)`
		};
	}
	if (size.w < spec.minWindow.w) {
		return {
			ok: false,
			reason:
				`layout preset ${spec.id} needs a window at least ${spec.minWindow.w} px wide; ` +
				`this window is ${size.w} px wide (${spec.minWindow.w - size.w} px short)`
		};
	}
	return { ok: true };
}

/** Every way the preset breaks the window-minimum contract; empty when it holds. */
export function presetContractOverflows(spec: PerformanceLayoutSpec): string[] {
	const out: string[] = [];
	const { wavestackRows: rows, deckRowPx, libraryPx } = spec.floors;
	for (const { w, h } of [...CONTRACT_WINDOWS, spec.minWindow]) {
		const need = presetClipNeedPx(spec, h);
		if (need > h) {
			out.push(
				`preset ${spec.id} at ${w}x${h}: needs ${need} px (${TOPBAR_H_PX} px topbar + ${rows} x ` +
					`${waverowPxAt(h)} px waverows + ${deckRowPx} px deck row), window is ${h} px: ` +
					`${need - h} px over`
			);
		}
	}
	const [small, large] = CONTRACT_WINDOWS;
	if (spec.minWindow.w > small.w || spec.minWindow.h > small.h) {
		out.push(
			`preset ${spec.id} declares minWindow ${spec.minWindow.w}x${spec.minWindow.h}, ` +
				`larger than the ${small.w}x${small.h} contract window`
		);
	}
	const comfort = presetComfortNeedPx(spec, WAVEROW_H_PX_TALLEST);
	if (comfort > large.h) {
		out.push(
			`preset ${spec.id} at ${large.w}x${large.h}: needs ${comfort} px (${TOPBAR_H_PX} px topbar + ` +
				`${rows} x ${WAVEROW_H_PX_TALLEST} px waverows + ${deckRowPx} px deck row + ${libraryPx} px library), ` +
				`window is ${large.h} px: ${comfort - large.h} px over`
		);
	}
	return out;
}

//----- Now Playing panel data (pure, no fetch)

export type NowPlayingSource = 'master-deck' | 'preview' | 'selection';

export type NowPlayingPick = Readonly<{ stable_id: string; source: NowPlayingSource }> | null;

export type StoredGigSession = Readonly<{
	master_deck?: number | null;
	decks: Readonly<Record<number, Readonly<{ stable_id: string | null }>>>;
}>;

/** The track the Now Playing panel shows. Library mode runs no decks, so the
 * master deck is the one the last Gig session left on master. */
export function nowPlayingId(input: {
	session: StoredGigSession | null;
	previewId: string | null;
	selectedId: string | null;
}): NowPlayingPick {
	const master = input.session?.master_deck;
	if (master !== undefined && master !== null) {
		const sid = input.session?.decks[master]?.stable_id ?? null;
		if (sid !== null) return { stable_id: sid, source: 'master-deck' };
	}
	if (input.previewId !== null) return { stable_id: input.previewId, source: 'preview' };
	if (input.selectedId !== null) return { stable_id: input.selectedId, source: 'selection' };
	return null;
}

export const NOW_PLAYING_SOURCE_LABEL: Readonly<Record<NowPlayingSource, string>> = {
	'master-deck': 'Master deck (last Gig session)',
	preview: 'Library preview',
	selection: 'Selected track'
};

export function nextInQueueId(chain: readonly string[], nowId: string | null): string | null {
	return chain.find((sid) => sid !== nowId) ?? null;
}

export type AboutTrack = Readonly<{
	artist?: string | null;
	album?: string | null;
	bpm?: number | null;
	key?: string | null;
	play_count?: number | null;
	tags?: readonly string[];
}>;

export type AboutFact = Readonly<{ label: string; value: string }>;

export function aboutFacts(track: AboutTrack): AboutFact[] {
	const facts: AboutFact[] = [];
	const push = (label: string, value: string | null | undefined): void => {
		if (value !== null && value !== undefined && value.trim() !== '') facts.push({ label, value });
	};
	push('Artist', track.artist);
	push('Album', track.album);
	push('BPM', track.bpm === null || track.bpm === undefined ? null : track.bpm.toFixed(1));
	push('Key', track.key);
	push(
		'Plays',
		track.play_count === null || track.play_count === undefined ? null : String(track.play_count)
	);
	push('Tags', track.tags && track.tags.length > 0 ? track.tags.join(', ') : null);
	return facts;
}

/** The green strip under the player. Library previews play through the
 * browser's default sink, so an unlabeled sink is named as such, never guessed. */
export function playingOnLabel(sinkLabel: string | null): string {
	const name =
		sinkLabel === null || sinkLabel.trim() === '' ? 'the system default output' : sinkLabel.trim();
	return `Playing on ${name}`;
}

/** 0..1 for the thin progress line; 0 when the duration is unknown. */
export function progressRatio(position_ms: number, duration_ms: number): number {
	if (!(duration_ms > 0)) return 0;
	return Math.min(1, Math.max(0, position_ms / duration_ms));
}

//----- Your Library panel search

export type NamedTreeNode<T> = { name: string; children: T[] };

/** Nodes whose name contains `query` (case-insensitive), keeping each match's
 * folder path. A blank query returns the input array itself: zero added work
 * on the listing path when nobody is searching. */
export function filterTreeByName<T extends NamedTreeNode<T>>(nodes: T[], query: string): T[] {
	const needle = query.trim().toLowerCase();
	if (needle === '') return nodes;
	const walk = (list: T[]): T[] => {
		const kept: T[] = [];
		for (const node of list) {
			if (node.name.toLowerCase().includes(needle)) kept.push(node);
			else {
				const children = walk(node.children);
				if (children.length > 0) kept.push({ ...node, children });
			}
		}
		return kept;
	};
	return walk(nodes);
}
