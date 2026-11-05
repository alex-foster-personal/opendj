/**
 * SKINLAYOUT-8869: the LAYOUT part of a skin manifest ((private path)
 * section 2, ADR-0136). A skin names one preset per surface from a fixed
 * catalog; the library workspace grid reads it as `data-layout` and places
 * the same components in different grid areas. No component forks per skin.
 *
 * Requirements (mini-PRD):
 *   ? SKIN_LAYOUT: every skin declares a library preset; only Green Room bends.
 *     [if] libraryLayoutFor('mono-dev') is not 'crate-desk' [then ⛔️]
 *     [if] libraryLayoutFor('green-room') is not 'three-panel' [then ⛔️]
 *   ? parseLibraryLayout: unknown preset ids throw.
 *     [if] parseLibraryLayout('sidebar-left') returns instead of throwing [then ⛔️]
 *   ? nowPlayingId: master deck from the last Gig session, then the preview, then the selection.
 *     [if] a stored master deck with a track loses to the preview [then ⛔️]
 *   ? nextInQueueId: AutoPlay's first charted pick that is not the track now playing.
 *     [if] the now-playing id is shown as its own next pick [then ⛔️]
 *   ? aboutFacts: only fields the track already holds, empty fields dropped.
 *     [if] a null album renders as an About row [then ⛔️]
 */

import type { UiSkin } from './ui-skin';

/** Library surface presets. A new preset is a new grid map in CrateDeskView, never a new component. */
export type LibraryLayout = 'crate-desk' | 'three-panel';

export const LIBRARY_LAYOUTS: readonly LibraryLayout[] = ['crate-desk', 'three-panel'];

/** The manifest's `layout` block. Only the library surface has presets so far;
 * `performance` and `mixer` join here when their first preset ships. */
export type SkinLayout = Readonly<{ library: LibraryLayout }>;

/** One entry per skin, so a new skin without a layout fails the type check. */
export const SKIN_LAYOUT: Readonly<Record<UiSkin, SkinLayout>> = {
	default: { library: 'crate-desk' },
	'mono-dev': { library: 'crate-desk' },
	light: { library: 'crate-desk' },
	'green-room': { library: 'three-panel' },
	// Hardware practice skin: the library keeps the standard desk; the replica is /practice/<device>.
	'practice-ddj-flx4': { library: 'crate-desk' }
};

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
