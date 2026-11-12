// bundle-budget-load.mjs -- reads the per-surface budgets from budgets/ (ADR-NEW-bundle-budget-charges).
//
// Every PR that grew a surface used to rewrite the same `limit:` scalar in bundle-budget.mjs and
// append to the same comment wall, so bundle-budget.mjs sat in 60 of the 293 conflicting re-merges
// of main in the 7 days to Tue 3 Nov 2026. Now the numbers are files a PR only ADDS:
//
//   budgets/<surface>/base.json            the surface's base limit and how it is attributed;
//                                          rewritten only by a rebaseline on main
//   budgets/<surface>/charges/<id>.json    one PR's measured growth of that surface
//
// and the gate enforces   limit = base.limit + sum(charge.bytes).
// Two PRs that each charge their own growth no longer race on one number, and the sum stays right
// under merge skew, because each charge is that PR's own delta, not a new absolute ceiling.
//
// Rebaseline (on main only, folding charges into the base): limit = measured, ceiled to KiB,
// plus 1 KiB; then delete the folded charge files in the same commit.
//
// Everything here fails loudly. A malformed or stray file is an error, never a skipped file:
// a charge nobody reads would be a budget nobody enforces.

import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

export const BUDGETS_DIR = fileURLToPath(new URL('../budgets/', import.meta.url));
export const KIB = 1024;
export const GRAPH_SURFACES = ['library', 'performance', 'other'];

/** Git branches may use `/`; charge files allow only letters, digits, `.`, `_`, and `-`. */
export function chargeBranchId(branch) {
	return branch.replaceAll('/', '--');
}

const BASE_KEYS = [
	'note',
	'order',
	'attribution',
	'limit',
	'measured',
	'measured_on',
	'baseline_sha'
];
const CHARGE_KEYS = [
	'bytes',
	'measured_main',
	'measured_with_pr',
	'sha',
	'date',
	'reason',
	'payback'
];
const SURFACE_ENTRIES = new Set(['base.json', 'charges', 'HISTORY.md']);
// Ratchets that live under budgets/ but are not gzip bundle surfaces; each has its own script.
const NON_BUNDLE_DIRS = new Set(['story-coverage']); // scripts/story-coverage.mjs
const CHARGE_NAME = /^[a-z0-9][a-z0-9._-]*\.json$/;
const SHA = /^[0-9a-f]{7,40}$/;
const DATE =
	/^(Mon|Tue|Wed|Thu|Fri|Sat|Sun) (\d{1,2}) (Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec) (\d{4})$/;
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

export class BudgetError extends Error {}

// ---------------------------------------------------------------- helpers ---

function _readJson(path) {
	let text;
	try {
		text = readFileSync(path, 'utf8');
	} catch (error) {
		throw new BudgetError(`${path}: cannot read: ${error.message}`);
	}
	try {
		return JSON.parse(text);
	} catch (error) {
		throw new BudgetError(`${path}: not valid JSON: ${error.message}`);
	}
}

function _exactKeys(where, object, keys) {
	if (object === null || typeof object !== 'object' || Array.isArray(object)) {
		throw new BudgetError(`${where}: must be a JSON object`);
	}
	const missing = keys.filter((key) => !(key in object));
	const unknown = Object.keys(object).filter((key) => !keys.includes(key));
	if (missing.length || unknown.length) {
		throw new BudgetError(
			`${where}: keys must be exactly ${keys.join(', ')}` +
				(missing.length ? `; missing ${missing.join(', ')}` : '') +
				(unknown.length ? `; unknown ${unknown.join(', ')}` : '')
		);
	}
}

function _positiveInt(where, key, value) {
	if (!Number.isInteger(value) || value <= 0)
		throw new BudgetError(`${where}: ${key} must be a positive integer, got ${value}`);
}

function _text(where, key, value) {
	if (typeof value !== 'string' || value.trim() === '')
		throw new BudgetError(`${where}: ${key} must be non-empty text`);
}

