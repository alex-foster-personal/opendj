/**
 * What the table draws for works, version stacks and the classical layout
 * (Library Organization PRD s5.3 to s5.5). Pure, so node tests load it.
 *
 * A track in no work with no classical metadata is absent from the overlay and
 * every helper here returns the plain answer for it (W7).
 */
export type WorkRole = 'original' | 'recording' | 'remix' | 'cover' | 'edit' | 'live' | 'mashup';

export interface WorkInfo {
	kind: 'classical' | 'song';
	title: string;
	composer: string | null;
	catalogue: string | null;
	movement: string | null;
	aliases: string[];
	member_count: number;
}

export interface MemberLink {
	work_id: string;
	role: WorkRole;
	is_primary: boolean;
}

export interface ClassicalValues {
	composer?: string;
	catalogue?: string;
	movement?: string;
	soloist?: string;
	ensemble?: string;
	conductor?: string;
	label?: string;
	recording_year?: number;
}

export interface WorksOverlay {
	works: Record<string, WorkInfo>;
	members: Record<string, MemberLink[]>;
	classical: Record<string, ClassicalValues>;
}

export const EMPTY_OVERLAY: WorksOverlay = { works: {}, members: {}, classical: {} };

export const OPTIONAL_COLUMNS = ['work', 'composer', 'ensemble', 'label', 'year'] as const;
export type OptionalColumn = (typeof OPTIONAL_COLUMNS)[number];

export const OPTIONAL_COLUMN_LABELS: Record<OptionalColumn, string> = {
	work: 'Work',
	composer: 'Composer',
	ensemble: 'Conductor / Ensemble',
	label: 'Label',
	year: 'Year'
};

/** The smallest stack worth showing: a work with one member has no versions (E9). */
const MIN_STACK_MEMBERS = 2;

function primaryLink(overlay: WorksOverlay, stableId: string): MemberLink | null {
	const links = overlay.members[stableId];
	if (links === undefined || links.length === 0) return null;
	return links.find((link) => link.is_primary) ?? links[0];
}

export function primaryWork(overlay: WorksOverlay, stableId: string): WorkInfo | null {
	const link = primaryLink(overlay, stableId);
	return link === null ? null : (overlay.works[link.work_id] ?? null);
}

export function primaryWorkId(overlay: WorksOverlay, stableId: string): string | null {
	return primaryLink(overlay, stableId)?.work_id ?? null;
}

/** `▸ N`, `▸ ?` for suggestions only, or null for a plain track (R-VER-2). */
export function stackIndicator(
	overlay: WorksOverlay,
	stableId: string,
	hasSuggestion: boolean
): string | null {
	const count = primaryWork(overlay, stableId)?.member_count ?? 0;
	if (count >= MIN_STACK_MEMBERS) return `▸ ${count}`;
	return hasSuggestion ? '▸ ?' : null;
}

/** Primary work first, then "contains" works: "Lacrimosa + Dies irae" (E24). */
export function workCellText(overlay: WorksOverlay, stableId: string): string {
	const links = overlay.members[stableId] ?? [];
	return [...links]
		.sort((a, b) => Number(b.is_primary) - Number(a.is_primary))
		.flatMap((link) => overlay.works[link.work_id]?.title ?? [])
		.join(' + ');
}

export function filedUnderText(overlay: WorksOverlay, stableId: string): string {
	const work = primaryWork(overlay, stableId);
	if (work === null) return '';
	const who = composerOf(overlay, stableId);
	return who === '' ? `filed under ${work.title}` : `filed under ${work.title} (${who})`;
}

export function composerOf(overlay: WorksOverlay, stableId: string): string {
	return (
		overlay.classical[stableId]?.composer ?? primaryWork(overlay, stableId)?.composer ?? ''
	);
}

function join(parts: readonly (string | undefined)[], separator: string): string {
	return parts.filter((part): part is string => Boolean(part)).join(separator);
}

/** Soloist, ensemble and conductor, for the Conductor / Ensemble column. */
export function performerOf(overlay: WorksOverlay, stableId: string): string {
	const values = overlay.classical[stableId];
	return values === undefined ? '' : join([values.soloist, values.ensemble, values.conductor], ', ');
}

