/**
 * Route-local fetch helper for GET /api/v1/progress.
 *
 * Deliberately NOT in $lib/api.ts (owned by a concurrent workflow).
 * Fail-fast: the response is structurally validated before render;
 * unknown statuses/efforts, duplicate node ids, or deps pointing at
 * unknown node ids throw explicit errors instead of rendering junk.
 * The ledger is canonical shared state for parallel agents, so
 * surfacing corruption loudly is the point.
 */

import {
	BUILD_STATES,
	EFFORTS,
	STATUSES,
	type Build,
	type BuildState,
	type Commit,
	type Effort,
	type FileGit,
	type Links,
	type NodeStatus,
	type ProgressArea,
	type ProgressMeta,
	type ProgressNode,
	type ProgressResponse,
	type Verified
} from './types';

const BASE = typeof window === 'undefined' ? 'http://127.0.0.1:8585' : '';

//----- primitive validators -----------------------------------------------

function asObject(v: unknown, ctx: string): Record<string, unknown> {
	if (typeof v !== 'object' || v === null || Array.isArray(v)) {
		throw new Error(`progress: ${ctx} is not an object`);
	}
	return v as Record<string, unknown>;
}

function asString(v: unknown, ctx: string): string {
	if (typeof v !== 'string') throw new Error(`progress: ${ctx} is not a string`);
	return v;
}

function asStringOrNull(v: unknown, ctx: string): string | null {
	// undefined means the key is absent: that is a schema violation, not a null.
	if (v === undefined) throw new Error(`progress: ${ctx} is missing (expected string or null)`);
	if (v === null) return null;
	return asString(v, ctx);
}

function asArray(v: unknown, ctx: string): unknown[] {
	if (!Array.isArray(v)) throw new Error(`progress: ${ctx} is not an array`);
	return v;
}

//----- schema validators ---------------------------------------------------

/** 'working' is a deprecated alias for 'built' (see FANOUT-CONVENTIONS.md);
 * tolerated in transit in case a not-yet-restarted daemon still emits it. */
function parseStatus(v: unknown, ctx: string): NodeStatus {
	const s = asString(v, ctx);
	if (s === 'working') return 'built';
	if (!(STATUSES as readonly string[]).includes(s)) {
		throw new Error(`progress: ${ctx} has unknown status '${s}' (expected ${STATUSES.join('|')})`);
	}
	return s as NodeStatus;
}

function parseEffort(v: unknown, ctx: string): Effort {
	const s = asString(v, ctx);
	if (!(EFFORTS as readonly string[]).includes(s)) {
		throw new Error(`progress: ${ctx} has unknown effort '${s}' (expected ${EFFORTS.join('|')})`);
	}
	return s as Effort;
}

function parseCommit(v: unknown, ctx: string): Commit {
	const o = asObject(v, ctx);
	return { sha: asString(o.sha, `${ctx}.sha`), note: asString(o.note, `${ctx}.note`) };
}

function parseVerified(v: unknown, ctx: string): Verified | null {
	if (v === undefined) throw new Error(`progress: ${ctx} is missing (expected object or null)`);
	if (v === null) return null;
	const o = asObject(v, ctx);
	return {
		by: asString(o.by, `${ctx}.by`),
		date: asString(o.date, `${ctx}.date`),
		method: asString(o.method, `${ctx}.method`)
	};
}

/** Sub-field of an optional object (build): absent key and explicit null
 * both mean "not set" -- unlike top-level notes/reuse, the backend may omit
 * the key entirely rather than emit null. */
function asOptionalString(v: unknown, ctx: string): string | null {
	if (v === undefined || v === null) return null;
	return asString(v, ctx);
}

/** build/links are OPTIONAL objects: entirely absent from the node dict
 * unless the node is under active work / carries cross-references. */
function parseBuild(v: unknown, ctx: string): Build | null {
	if (v === undefined || v === null) return null;
	const o = asObject(v, ctx);
	const state = o.state;
	if (state !== undefined && state !== null && !(BUILD_STATES as readonly string[]).includes(state as string)) {
		throw new Error(
			`progress: ${ctx}.state has unknown value '${String(state)}' (expected ${BUILD_STATES.join('|')})`
		);
	}
	return {
		branch: asOptionalString(o.branch, `${ctx}.branch`),
		pr: asOptionalString(o.pr, `${ctx}.pr`),
		worktree: asOptionalString(o.worktree, `${ctx}.worktree`),
		stage: asOptionalString(o.stage, `${ctx}.stage`),
		state: state === undefined || state === null ? null : (state as BuildState),
		updated: asOptionalString(o.updated, `${ctx}.updated`)
	};
}

