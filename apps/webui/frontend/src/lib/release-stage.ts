/**
 * The product's release stage, shown beside the wordmark (OSSPUB-04).
 *
 * One constant so moving to beta, or dropping the badge at a stable release, is
 * a one-line change that every surface follows. The title is the explanation a
 * hover gives: what "alpha" means for the person using it, not a label alone.
 */
export const RELEASE_STAGE = 'alpha' as const;

export const RELEASE_STAGE_TITLE =
	'Open DJ is alpha software: features, screens and stored data formats can change ' +
	'between releases, and some controls are not finished yet. Keep a backup of your ' +
	'DJ library before letting it write anything.';
