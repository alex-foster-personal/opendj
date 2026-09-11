/**
 * Autolist selection-to-rule compiler (issue #2066).
 *
 * Mirrors apps/smartlists/autolist_groups.py: OR within a group, AND across
 * groups. Empty selection returns null.
 */
import type { RuleAst } from './rule-form';

export type AutolistGroupId = 'genre' | 'rating' | 'bpm';
export type AutolistChildId = string;
export type AutolistSelection = Record<AutolistGroupId, AutolistChildId[]>;

export const GENRE_UNSPECIFIED_ID = 'unspecified';
export const RATING_UNRATED_ID = 'unrated';
export const BPM_UNSPECIFIED_ID = 'unspecified';
export const BPM_LT60_ID = 'lt60';
export const BPM_GE200_ID = 'ge200';

const GROUP_ORDER: AutolistGroupId[] = ['genre', 'rating', 'bpm'];

function genreRule(childId: string): RuleAst {
	if (childId === GENRE_UNSPECIFIED_ID) {
		return { field: 'genre', op: 'missing', value: null };
	}
	return { field: 'genre', op: '=', value: childId };
}

function ratingRule(childId: string): RuleAst {
	if (childId === RATING_UNRATED_ID) {
		return {
			op: 'or',
			children: [
				{ field: 'rating', op: '=', value: 0 },
				{ field: 'rating', op: 'missing', value: null }
			]
		};
	}
	return { field: 'rating', op: '=', value: Number(childId) };
}

function bpmRule(childId: string): RuleAst {
	if (childId === BPM_UNSPECIFIED_ID) {
		return { field: 'bpm', op: 'missing', value: null };
	}
	if (childId === BPM_LT60_ID) {
		return { field: 'bpm', op: '<', value: 60 };
	}
	if (childId === BPM_GE200_ID) {
		return { field: 'bpm', op: '>=', value: 200 };
	}
	const dash = childId.indexOf('-');
	if (dash > 0) {
		const lo = Number(childId.slice(0, dash));
		const hi = Number(childId.slice(dash + 1));
		return {
			op: 'and',
			children: [
				{ field: 'bpm', op: '>=', value: lo },
				{ field: 'bpm', op: '<', value: hi + 1 }
			]
		};
	}
	throw new Error(`unknown bpm bucket id ${childId}`);
}

function groupRule(group: AutolistGroupId, childIds: AutolistChildId[]): RuleAst | null {
	if (childIds.length === 0) return null;
	const rules: RuleAst[] =
		group === 'genre'
			? childIds.map(genreRule)
			: group === 'rating'
				? childIds.map(ratingRule)
				: childIds.map(bpmRule);
	if (rules.length === 1) return rules[0];
	return { op: 'or', children: rules };
}

export function autolistSelectionToRule(selection: AutolistSelection): RuleAst | null {
	const parts: RuleAst[] = [];
	for (const group of GROUP_ORDER) {
		const rule = groupRule(group, selection[group] ?? []);
		if (rule !== null) parts.push(rule);
	}
	if (parts.length === 0) return null;
	if (parts.length === 1) return parts[0];
	return { op: 'and', children: parts };
}

const RATING_LABELS: Record<string, string> = {
	'5': '5 stars',
	'4': '4 stars',
	'3': '3 stars',
	'2': '2 stars',
	'1': '1 star',
	unrated: 'Unrated'
};

const BPM_LABELS: Record<string, string> = {
	lt60: '<60',
	ge200: '>=200',
	unspecified: 'Unspecified'
};

function childLabel(group: AutolistGroupId, childId: string): string {
	if (group === 'rating') return RATING_LABELS[childId] ?? childId;
	if (group === 'bpm') return BPM_LABELS[childId] ?? childId;
	if (childId === GENRE_UNSPECIFIED_ID) return 'Unspecified';
	return childId;
}

export function summarizeAutolistSelection(selection: AutolistSelection): string {
	const parts: string[] = [];
	for (const group of GROUP_ORDER) {
		for (const childId of selection[group] ?? []) {
			parts.push(childLabel(group, childId));
		}
	}
	if (parts.length === 0) return 'Autolists';
	if (parts.length <= 2) return parts.join(', ');
	return `${parts.slice(0, 2).join(', ')} + ${parts.length - 2} more`;
}

export function emptyAutolistSelection(): AutolistSelection {
	return { genre: [], rating: [], bpm: [] };
}

export function hasAutolistSelection(selection: AutolistSelection): boolean {
	return GROUP_ORDER.some((g) => (selection[g] ?? []).length > 0);
}