function asStringArrayOrEmpty(v: unknown, ctx: string): string[] {
	if (v === undefined || v === null) return [];
	return asArray(v, ctx).map((s, i) => asString(s, `${ctx}[${i}]`));
}

function parseLinks(v: unknown, ctx: string): Links | null {
	if (v === undefined || v === null) return null;
	const o = asObject(v, ctx);
	return {
		issues: asStringArrayOrEmpty(o.issues, `${ctx}.issues`),
		specs: asStringArrayOrEmpty(o.specs, `${ctx}.specs`),
		refs: asStringArrayOrEmpty(o.refs, `${ctx}.refs`)
	};
}

function parseNode(v: unknown, ctx: string): ProgressNode {
	const o = asObject(v, ctx);
	const id = asString(o.id, `${ctx}.id`);
	const nodeCtx = `node '${id}'`;
	return {
		id,
		title: asString(o.title, `${nodeCtx}.title`),
		status: parseStatus(o.status, `${nodeCtx}.status`),
		effort: parseEffort(o.effort, `${nodeCtx}.effort`),
		reuse: asStringOrNull(o.reuse, `${nodeCtx}.reuse`),
		deps: asArray(o.deps, `${nodeCtx}.deps`).map((d, i) => asString(d, `${nodeCtx}.deps[${i}]`)),
		commits: asArray(o.commits, `${nodeCtx}.commits`).map((c, i) =>
			parseCommit(c, `${nodeCtx}.commits[${i}]`)
		),
		tests: asArray(o.tests, `${nodeCtx}.tests`).map((t, i) =>
			asString(t, `${nodeCtx}.tests[${i}]`)
		),
		verified: parseVerified(o.verified, `${nodeCtx}.verified`),
		notes: asStringOrNull(o.notes, `${nodeCtx}.notes`),
		build: parseBuild(o.build, `${nodeCtx}.build`),
		links: parseLinks(o.links, `${nodeCtx}.links`)
	};
}

function parseArea(v: unknown, ctx: string): ProgressArea {
	const o = asObject(v, ctx);
	const id = asString(o.id, `${ctx}.id`);
	return {
		id,
		title: asString(o.title, `area '${id}'.title`),
		nodes: asArray(o.nodes, `area '${id}'.nodes`).map((n, i) =>
			parseNode(n, `area '${id}'.nodes[${i}]`)
		)
	};
}

function parseMeta(v: unknown): ProgressMeta {
	const o = asObject(v, 'meta');
	return {
		branch: asString(o.branch, 'meta.branch'),
		updated: asString(o.updated, 'meta.updated'),
		convention: asString(o.convention, 'meta.convention')
	};
}

function parseFileGit(v: unknown): FileGit {
	// Backend returns explicit nulls when the ledger has no git history yet.
	const o = asObject(v, 'file_git');
	return {
		last_sha: asStringOrNull(o.last_sha, 'file_git.last_sha'),
		last_author: asStringOrNull(o.last_author, 'file_git.last_author'),
		last_date: asStringOrNull(o.last_date, 'file_git.last_date')
	};
}

export function validateProgressResponse(raw: unknown): ProgressResponse {
	const o = asObject(raw, 'response');
	const meta = parseMeta(o.meta);
	const areas = asArray(o.areas, 'areas').map((a, i) => parseArea(a, `areas[${i}]`));
	const fileGit = parseFileGit(o.file_git);

	// Cross-checks: node ids globally unique; deps reference known nodes.
	const seen = new Set<string>();
	for (const area of areas) {
		for (const node of area.nodes) {
			if (seen.has(node.id)) throw new Error(`progress: duplicate node id '${node.id}'`);
			seen.add(node.id);
		}
	}
	for (const area of areas) {
		for (const node of area.nodes) {
			for (const dep of node.deps) {
				if (!seen.has(dep)) {
					throw new Error(`progress: node '${node.id}' dep '${dep}' does not exist`);
				}
			}
		}
	}
	return { meta, areas, file_git: fileGit };
}

export async function fetchProgress(): Promise<ProgressResponse> {
	let r: Response;
	try {
		r = await fetch(`${BASE}/api/v1/progress`, { headers: { Accept: 'application/json' } });
	} catch (e) {
		throw new Error(`daemon unreachable at :8585 (${e instanceof Error ? e.message : String(e)})`);
	}
	if (!r.ok) {
		const body = await r.text();
		throw new Error(`GET /api/v1/progress failed: ${r.status} ${body.slice(0, 300)}`);
	}
	return validateProgressResponse(await r.json());
}