export function labelOf(overlay: WorksOverlay, stableId: string): string {
	return overlay.classical[stableId]?.label ?? '';
}

export function yearOf(overlay: WorksOverlay, stableId: string): number | null {
	return overlay.classical[stableId]?.recording_year ?? null;
}

/** The classical work a track IS a performance of. A remix, cover or edit is filed
 * under a work without being one: its own title and artist are what tell it from
 * the other versions, so the layout leaves them alone. */
function performedWork(overlay: WorksOverlay, stableId: string): WorkInfo | null {
	const link = primaryLink(overlay, stableId);
	if (link === null || (link.role !== 'recording' && link.role !== 'original')) return null;
	const work = overlay.works[link.work_id];
	return work?.kind === 'classical' ? work : null;
}

/** `Movement - Work, Catalogue`, then `Work, Catalogue`, then the stored title (E44). */
export function classicalTitle(
	overlay: WorksOverlay,
	stableId: string,
	storedTitle: string
): string {
	const values = overlay.classical[stableId];
	const work = performedWork(overlay, stableId);
	const movement = values?.movement ?? work?.movement ?? undefined;
	const catalogue = values?.catalogue ?? work?.catalogue ?? undefined;
	const name = work?.title;
	const piece = movement === name ? name : join([movement, name], ' - ');
	if (!piece) return storedTitle;
	return join([piece, catalogue], ', ');
}

/** `Composer - Performer`, falling back to whichever exists, then the stored artist. */
export function classicalArtist(
	overlay: WorksOverlay,
	stableId: string,
	storedArtist: string
): string {
	const composer =
		overlay.classical[stableId]?.composer ??
		performedWork(overlay, stableId)?.composer ??
		undefined;
	return join([composer, performerOf(overlay, stableId)], ' - ') || storedArtist;
}

/** Every track filed under a work whose title or alias holds the query (W3). */
export function tracksViaWork(overlay: WorksOverlay, query: string): Set<string> {
	const needle = query.trim().toLocaleLowerCase();
	const hits = new Set<string>();
	if (needle === '') return hits;
	const matching = new Set(
		Object.entries(overlay.works)
			.filter(([, work]) =>
				[work.title, ...work.aliases].some((name) =>
					name.toLocaleLowerCase().includes(needle)
				)
			)
			.map(([id]) => id)
	);
	if (matching.size === 0) return hits;
	for (const [stableId, links] of Object.entries(overlay.members)) {
		if (links.some((link) => matching.has(link.work_id))) hits.add(stableId);
	}
	return hits;
}

type SortableRow = { stable_id: string; title?: string | null; artist?: string | null };

/** A sort value for works columns, or undefined when the key is not ours to answer. */
export function worksSortValue(
	overlay: WorksOverlay,
	classicalLayout: boolean,
	row: SortableRow,
	key: string
): string | number | null | undefined {
	const text = (value: string): string | null => (value === '' ? null : value);
	if (key === 'work') return text(workCellText(overlay, row.stable_id));
	if (key === 'composer') return text(composerOf(overlay, row.stable_id));
	if (key === 'ensemble') return text(performerOf(overlay, row.stable_id));
	if (key === 'label') return text(labelOf(overlay, row.stable_id));
	if (key === 'year') return yearOf(overlay, row.stable_id);
	if (classicalLayout && key === 'title') {
		return classicalTitle(overlay, row.stable_id, row.title ?? '').toLocaleLowerCase();
	}
	if (classicalLayout && key === 'artist') {
		return classicalArtist(overlay, row.stable_id, row.artist ?? '').toLocaleLowerCase();
	}
	return undefined;
}

export function normalizeOptionalColumns(value: unknown): OptionalColumn[] {
	if (!Array.isArray(value)) {
		throw new Error(`library_optional_columns must be a list, got ${JSON.stringify(value)}`);
	}
	return OPTIONAL_COLUMNS.filter((col) => value.includes(col));
}
