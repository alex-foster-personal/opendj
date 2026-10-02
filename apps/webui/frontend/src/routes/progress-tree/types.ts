/**
 * Types + constants for the /progress-tree page.
 *
 * Mirrors the canonical ledger at data/progress-tree.yaml, served by
 * GET /api/v1/progress. Everything for this route is co-located here:
 * this dir must NOT import from $lib/api.ts or $lib/rb/* (those files
 * are owned by a concurrent workflow).
 */

import { GITHUB_REPO_BASE } from '$lib/repo-config';

/** Ordered status lifecycle. 'working' is a DEPRECATED alias for 'built',
 * still accepted server-side during the fleet grace period (see
 * .planning/FANOUT-CONVENTIONS.md); the frontend never emits it and treats
 * it as 'built' wherever it might still appear in transit. */
export const STATUSES = [
	'missing',
	'spiked',
	'building',
	'partial',
	'built',
	'verified',
	'merged',
	'user-finalized'
] as const;
export type NodeStatus = (typeof STATUSES)[number];

export const EFFORTS = ['S', 'M', 'L'] as const;
export type Effort = (typeof EFFORTS)[number];

/** Build-state icon meaning, exhaustive over BuildState. */
export const BUILD_STATES = ['active', 'idle', 'blocked', 'hanging'] as const;
export type BuildState = (typeof BUILD_STATES)[number];

export interface Commit {
	sha: string;
	note: string;
}

export interface Verified {
	by: string;
	date: string;
	method: string;
}

/** Optional active-work metadata. Absent unless a node is under active work;
 * 'updated' is stamped server-side on any PATCH that touches build. */
export interface Build {
	branch: string | null;
	pr: string | null;
	worktree: string | null;
	stage: string | null;
	state: BuildState | null;
	updated: string | null;
}

/** Exhaustive build-state -> glyph map. Unicode, not SVG: renders identically
 * inline in HTML rows/panels and inside the graph tab's SVG chips. */
export const BUILD_STATE_GLYPH: Record<BuildState, string> = {
	active: '●', // filled circle, pulses via CSS
	idle: '⏸', // pause
	blocked: '✋', // raised hand
	hanging: '⚠' // warning triangle
};

export const BUILD_STATE_LABEL: Record<BuildState, string> = {
	active: 'active',
	idle: 'idle',
	blocked: 'blocked',
	hanging: 'hanging'
};

/** Optional cross-references. issues are GitHub issue/PR numbers (rendered
 * as a GitHub icon chip), specs are file paths (repo-relative preferred),
 * refs are plain urls. */
export interface Links {
	issues: string[];
	specs: string[];
	refs: string[];
}

/** WHERE a node can be iteratively built: cloud (Claude Code cloud / any
 * remote sandbox) vs this Mac. Criterion: cloud-suitable = the iterative
 * build+test loop never needs local-only resources (a user's real library,
 * real ANLZ/audio, hardware, remote hosts). Full rationale + per-node reasons
 * in .planning/rekordbox-parity/CLOUD-BUILDABILITY.md. */
export const TIERS = ['cloud', 'hybrid', 'local'] as const;
export type BuildableTier = (typeof TIERS)[number];

export interface Buildable {
	tier: BuildableTier;
	reason: string;
}

export interface ProgressNode {
	id: string;
	title: string;
	status: NodeStatus;
	effort: Effort;
	reuse: string | null;
	deps: string[];
	commits: Commit[];
	tests: string[];
	verified: Verified | null;
	notes: string | null;
	build: Build | null;
	links: Links | null;
	buildable: Buildable | null;
}

export interface ProgressArea {
	id: string;
	title: string;
	nodes: ProgressNode[];
}

export interface ProgressMeta {
	branch: string;
	updated: string;
	convention: string;
}

/** Last commit touching the ledger file. All fields are null when the
 * file has no committed history yet (backend returns explicit nulls). */
export interface FileGit {
	last_sha: string | null;
	last_author: string | null;
	last_date: string | null;
}

export interface ProgressResponse {
	meta: ProgressMeta;
	areas: ProgressArea[];
	file_git: FileGit;
}

/** Statuses exempt from the commits_append rule: missing/spiked represent
 * absence or investigation-only outcomes, so there is no code to cite.
 * Mirrors backend _STATUSES_WITHOUT_COMMITS exactly. */
const STATUSES_WITHOUT_COMMITS: ReadonlySet<NodeStatus> = new Set(['missing', 'spiked']);

/** Any non-exempt status with zero commits breaks the ledger convention:
 * status changes MUST append commits. Rendered as a red tag. */
