/**
 * Where a KPI reading came from, and how the card says so.
 *
 * The whole point of the admin panel is deciding what to believe. A number
 * somebody typed and a number derived from telemetry must never be pixel
 * identical, and a config constant must never read as an observed measurement -
 * that is how a near-serial farm looks healthy because `max_parallel_gpus` says
 * what the run was ALLOWED to reach.
 *
 * Kept out of KpiTile.svelte so the rule is testable on its own: a component
 * cannot be imported by the node --test unit suite, and this rule is exactly
 * the kind a refactor flattens without noticing.
 */

/** Provenance values the ledger writes. See routes/admin/kpi-api.ts. */
export type KpiOrigin =
	| 'derived'
	| 'hand'
	| 'hand-unverifiable'
	| 'configured-not-measured'
	| 'not-derivable'
	| 'unmeasured';

/** How the latest reading got here, so measured and typed never look alike. */
export const ORIGIN_TEXT: Record<string, string> = {
	derived: 'Origin: DERIVED from per-track cache telemetry by scripts/bench/kpi_derive.py.',
	hand: 'Origin: HAND-ENTERED. Not reconstructable from telemetry, so nothing checks it.',
	'hand-unverifiable':
		'Origin: HAND-ENTERED and unverifiable. No telemetry survives for this run.',
	'configured-not-measured':
		'Origin: CONFIG CONSTANT, not a measurement. It is the ceiling the run was allowed, ' +
		'not what it reached.'
};

/**
 * Corner badge for a reading, or '' for no badge.
 *
 * `derived` deliberately has NO badge: badging every card trains the eye to
 * ignore badges, and derived is the honest default this panel is built around.
 */
export const BADGE_TEXT: Record<string, string> = {
	hand: 'typed',
	'hand-unverifiable': 'typed',
	'configured-not-measured': 'config'
};

/** Badge for one origin string ('' = no badge). Unknown origins get no badge. */
export function originBadge(origin: string | undefined): string {
	return BADGE_TEXT[origin ?? ''] ?? '';
}

/** Explainer sentence for one origin string, or null when there is nothing to say. */
export function originText(origin: string | undefined): string | null {
	return ORIGIN_TEXT[origin ?? ''] ?? null;
}

/** True when a reading is a human keystroke or a config constant, not a measurement. */
export function isUnmeasuredOrigin(origin: string | undefined): boolean {
	return originBadge(origin) !== '';
}
