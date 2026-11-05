/**
 * SET-6586 Routes view helpers: pure functions over the components and sets the
 * HTTP routes return. A route is one component per slot, in slot order; the
 * view keeps it as slot -> component id so a set that skips a slot stays exact.
 */

export interface RouteComponent {
	id: number;
	name: string;
	slot: string;
	stable_ids: string[];
	tags: string[];
	/** Borrowed from another playlist by a saved set; listed for drawing, not owned here. */
	borrowed?: boolean;
}

export interface RouteSet {
	id: number;
	name: string;
	play_count: number;
	parts: number[];
}

export interface SlotGroup {
	slot: string;
	components: RouteComponent[];
}

/** slot -> picked component id. A slot with no entry is not on the route. */
export type RoutePick = Record<string, number>;

/** Groups in the order the server lists components (slot ladder order). */
export function slotGroups(components: readonly RouteComponent[]): SlotGroup[] {
	const groups: SlotGroup[] = [];
	const bySlot = new Map<string, SlotGroup>();
	for (const comp of components) {
		let group = bySlot.get(comp.slot);
		if (!group) {
			group = { slot: comp.slot, components: [] };
			bySlot.set(comp.slot, group);
			groups.push(group);
		}
		group.components.push(comp);
	}
	return groups;
}

/** The first component of every slot: the route shown before any choice. */
export function defaultPick(groups: readonly SlotGroup[]): RoutePick {
	const pick: RoutePick = {};
	for (const g of groups) pick[g.slot] = g.components[0].id;
	return pick;
}

/** The route shown on opening: the most-played saved route (ties go to the
 *  first listed), else the first component of every slot. */
export function openingPick(
	groups: readonly SlotGroup[],
	components: readonly RouteComponent[],
	sets: readonly RouteSet[]
): RoutePick {
	let best: RouteSet | null = null;
	for (const s of sets) {
		if (s.parts.length > 0 && (best === null || s.play_count > best.play_count)) best = s;
	}
	return best ? pickFromParts(best.parts, components) : defaultPick(groups);
}

/** A saved set's parts as a pick. Parts that are not listed here (a borrowed
 *  component) are dropped, since the view cannot draw them. */
export function pickFromParts(
	parts: readonly number[],
	components: readonly RouteComponent[]
): RoutePick {
	const byId = new Map(components.map((c) => [c.id, c]));
	const pick: RoutePick = {};
	for (const id of parts) {
		const comp = byId.get(id);
		if (comp) pick[comp.slot] = id;
	}
	return pick;
}

/** The pick with only the slots whose component still exists, so a deleted component cannot stay in a route. */
// A pick keys each component by the slot it held when picked. A component another client moved to a
// different slot must be re-keyed by its current slot, or partsOf drops it and the route looks unsaved.
// Two picks landing in one slot are no route at all, so the pick clears rather than truncating.
export function prunePick(pick: RoutePick, components: readonly RouteComponent[]): RoutePick {
	const byId = new Map(components.map((c) => [c.id, c]));
	const kept: RoutePick = {};
	for (const id of Object.values(pick)) {
		const comp = byId.get(id);
		if (!comp) continue; // a deleted component leaves a hidden part, which blocks Move
		if (kept[comp.slot] !== undefined) return {};
		kept[comp.slot] = id;
	}
	return kept;
}

export function partsOf(pick: RoutePick, groups: readonly SlotGroup[]): number[] {
	const parts: number[] = [];
	for (const g of groups) {
		const id = pick[g.slot];
		if (id !== undefined) parts.push(id);
	}
	return parts;
}

export function sameParts(a: readonly number[], b: readonly number[]): boolean {
	return a.length === b.length && a.every((x, i) => x === b[i]);
}

/** The saved set whose route is exactly these parts; sets without parts never match. */
export function matchSet<S extends RouteSet>(
	parts: readonly number[],
	sets: readonly S[]
): S | null {
	if (parts.length === 0) return null;
	return sets.find((s) => s.parts.length > 0 && sameParts(s.parts, parts)) ?? null;
}

/** Shortest case-insensitive name prefix unique across the playlist's
 *  components, first letter capitalized: Warm, Straight in -> W, S; Sunrise,
 *  Singalong -> Su, Si. A name that is a prefix of another keeps its full name. */
export function componentGlyphs(components: readonly RouteComponent[]): Map<number, string> {
	const names = components.map((c) => c.name.replace(/\s+/g, '').toLowerCase());
	const out = new Map<number, string>();
	components.forEach((comp, i) => {
		const name = names[i];
		let len = 1;
		while (
			len < name.length &&
			names.some((other, j) => j !== i && other.startsWith(name.slice(0, len)))
		) {
			len += 1;
		}
		const glyph = name.slice(0, len);
		out.set(comp.id, glyph.charAt(0).toUpperCase() + glyph.slice(1));
	});
	return out;
}

/** The tab glyph: W·M·B. Empty for a set with no parts (a SET-05 flat set). */
export function routeGlyph(parts: readonly number[], glyphs: ReadonlyMap<number, string>): string {
	return parts.map((id) => glyphs.get(id) ?? '?').join('·');
}

/** Tracks that sit in more than one component: where routes rejoin. */
export function sharedTracks(components: readonly RouteComponent[]): Set<string> {
	const seen = new Set<string>();
	const shared = new Set<string>();
	for (const comp of components) {
		for (const sid of new Set(comp.stable_ids)) {
			if (seen.has(sid)) shared.add(sid);
			seen.add(sid);
		}
	}
	return shared;
}

/** Total length of these tracks, and how many had no known length. */
export function tracksDuration(
	stableIds: readonly string[],
	durationOf: (sid: string) => number | null | undefined
): { ms: number; unknown: number } {
	let ms = 0;
	let unknown = 0;
	for (const sid of stableIds) {
		const d = durationOf(sid);
		if (typeof d === 'number' && d >= 0) ms += d;
		else unknown += 1;
	}
	return { ms, unknown };
}

/** M:SS under an hour, H:MM:SS above. */
export function formatDuration(ms: number): string {
	const total = Math.round(ms / 1000);
	const h = Math.floor(total / 3600);
	const m = Math.floor((total % 3600) / 60);
	const s = String(total % 60).padStart(2, '0');
	return h > 0 ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`;
}

/** Comma or space separated tags, '#' optional, as the store will keep them. */
export function parseTags(text: string): string[] {
	const out: string[] = [];
	for (const raw of text.split(/[\s,]+/)) {
		const tag = raw.replace(/^#+/, '').trim();
		if (tag && !out.includes(tag)) out.push(tag);
	}
	return out;
}

/** The slot ladder (apps/shared/playlist_sets/components.py DEFAULT_SLOTS). */
export const DEFAULT_SLOTS = ['opener', 'build', 'main', 'peak', 'ending', 'encore'] as const;
