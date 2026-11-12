/**
 * Agent-facing registry for /performance alternate views and the first CSS
 * theme packs. Parent #325 (alt-views-agent-kit): add a row here instead of
 * forking the default rekordbox-parity shell.
 *
 * Requirements (mini-PRD):
 *   ? Every perf_view and the first ui_skin packs declare id, title, css pack,
 *     layout slots and capability flags.
 *     [if] a registered entry is missing any field [then ⛔️]
 *     [if] waveform, list, phase-scope or phase-meter is absent [then ⛔️]
 *     [if] default, mono-dev or light is absent from skin packs [then ⛔️]
 *   ? parsePerfViewId refuses unknown ids.
 *     [if] parsePerfViewId('grid') returns instead of throwing [then ⛔️]
 *   ? Waveform is the only view that mounts the default scrolling stack.
 *     [if] a non-waveform view's flags still claim mountsWaveformStack [then ⛔️]
 */

import { SKIN_LAYOUT, type SkinLayout } from './skin-layout';
import type { UiSkin } from './ui-skin';

/** Named CSS / theme pack an entry reuses (not a fork of the default shell). */
export type CssPackId = 'rekordbox-parity' | UiSkin;

/** Grid areas on /performance an alternate view may occupy. */
export type PerfViewLayoutSlot = 'wavestack' | 'phase-view';

export type PerfViewCapabilityFlags = Readonly<{
	/** Mounts the four-deck WaveformStack in the wavestack slot. */
	mountsWaveformStack: boolean;
	/** Sets --rb-wavestack-shown: 0 on the performance root (List view). */
	collapsesWavestackTrack: boolean;
	/** Loads a view module on first selection (phase views). */
	lazyViewChunk: boolean;
	/** Renders in the phase-view region and follows technically-working hide rules. */
	phaseViewRegion: boolean;
}>;

export type PerfViewWavestackMount = 'waveform' | 'phase-scope' | 'phase-meter' | 'none';

export type PerfViewRegistryEntry = Readonly<{
	id: PerfViewId;
	title: string;
	cssPack: CssPackId;
	layoutSlots: readonly PerfViewLayoutSlot[];
	flags: PerfViewCapabilityFlags;
	wavestackMount: PerfViewWavestackMount;
}>;

/** Live perf_view values; the registry is the allow list. */
export type PerfViewId = 'waveform' | 'list' | 'phase-scope' | 'phase-meter';

/** First-wave skin packs (Macintosh and other pre-mades stay on #325). */
export type RegisteredSkinPackId = 'default' | 'mono-dev' | 'light';

export type SkinPackLayoutSlot = 'performance-root' | 'library-surface';

export type SkinPackCapabilityFlags = Readonly<{
	/** Writes html[data-skin] for non-default packs. */
	setsDataSkinAttribute: boolean;
	/** Skin declares wave look through SKIN_WAVE_LOOK / theme.css. */
	declaresWaveformChrome: boolean;
}>;

export type SkinPackRegistryEntry = Readonly<{
	id: RegisteredSkinPackId;
	title: string;
	cssPack: CssPackId;
	layoutSlots: readonly SkinPackLayoutSlot[];
	flags: SkinPackCapabilityFlags;
	layout: SkinLayout;
}>;

/** One row per perf_view icon; agents add views by extending this table. */
export const PERF_VIEW_REGISTRY: Readonly<Record<PerfViewId, PerfViewRegistryEntry>> = {
	waveform: {
		id: 'waveform',
		title: 'Waveforms',
		cssPack: 'rekordbox-parity',
		layoutSlots: ['wavestack'],
		flags: {
			mountsWaveformStack: true,
			collapsesWavestackTrack: false,
			lazyViewChunk: false,
			phaseViewRegion: false
		},
		wavestackMount: 'waveform'
	},
	list: {
		id: 'list',
		title: 'List',
		cssPack: 'rekordbox-parity',
		layoutSlots: [],
		flags: {
			mountsWaveformStack: false,
			collapsesWavestackTrack: true,
			lazyViewChunk: false,
			phaseViewRegion: false
		},
		wavestackMount: 'none'
	},
	'phase-scope': {
		id: 'phase-scope',
		title: 'Phase scope',
		cssPack: 'rekordbox-parity',
		layoutSlots: ['wavestack', 'phase-view'],
		flags: {
			mountsWaveformStack: false,
			collapsesWavestackTrack: false,
			lazyViewChunk: true,
			phaseViewRegion: true
		},
		wavestackMount: 'phase-scope'
	},
	'phase-meter': {
		id: 'phase-meter',
		title: 'Phase meter',
		cssPack: 'rekordbox-parity',
		layoutSlots: ['wavestack', 'phase-view'],
		flags: {
			mountsWaveformStack: false,
			collapsesWavestackTrack: false,
			lazyViewChunk: true,
			phaseViewRegion: true
		},
		wavestackMount: 'phase-meter'
	}
};

