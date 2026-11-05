/**
 * PREPWS-01: what the Prep mode page (/prep) offers until its own workspace
 * (PERFMODE-14 follow-on) is built.
 *
 * The mode chooser has advertised Prep since PERFMODE-13, and /prep used to be
 * a stub paragraph naming a requirement id. For V1 it is a hub instead: every
 * entry links to a page that already works today, so Prep is never a dead end
 * and nothing here is inert or mocked. Pure data, so the list and its rules
 * are unit testable without a browser.
 */

export interface PrepHubEntry {
	href: string;
	label: string;
	/** One line on what the page is for, in prep terms. */
	does: string;
}

export const PREP_HUB_ENTRIES: readonly PrepHubEntry[] = [
	{
		href: '/',
		label: 'Library',
		does: 'Tag, rate and comment tracks, edit playlists, and run Find & Replace or Bulk Edit.'
	},
	{
		href: '/smartlists',
		label: 'Smartlists',
		does: 'Build rule-based playlists that keep themselves up to date.'
	},
	{
		href: '/pairings',
		label: 'Pairings',
		does: 'Save track pairs that mix well together, to find again on the night.'
	},
	{
		href: '/dedup',
		label: 'Dedup Review',
		does: 'Review duplicate tracks and choose which copy to keep.'
	},
	{
		href: '/reconcile',
		label: 'Missing tracks',
		does: 'Find tracks whose audio file moved or went missing, then relocate or remove them.'
	},
	{
		href: '/analysis-backfill',
		label: 'Analysis queue',
		does: 'Plan and watch background analysis for tracks that still need it. Running the batch still uses the command line.'
	}
];
