/** Related Tracks criteria prefs (LIBUX-10046): the BPM range and the six
 * on/off rules the Related tab scores by. Mirrors
 * apps/webui/server/library_related_prefs.py; defaults are the #9753 set. */

export interface RelatedCriteriaPrefs {
	bpm_percent: number;
	key: boolean;
	genre: boolean;
	color: boolean;
	rating: boolean;
	mytag: boolean;
	artist: boolean;
}

export type RelatedCriteriaFlag = Exclude<keyof RelatedCriteriaPrefs, 'bpm_percent'>;

export const RELATED_BPM_PERCENT_MIN = 1;
export const RELATED_BPM_PERCENT_MAX = 20;

export const RELATED_CRITERIA_DEFAULTS: RelatedCriteriaPrefs = {
	bpm_percent: 6,
	key: true,
	genre: true,
	color: false,
	rating: false,
	mytag: false,
	artist: false
};

/** rekordbox order, with the labels the cog shows. */
export const RELATED_CRITERIA_FLAGS: readonly { name: RelatedCriteriaFlag; label: string }[] = [
	{ name: 'key', label: 'Key' },
	{ name: 'genre', label: 'Genre' },
	{ name: 'color', label: 'Colour' },
	{ name: 'rating', label: 'Rating (equal or higher)' },
	{ name: 'mytag', label: 'My Tag' },
	{ name: 'artist', label: 'Same artist' }
];

export function isValidRelatedBpmPercent(v: unknown): v is number {
	return (
		typeof v === 'number' &&
		Number.isInteger(v) &&
		v >= RELATED_BPM_PERCENT_MIN &&
		v <= RELATED_BPM_PERCENT_MAX
	);
}

/** The valid subset of a stored or wire value; an invalid field is reported and dropped. */
export function validateRelatedCriteriaPrefs(
	raw: unknown,
	source: string
): Partial<RelatedCriteriaPrefs> {
	if (raw === undefined) return {};
	if (typeof raw !== 'object' || raw === null) {
		throw new Error(`${source}: library_related_criteria must be an object`);
	}
	const obj = raw as Record<string, unknown>;
	const out: Partial<RelatedCriteriaPrefs> = {};
	if (obj.bpm_percent !== undefined) {
		if (isValidRelatedBpmPercent(obj.bpm_percent)) out.bpm_percent = obj.bpm_percent;
		else console.error(`${source}: library_related_criteria.bpm_percent rejected`, obj.bpm_percent);
	}
	for (const { name } of RELATED_CRITERIA_FLAGS) {
		const value = obj[name];
		if (value === undefined) continue;
		if (typeof value === 'boolean') out[name] = value;
		else console.error(`${source}: library_related_criteria.${name} rejected`, value);
	}
	return out;
}

/** All seven params, so each request says exactly what the cog shows. */
export function relatedCriteriaQuery(c: RelatedCriteriaPrefs): URLSearchParams {
	const params = new URLSearchParams({ bpm_percent: String(c.bpm_percent) });
	for (const { name } of RELATED_CRITERIA_FLAGS) params.set(name, c[name] ? '1' : '0');
	return params;
}