export function hasProvenanceGap(node: ProgressNode): boolean {
	return !STATUSES_WITHOUT_COMMITS.has(node.status) && node.commits.length === 0;
}

/** Exhaustive status -> css class map; TS enforces every status is styled. */
export const STATUS_CLASS: Record<NodeStatus, string> = {
	missing: 'st-missing',
	spiked: 'st-spiked',
	building: 'st-building',
	partial: 'st-partial',
	built: 'st-built',
	verified: 'st-verified',
	merged: 'st-merged',
	'user-finalized': 'st-user-finalized'
};

/** One status swatch (fg text / border / fill). Single source of truth for
 * both the CSS chips in StatusChip and the inline SVG chips in the graph tab -
 * the hex values here mirror StatusChip's stylesheet exactly. */
export interface StatusSwatch {
	fg: string;
	border: string;
	bg: string;
}

/** Exhaustive status -> colour swatch. TS enforces every status is covered. */
export const STATUS_PALETTE: Record<NodeStatus, StatusSwatch> = {
	missing: { fg: '#9aa4b2', border: '#3a4250', bg: '#1a212c' },
	spiked: { fg: '#c4b5fd', border: '#6d5bb8', bg: '#241d3a' },
	building: { fg: '#ffb43a', border: '#7a5a1f', bg: '#2a2110' },
	partial: { fg: '#7cc0ff', border: '#2c5d8f', bg: '#12233a' },
	built: { fg: '#4ade80', border: '#1f6b3d', bg: '#0f2a1a' },
	verified: { fg: '#4ade80', border: '#4ade80', bg: '#0f2a1a' },
	merged: { fg: '#c4a4fb', border: '#7c3aed', bg: '#241a3d' },
	'user-finalized': { fg: '#fef08a', border: '#eab308', bg: '#2b2410' }
};

/** Per-tier presentation: colour (go/caution/stop, matching the cloud-push
 * intent), one-word label, and a compact letter for the graph chip badge.
 * Exhaustive over BuildableTier so TS flags a missing tier. */
export interface TierMeta {
	label: string;
	color: string;
	letter: string;
}
export const TIER_META: Record<BuildableTier, TierMeta> = {
	cloud: { label: 'cloud', color: '#34d399', letter: 'C' },
	hybrid: { label: 'hybrid', color: '#fbbf24', letter: 'H' },
	local: { label: 'local', color: '#f87171', letter: 'L' }
};

/** Inline SVG glyph paths (24x24 viewBox), dependency-free like
 * GITHUB_MARK_PATH. cloud = a cloud (fully cloud-buildable), laptop = this
 * Mac (local-only), hybrid draws both (cloud + a small laptop). Rendered by
 * TierIcon.svelte in HTML surfaces; the graph uses a compact letter badge. */
export const TIER_CLOUD_PATH =
	'M19.35 10.04C18.67 6.59 15.64 4 12 4 9.11 4 6.6 5.64 5.35 8.04 2.34 8.36 0 ' +
	'10.91 0 14c0 3.31 2.69 6 6 6h13c2.76 0 5-2.24 5-5 0-2.64-2.05-4.78-4.65-4.96z';
export const TIER_LAPTOP_PATH =
	'M5 7a1 1 0 0 1 1-1h12a1 1 0 0 1 1 1v8H5z ' +
	'M3 16h18l1 2.5a1 1 0 0 1-.94 1.5H2.94A1 1 0 0 1 2 18.5z';

/** One tooltip string for a node's buildable tier + reason, or null when a
 * node carries no classification (older data / mid-seed). */
export function buildableTooltip(buildable: Buildable | null): string | null {
	if (buildable === null) return null;
	return `buildable: ${TIER_META[buildable.tier].label} - ${buildable.reason}`;
}

/** Statuses that still have outstanding work (i.e. not built/verified/merged/
 * user-finalized). These are the nodes the wave scheduler and default graph
 * view care about. */
export const OUTSTANDING_STATUSES: ReadonlySet<NodeStatus> = new Set([
	'missing',
	'spiked',
	'building',
	'partial'
]);

export function isOutstanding(node: ProgressNode): boolean {
	return OUTSTANDING_STATUSES.has(node.status);
}

/** A dep counts as met when its target is already done (built/verified/
 * merged/user-finalized) -- i.e. anything past the outstanding set. */
export function isDoneStatus(status: NodeStatus): boolean {
	return !OUTSTANDING_STATUSES.has(status);
}

/** Lane tags live inside notes as one or more 'LANE <name>:' prefixes/segments.
 * Returns the distinct lane names in first-seen order. */