export const PERF_VIEW_IDS = Object.keys(PERF_VIEW_REGISTRY) as PerfViewId[];

/** First ui_skin packs wired for alternate chrome without forking components. */
export const SKIN_PACK_REGISTRY: Readonly<Record<RegisteredSkinPackId, SkinPackRegistryEntry>> = {
	default: {
		id: 'default',
		title: 'Default',
		cssPack: 'default',
		layoutSlots: ['performance-root', 'library-surface'],
		flags: { setsDataSkinAttribute: false, declaresWaveformChrome: true },
		layout: SKIN_LAYOUT.default
	},
	'mono-dev': {
		id: 'mono-dev',
		title: 'Gothic',
		cssPack: 'mono-dev',
		layoutSlots: ['performance-root', 'library-surface'],
		flags: { setsDataSkinAttribute: true, declaresWaveformChrome: true },
		layout: SKIN_LAYOUT['mono-dev']
	},
	light: {
		id: 'light',
		title: 'Light',
		cssPack: 'light',
		layoutSlots: ['performance-root', 'library-surface'],
		flags: { setsDataSkinAttribute: true, declaresWaveformChrome: true },
		layout: SKIN_LAYOUT.light
	}
};

export const SKIN_PACK_IDS = Object.keys(SKIN_PACK_REGISTRY) as RegisteredSkinPackId[];

export function getPerfViewEntry(id: PerfViewId): PerfViewRegistryEntry {
	return PERF_VIEW_REGISTRY[id];
}

export function getSkinPackEntry(id: RegisteredSkinPackId): SkinPackRegistryEntry {
	return SKIN_PACK_REGISTRY[id];
}

export function isPerfViewId(value: unknown): value is PerfViewId {
	return typeof value === 'string' && Object.hasOwn(PERF_VIEW_REGISTRY, value);
}

export function parsePerfViewId(raw: unknown): PerfViewId {
	if (!isPerfViewId(raw)) {
		throw new Error(`perf_view must be one of ${PERF_VIEW_IDS.join(', ')}, got ${String(raw)}`);
	}
	return raw;
}

export function isRegisteredSkinPackId(value: unknown): value is RegisteredSkinPackId {
	return typeof value === 'string' && Object.hasOwn(SKIN_PACK_REGISTRY, value);
}

export function parseSkinPackId(raw: unknown): RegisteredSkinPackId {
	if (!isRegisteredSkinPackId(raw)) {
		throw new Error(`ui_skin pack must be one of ${SKIN_PACK_IDS.join(', ')}, got ${String(raw)}`);
	}
	return raw;
}

export function perfViewCollapsesWavestack(view: PerfViewId): boolean {
	return PERF_VIEW_REGISTRY[view].flags.collapsesWavestackTrack;
}

export function perfViewWavestackMount(view: PerfViewId): PerfViewWavestackMount {
	return PERF_VIEW_REGISTRY[view].wavestackMount;
}

/** Enumerate registered perf views for agents / CLI parity. */
export function listPerfViewRegistry(): readonly PerfViewRegistryEntry[] {
	return PERF_VIEW_IDS.map((id) => PERF_VIEW_REGISTRY[id]);
}

/** Enumerate the first-wave skin packs for agents. */
export function listSkinPackRegistry(): readonly SkinPackRegistryEntry[] {
	return SKIN_PACK_IDS.map((id) => SKIN_PACK_REGISTRY[id]);
}
