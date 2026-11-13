/**
 * #14166: strict parser for a skin's `layout.json` region tree (PLAN.md section 3.3).
 * Skins may reorder, nest, hide and size regions but may not invent them. Every
 * error names the key path. Pure: no IO, no store writes.
 */
import { LAYOUT_REGION_IDS, type LayoutRegionId } from '$lib/rb/layout-regions';

export const LAYOUT_MANIFEST_SCHEMA = 1;
export const LAYOUT_DIRECTIONS = ['row', 'column'] as const;
export type LayoutDirection = (typeof LAYOUT_DIRECTIONS)[number];

export interface LayoutSizePx {
	default: number;
	min: number;
	max: number;
}
export type LayoutNodeSize = LayoutSizePx | 'flex';

export interface LayoutNode {
	id?: LayoutRegionId;
	direction?: LayoutDirection;
	size?: LayoutNodeSize;
	collapsible?: boolean;
	tiers?: number[];
	children?: LayoutNode[];
}

export interface LayoutManifest {
	schema: 1;
	root: LayoutNode;
}

const MANIFEST_KEYS = ['schema', 'root'];
const NODE_KEYS = ['id', 'direction', 'size', 'collapsible', 'tiers', 'children'];
const SIZE_KEYS = ['default', 'min', 'max'];

function isObject(v: unknown): v is Record<string, unknown> {
	return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function fail(path: string, msg: string): never {
	throw new TypeError(`layout.json ${path}: ${msg}`);
}

function checkKeys(obj: Record<string, unknown>, allowed: string[], path: string): void {
	for (const k of Object.keys(obj)) {
		if (!allowed.includes(k))
			fail(`${path}.${k}`, `unknown key; allowed keys are ${allowed.join(', ')}`);
	}
}

function parseSize(v: unknown, path: string): LayoutNodeSize {
	if (v === 'flex') return 'flex';
	if (!isObject(v)) fail(path, 'must be "flex" or {default, min, max} in px');
	checkKeys(v, SIZE_KEYS, path);
	for (const k of SIZE_KEYS) {
		const n = v[k];
		if (typeof n !== 'number' || !Number.isFinite(n) || n < 0) {
			fail(`${path}.${k}`, 'must be a finite px number >= 0');
		}
	}
	const { default: def, min, max } = v as unknown as LayoutSizePx;
	if (min > max) fail(path, `min ${min} is greater than max ${max}`);
	if (def < min || def > max) fail(`${path}.default`, `${def} is outside min ${min}..max ${max}`);
	return { default: def, min, max };
}

function parseNode(v: unknown, path: string, seen: Map<string, string>): LayoutNode {
	if (!isObject(v)) fail(path, 'must be an object');
	checkKeys(v, NODE_KEYS, path);
	const out: LayoutNode = {};
	if ('id' in v) {
		const id = v.id;
		if (typeof id !== 'string' || !(LAYOUT_REGION_IDS as readonly string[]).includes(id)) {
			fail(
				`${path}.id`,
				`${JSON.stringify(id)} is not a region; allowed ids are ${LAYOUT_REGION_IDS.join(', ')}`
			);
		}
		const prev = seen.get(id);
		if (prev !== undefined)
			fail(`${path}.id`, `region ${id} already appears at ${prev}.id and ${path}.id`);
		seen.set(id, path);
		out.id = id as LayoutRegionId;
	}
	if ('direction' in v) {
		if (!(LAYOUT_DIRECTIONS as readonly unknown[]).includes(v.direction)) {
			fail(`${path}.direction`, `must be one of ${LAYOUT_DIRECTIONS.join(', ')}`);
		}
		out.direction = v.direction as LayoutDirection;
	}
	if ('size' in v) out.size = parseSize(v.size, `${path}.size`);
	if ('collapsible' in v) {
		if (typeof v.collapsible !== 'boolean') fail(`${path}.collapsible`, 'must be a boolean');
		out.collapsible = v.collapsible;
	}
	if ('tiers' in v) {
		const t = v.tiers;
		if (!Array.isArray(t)) fail(`${path}.tiers`, 'must be an array of px breakpoints');
		t.forEach((n, i) => {
			if (typeof n !== 'number' || !Number.isFinite(n) || n < 0)
				fail(`${path}.tiers[${i}]`, 'must be a finite px number >= 0');
		});
		out.tiers = [...(t as number[])];
	}
	if ('children' in v) {
		const c = v.children;
		if (!Array.isArray(c)) fail(`${path}.children`, 'must be an array of nodes');
		out.children = c.map((child, i) => parseNode(child, `${path}.children[${i}]`, seen));
	}
	return out;
}

export function parseLayoutManifest(value: unknown): LayoutManifest {
	if (!isObject(value)) fail('$', 'must be an object');
	if (value.schema !== LAYOUT_MANIFEST_SCHEMA) {
		fail(
			'$.schema',
			`got ${JSON.stringify(value.schema)}; the supported schema is ${LAYOUT_MANIFEST_SCHEMA}`
		);
	}
	checkKeys(value, MANIFEST_KEYS, '$');
	if (!('root' in value)) fail('$.root', 'is required');
	return { schema: 1, root: parseNode(value.root, '$.root', new Map()) };
}