export function parseLanes(notes: string | null): string[] {
	if (notes === null) return [];
	const out: string[] = [];
	const re = /LANE ([^:]+):/g;
	let m: RegExpExecArray | null;
	while ((m = re.exec(notes)) !== null) {
		const name = m[1].trim();
		if (name !== '' && !out.includes(name)) out.push(name);
	}
	return out;
}

/** Notes are multi-part: distinct agents append ' | '-joined segments. Split
 * into trimmed, non-empty bullet segments for rendering. */
export function noteSegments(notes: string | null): string[] {
	if (notes === null) return [];
	return notes
		.split(' | ')
		.map((s) => s.trim())
		.filter((s) => s !== '');
}

/** A node earns an expandable fold-out when it carries more than a little
 * detail: long/multi-part notes, several commits, tests, verified, or deps. */
export function hasFoldoutDetail(node: ProgressNode): boolean {
	const notes = node.notes ?? '';
	return (
		notes.length > 90 ||
		notes.includes(' | ') ||
		node.commits.length > 2 ||
		node.tests.length > 0 ||
		node.verified !== null ||
		node.deps.length > 0 ||
		node.build !== null ||
		node.links !== null
	);
}

/** Deterministic, stable colour per lane name (consistent everywhere it is
 * drawn - badges, graph halos, legend). Hue derived from the name so a lane
 * keeps its colour across renders without threading a shared map around. */
export function laneColor(lane: string): string {
	let hue = 0;
	for (let i = 0; i < lane.length; i += 1) {
		hue = (hue * 31 + lane.charCodeAt(i)) % 360;
	}
	return `hsl(${hue}, 58%, 62%)`;
}

/** How stale a build.updated timestamp is, for the dim 'stale <relative
 * time>' tag. null ONLY when the claim is well-formed and genuinely fresh.
 *
 * A missing or unparseable stamp returns a label rather than null, and that
 * is the whole point: build.updated is what makes a claim take-over-able
 * after the 3h lease in FANOUT-CONVENTIONS.md. Returning null for a stampless
 * claim rendered no tag on any of the three surfaces that call this
 * (NodeRow, NodeDetail, DepGraph), so a malformed claim could never look
 * stale and therefore could never be taken over -- it locked the node
 * permanently. Surfacing the malformed state is strictly safer than hiding
 * it, and it does not change the 3h semantics for well-formed claims. */
const STALE_AFTER_MS = 3 * 60 * 60 * 1000; // 3h

export function staleBuildLabel(updated: string | null, now: Date = new Date()): string | null {
	if (updated === null) return 'stale (unstamped)';
	const ts = Date.parse(updated);
	if (Number.isNaN(ts)) {
		console.error(`progress: build.updated '${updated}' is not a parseable timestamp`);
		return 'stale (bad timestamp)';
	}
	const ageMs = now.getTime() - ts;
	if (ageMs < STALE_AFTER_MS) return null;
	const hours = Math.floor(ageMs / (60 * 60 * 1000));
	if (hours < 24) return `stale ${hours}h`;
	const days = Math.floor(hours / 24);
	return `stale ${days}d`;
}

/** GitHub repo base for issue/PR chip links. Defined once in
 * `$lib/repo-config` and re-exported here so the route keeps one import site.
 * progress-repo-base.test.mjs asserts it stays a credential-free https URL
 * that names either this checkout's origin or the public repository. */
export { GITHUB_REPO_BASE };

export function githubIssueUrl(issue: string): string {
	return `${GITHUB_REPO_BASE}/issues/${issue}`;
}

/** Octicons 'mark-github' path (MIT-licensed, github/octicons), 16x16
 * viewBox. Used as the issue-link chip glyph so the link is recognisable
 * as GitHub at a glance, dependency-free (no icon package import). */
export const GITHUB_MARK_PATH =
	'M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49' +
	'-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 ' +
	'1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-' +
	'1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 ' +
	'2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 ' +
	'3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 ' +
	'0 16 8c0-4.42-3.58-8-8-8z';

/** node id -> ids of nodes that declare it as a dep ("blocks: ..."). Computed
 * once per render from the flat node list; both the tree fold-out and the
 * graph side panel show the same reverse-dep chips via this map. */
export function buildReverseDeps(nodes: ProgressNode[]): Map<string, string[]> {
	const map = new Map<string, string[]>();
	for (const node of nodes) {
		for (const dep of node.deps) {
			const list = map.get(dep) ?? [];
			list.push(node.id);
			map.set(dep, list);
		}
	}
	return map;
}
