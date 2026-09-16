/**
 * The root Playwright config must ignore every spec that owns a dedicated config.
 *
 * `apps/webui/frontend/playwright.config.ts` starts vite and nothing else, so a
 * spec written for a harness that supplies a backend, a production build or its
 * own environment cannot run there. Several of them say so by throwing at MODULE
 * SCOPE when their variable is unset, and ONE module-scope throw fails
 * Playwright's whole collection: the root suite then reports
 * "Total: 0 tests in 0 files" and the e2e gate is red for every lane, not just
 * the new spec's.
 *
 * That has now happened twice from the same cause -- a commit adding a spec plus
 * its own config without adding the matching `testIgnore` entry:
 * `stem-decode-bench.spec.ts` (#1509) and `kpi-boot-library-capture.spec.ts`
 * (2942a081e, which took the e2e gate red on main with
 * "KPI_CAPTURE_TIMEOUT_S is required"). Review cannot see the omission, because
 * the new files are self-consistent and the file they forgot is somewhere else.
 * This check can.
 *
 * Acceptance tests:
 *
 * - [if] a dedicated config names a spec the root config does not ignore
 *   [then ⛔️] this test fails, naming the spec and the config that claims it.
 * - [if] a spec is on the deliberate double-run allowlist
 *   [then] it is not reported, because running under both harnesses is its
 *   documented behaviour.
 * - [if] a dedicated config's testMatch is a regex or an expression
 *   [then] it is skipped rather than guessed at, and the skip is counted so a
 *   parser that silently matched nothing cannot pass as a green check.
 */
import assert from 'node:assert/strict';
import { readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';
import test from 'node:test';

const FRONTEND_ROOT = join(dirname(fileURLToPath(import.meta.url)), '..', '..');
const E2E_DIR = join(FRONTEND_ROOT, 'tests', 'e2e');
const ROOT_CONFIG = join(FRONTEND_ROOT, 'playwright.config.ts');

/**
 * Specs that deliberately run under BOTH the root harness and a dedicated one.
 * Each is harness-agnostic (nothing is read at module scope), so the double run
 * is extra coverage rather than an omission. The root config's own comment
 * documents setup-entry-points; the other two ride the same webkit artifact
 * config and are equally safe under plain vite/chromium.
 */
const DELIBERATE_DOUBLE_RUNS = new Set([
	'setup-entry-points.spec.ts',
	'autoplay-explainer-placement.spec.ts',
	'zz-autoplay-playlist-switch.spec.ts'
]);

/** The specs named by a config's TOP-LEVEL testMatch, or null if it is not a literal. */
function declaredSpecs(source) {
	const single = source.match(/^\ttestMatch: '([^']+)',$/m);
	if (single) return [single[1]];
	const list = source.match(/^\ttestMatch: \[\n([\s\S]*?)^\t\],$/m);
	if (!list) return null; // a regex, or an expression such as `process.env.X ?? '...'`
	const entries = [...list[1].matchAll(/'([^']+)'/g)].map((match) => match[1]);
	return entries.length > 0 ? entries : null;
}

/** Ignore globs, reduced to the spec names they cover. */
function ignoreMatchers(source) {
	const block = source.match(/\ttestIgnore: \[\n([\s\S]*?)\n\t\],/m);
	assert.ok(block, 'the root config must declare a testIgnore array');
	return [...block[1].matchAll(/'\*\*\/([^']+)'/g)].map((match) => {
		const pattern = match[1];
		if (!pattern.includes('*')) return (name) => name === pattern;
		const [prefix, suffix] = pattern.split('*');
		return (name) => name.startsWith(prefix) && name.endsWith(suffix);
	});
}

test('if a dedicated Playwright config claims a spec then the root config ignores it', () => {
	const isIgnored = ignoreMatchers(readFileSync(ROOT_CONFIG, 'utf-8'));
	const configs = readdirSync(E2E_DIR).filter(
		(name) => name.startsWith('playwright.') && name.endsWith('.config.ts')
	);
	assert.ok(configs.length >= 20, `expected the dedicated configs, found ${configs.length}`);

	const unparsed = [];
	const leaked = [];
	for (const config of configs) {
		const specs = declaredSpecs(readFileSync(join(E2E_DIR, config), 'utf-8'));
		if (specs === null) {
			unparsed.push(config);
			continue;
		}
		for (const spec of specs) {
			if (DELIBERATE_DOUBLE_RUNS.has(spec)) continue;
			if (!isIgnored.some((matches) => matches(spec))) {
				leaked.push(`${spec} (claimed by ${config})`);
			}
		}
	}

	assert.deepEqual(
		leaked,
		[],
		`these specs own a dedicated config but still run under the root harness, where one ` +
			`module-scope throw zeroes the whole suite; add each to testIgnore in ` +
			`playwright.config.ts:\n  ${leaked.join('\n  ')}`
	);
	// A parser that matched nothing would report zero leaks forever.
	assert.ok(
		unparsed.length < configs.length / 2,
		`testMatch was unreadable in ${unparsed.length} of ${configs.length} configs: ${unparsed.join(', ')}`
	);
});
