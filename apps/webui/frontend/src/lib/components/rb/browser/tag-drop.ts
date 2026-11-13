/**
 * Dropping dragged library tracks onto a tag in the Taglists tree (LIBTAG-9668).
 * No state and no network, so node --test drives it directly; TaglistTree does
 * the write through `applyTagChange` and shows the toast this module words.
 */
import { normalizeTagName } from '$lib/rb/tag-display';

/** One row of the Taglists tree: a tag, or a header that groups tags. */
export type TagTreeRow =
	{ kind: 'tag'; name: string } | { kind: 'type-head' } | { kind: 'untyped-head' };

/** Only a tag row takes a track drop; a header is never a target. */
export function tagRowAcceptsDrop(row: TagTreeRow): row is { kind: 'tag'; name: string } {
	return row.kind === 'tag';
}

/** The dropped ids that do not hold the tag yet, once each, in drop order. */
export function idsNeedingTag(
	stableIds: readonly string[],
	tagName: string,
	assignments: Readonly<Record<string, readonly string[]>>
): string[] {
	const norm = normalizeTagName(tagName);
	const seen = new Set<string>();
	return stableIds.filter((id) => {
		if (seen.has(id)) return false;
		seen.add(id);
		return !(assignments[id] ?? []).some((held) => normalizeTagName(held) === norm);
	});
}

/** The toast after a tag drop wrote `count` tracks (0 means all held it already). */
export function tagDropToast(count: number, tagName: string): string {
	if (count === 0) return 'Already tagged';
	return `Tagged ${count} ${count === 1 ? 'track' : 'tracks'} #${tagName}. Cmd+Z undoes it.`;
}
