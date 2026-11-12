import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { existsSync, readdirSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';
import test from 'node:test';

const frontendRoot = join(dirname(fileURLToPath(import.meta.url)), '../..');
const read = (rel) => readFileSync(join(frontendRoot, rel), 'utf8');

// PR #12832 (issue #11467) moved the settings catalog and the non-default admin panels behind
// import(). What that buys is structural: those modules sit outside the static closure of the
// eagerly loaded shell and of the /settings and /admin route nodes. A byte pin on other-lazy
// cannot prove that and breaks on any unrelated growth, so the size half reads the surface's
// recorded budget (budgets/other-lazy/base.json plus its charges, the same limit the bundle gate
// enforces) and the structural half reads Vite's module graph.

const VITE_MANIFEST = '.svelte-kit/output/client/.vite/manifest.json';
const NODES_DIR = '.svelte-kit/generated/client-optimized/nodes';
const ADMIN_LAZY_PANELS = [
	'DiagnosticsPanel',
	'PlaygroundPanel',
	'SpotifyPanes',
	'TitleFixPanel'
].map((name) => `src/routes/admin/${name}.svelte`);

function _routeNodeKey(component) {
	const keys = readdirSync(join(frontendRoot, NODES_DIR))
		.filter((file) =>
			readFileSync(join(frontendRoot, NODES_DIR, file), 'utf8').includes(`/${component}"`)
		)
		.map((file) => `${NODES_DIR}/${file}`);
	assert.equal(
		keys.length,
		1,
		`exactly one route node renders ${component}, found ${keys.join(', ') || 'none'}`
	);
	return keys[0];
}

function _staticClosure(manifest, roots) {
	const seen = new Set();
	const queue = [...roots];
	while (queue.length) {
		const key = queue.pop();
		if (seen.has(key)) continue;
		assert.ok(manifest[key], `${key} is not in the Vite manifest`);
		seen.add(key);
		queue.push(...(manifest[key].imports ?? []));
	}
	return seen;
}

function _chunkKeyNamed(manifest, name) {
	const keys = Object.keys(manifest).filter((key) => manifest[key].name === name);
	assert.equal(
		keys.length,
		1,
		`exactly one chunk is named "${name}", found ${keys.join(', ') || 'none'}`
	);
	return keys[0];
}

test('[if] production build and bundle-budget JSON [then] other-lazy gzip is within its recorded budget (base + charges), [else stop].', (t) => {
	if (!existsSync(join(frontendRoot, 'build/_app/immutable'))) {
		// The unit job on CI runs before the production build is restored, so there is nothing to measure there.
		t.skip('needs a production build on disk (pnpm build); this job has none');
		return;
	}
	const measurePath = join(frontendRoot, 'build', '.tmp-bundle-measure.json');
	execFileSync('node', ['scripts/bundle-budget.mjs', '--measure', measurePath], {
		cwd: frontendRoot,
		stdio: 'pipe'
	});
	const report = JSON.parse(readFileSync(measurePath, 'utf8'));
	const bytes = report.totals['other-lazy']?.bytes;
	const budget = report.budgets.find((b) => b.name === 'other-lazy');
	assert.equal(typeof bytes, 'number');
	assert.ok(budget, 'the measurement carries the other-lazy budget');
	assert.ok(
		bytes <= budget.limit,
		`other-lazy ${bytes} > limit ${budget.limit} = base ${budget.base.limit} + charges ${budget.charged}`
	);
	assert.equal(report.unattributed?.length ?? 0, 0, 'no UNATTRIBUTED chunks');
});

test('[if] production build module graph [then] settings catalog/apply and admin panels are outside the eager shell and the /settings and /admin static closures, [else stop].', (t) => {
	if (!existsSync(join(frontendRoot, VITE_MANIFEST))) {
		// The unit job on CI runs before the production build is restored, so there is nothing to measure there.
		t.skip('needs a production build on disk (pnpm build); this job has none');
		return;
	}
	const manifest = JSON.parse(read(VITE_MANIFEST));
	const catalog = _chunkKeyNamed(manifest, 'catalog');
	const apply = 'src/lib/settings/apply.ts';
	assert.ok(manifest[apply]?.isDynamicEntry, `${apply} is its own lazily loaded chunk`);
	const settingsNode = _routeNodeKey('src/routes/settings/+page.svelte');
	const adminNode = _routeNodeKey('src/routes/admin/+page.svelte');
	const eagerRoots = [
		...Object.keys(manifest).filter(
			(key) => manifest[key].isEntry && !key.startsWith(`${NODES_DIR}/`)
		),
		_routeNodeKey('src/routes/+layout.svelte'),
		_routeNodeKey('src/routes/+page.svelte')
	];
	const closures = {
		eager: _staticClosure(manifest, eagerRoots),
		'/settings': _staticClosure(manifest, [settingsNode]),
		'/admin': _staticClosure(manifest, [adminNode])
	};
	for (const [where, closure] of Object.entries(closures)) {
		for (const deferred of [catalog, apply, ...ADMIN_LAZY_PANELS]) {
			assert.ok(!closure.has(deferred), `${deferred} is statically reachable from ${where}`);
		}
	}
	assert.ok(
		manifest[settingsNode].dynamicImports?.includes(catalog),
		'/settings loads the catalog with import()'
	);
	for (const panel of ADMIN_LAZY_PANELS) {
		assert.ok(
			manifest[adminNode].dynamicImports?.includes(panel),
			`/admin loads ${panel} with import()`
		);
	}
});

test('[if] settings/admin modules load after those routes open [then] they are not static-imported there, [else stop].', () => {
	const admin = read('src/routes/admin/+page.svelte');
	const settings = read('src/routes/settings/+page.svelte');
	const layout = read('src/routes/+layout.svelte');
	assert.doesNotMatch(admin, /from '\.\/DiagnosticsPanel\.svelte'/);
	assert.doesNotMatch(settings, /from '\$lib\/settings\/catalog'/);
	assert.doesNotMatch(layout, /from '\$lib\/settings\/(catalog|apply)'/);
	assert.match(admin, /import\('\.\/DiagnosticsPanel\.svelte'\)/);
	assert.match(settings, /import\('\$lib\/settings\/catalog'\)/);
});

test('[if] Cmd+, wiring and the settings overlay loader [then] still lazy-load SettingsOverlay, [else stop].', () => {
	const loader = read('src/lib/components/settings/settings-overlay-loader.ts');
	const layout = read('src/routes/+layout.svelte');
	assert.match(loader, /import\('\.\/SettingsOverlay\.svelte'\)/);
	assert.match(layout, /loadSettingsOverlay/);
	assert.match(layout, /prefetchSettingsOverlay/);
});

test('[if] /settings route source [then] it still calls catalogLinkSettings and openSettings for real, [else stop].', () => {
	const settings = read('src/routes/settings/+page.svelte');
	assert.match(settings, /catalogLinkSettings\('cloudsync'\)/);
	assert.match(settings, /openSettings\(\)/);
	assert.doesNotMatch(settings, /mock.*catalogLinkSettings/i);
});