function _sha(where, key, value) {
	if (typeof value !== 'string' || !SHA.test(value))
		throw new BudgetError(`${where}: ${key} must be a 7 to 40 char hex sha, got ${value}`);
}

/** 'Tue 3 Nov 2026', with the weekday checked against the date (it is the checksum). */
export function checkDate(where, key, value) {
	const match = typeof value === 'string' ? value.match(DATE) : null;
	if (!match) throw new BudgetError(`${where}: ${key} must read 'Ddd D Mon YYYY', got ${value}`);
	const [, weekday, day, month, year] = match;
	const parsed = new Date(Date.UTC(Number(year), MONTHS.indexOf(month), Number(day)));
	if (parsed.getUTCDate() !== Number(day))
		throw new BudgetError(`${where}: ${key} ${value} is not a calendar date`);
	const actual = WEEKDAYS[parsed.getUTCDay()];
	if (actual !== weekday)
		throw new BudgetError(`${where}: ${key} ${value} is a ${actual}, wrong weekday`);
}

export function ceilToKiB(bytes) {
	return Math.ceil(bytes / KIB) * KIB;
}

// ---------------------------------------------------------------- schema ---

function _checkAttribution(where, attribution) {
	if (attribution === null || typeof attribution !== 'object' || Array.isArray(attribution)) {
		throw new BudgetError(`${where}: attribution must be {"graph": ...} or {"markers": [...]}`);
	}
	const keys = Object.keys(attribution);
	if (keys.length === 1 && keys[0] === 'graph') {
		if (!GRAPH_SURFACES.includes(attribution.graph)) {
			throw new BudgetError(
				`${where}: attribution.graph must be one of ${GRAPH_SURFACES.join(', ')}`
			);
		}
		return;
	}
	if (keys.length === 1 && keys[0] === 'markers') {
		const { markers } = attribution;
		if (
			!Array.isArray(markers) ||
			markers.length === 0 ||
			!markers.every((m) => typeof m === 'string' && m !== '')
		) {
			throw new BudgetError(
				`${where}: attribution.markers must be a non-empty list of non-empty strings`
			);
		}
		return;
	}
	throw new BudgetError(
		`${where}: attribution must be {"graph": ...} or {"markers": [...]}, got keys ${keys.join(', ')}`
	);
}

function _checkBase(where, base) {
	_exactKeys(where, base, BASE_KEYS);
	_text(where, 'note', base.note);
	if (!Number.isInteger(base.order)) throw new BudgetError(`${where}: order must be an integer`);
	_checkAttribution(where, base.attribution);
	_positiveInt(where, 'limit', base.limit);
	_positiveInt(where, 'measured', base.measured);
	checkDate(where, 'measured_on', base.measured_on);
	_sha(where, 'baseline_sha', base.baseline_sha);
}

/**
 * A charge is one PR's own measured growth of one surface. `bytes` may carry at most the
 * rebaseline rule's headroom over that growth (ceil to KiB, plus 1 KiB), so a charge cannot
 * smuggle in slack the measurement does not show.
 */
export function checkCharge(where, charge) {
	_exactKeys(where, charge, CHARGE_KEYS);
	for (const key of ['bytes', 'measured_main', 'measured_with_pr'])
		_positiveInt(where, key, charge[key]);
	_sha(where, 'sha', charge.sha);
	checkDate(where, 'date', charge.date);
	_text(where, 'reason', charge.reason);
	_text(where, 'payback', charge.payback);
	const growth = charge.measured_with_pr - charge.measured_main;
	if (growth <= 0) {
		throw new BudgetError(
			`${where}: measured_with_pr ${charge.measured_with_pr} is not above measured_main ${charge.measured_main}; ` +
				'a PR that does not grow the surface needs no charge'
		);
	}
	const ceiling = ceilToKiB(growth) + KIB;
	if (charge.bytes > ceiling) {
		throw new BudgetError(
			`${where}: bytes ${charge.bytes} exceeds the measured growth ${growth} ceiled to KiB plus 1 KiB (${ceiling})`
		);
	}
}

// ---------------------------------------------------------------- loader ---

function _loadCharges(surfaceDir, surface) {
	const chargesDir = join(surfaceDir, 'charges');
	let names;
	try {
		names = readdirSync(chargesDir).sort();
	} catch (error) {
		if (error.code === 'ENOENT') return [];
		throw new BudgetError(`budgets/${surface}/charges: cannot list: ${error.message}`);
	}
	return names.map((name) => {
		const where = `budgets/${surface}/charges/${name}`;
		if (!CHARGE_NAME.test(name) || !statSync(join(chargesDir, name)).isFile()) {
			throw new BudgetError(
				`${where}: not a charge file; a charge is budgets/${surface}/charges/<pr>.json`
			);
		}
		const charge = _readJson(join(chargesDir, name));
		checkCharge(where, charge);
		return { id: name.replace(/\.json$/, ''), ...charge };
	});
}

/**
 * Every surface under `dir`, in charge order (a file reachable from several surfaces is
 * charged to the first). Each carries its base, its charges and the limit the gate enforces.
 */
export function loadBudgets(dir = BUDGETS_DIR) {
	let entries;
	try {
		entries = readdirSync(dir).sort();
	} catch (error) {
		throw new BudgetError(`${dir}: cannot list the budgets directory: ${error.message}`);
	}
	const budgets = [];
	for (const name of entries) {
		if (NON_BUNDLE_DIRS.has(name)) continue;
		const surfaceDir = join(dir, name);
		if (!statSync(surfaceDir).isDirectory()) {
			if (name === 'README.md' || name === 'HISTORY.md') continue;
			throw new BudgetError(
				`budgets/${name}: only surface directories, README.md and HISTORY.md belong here`
			);
		}
		const strays = readdirSync(surfaceDir).filter((entry) => !SURFACE_ENTRIES.has(entry));
		if (strays.length) {
			throw new BudgetError(
				`budgets/${name}/: ${strays.join(', ')} is not read by the gate; a surface holds base.json, charges/ and HISTORY.md`
			);
		}
		const base = _readJson(join(surfaceDir, 'base.json'));
		_checkBase(`budgets/${name}/base.json`, base);
		const charges = _loadCharges(surfaceDir, name);
		const charged = charges.reduce((sum, charge) => sum + charge.bytes, 0);
		budgets.push({ name, base, charges, charged, limit: base.limit + charged, note: base.note });
	}
	if (budgets.length === 0)
		throw new BudgetError(`${dir}: no surfaces; the gate would measure nothing`);
	const orders = budgets.map((b) => b.base.order);
	if (new Set(orders).size !== orders.length)
		throw new BudgetError('two surfaces share one order; charge order would be ambiguous');
	for (const graph of GRAPH_SURFACES) {
		const owners = budgets.filter((b) => b.base.attribution.graph === graph);
		if (owners.length !== 1) {
			throw new BudgetError(
				`exactly one surface must own graph "${graph}", found ${owners.map((b) => b.name).join(', ') || 'none'}`
			);
		}
	}
	budgets.sort((a, b) => a.base.order - b.base.order);
	// library pays first, performance second and other last; a marker surface ordered after
	// other would find every chunk already charged and measure nothing.
	const rank = (b) => ({ library: 0, performance: 1, other: 3 })[b.base.attribution.graph] ?? 2;
	const ranks = budgets.map(rank);
	if (ranks.some((r, i) => i > 0 && r < ranks[i - 1])) {
		throw new BudgetError(
			`surface order must be library, performance, marker surfaces, then other; got ${budgets.map((b) => b.name).join(', ')}`
		);
	}
	return budgets;
}
