import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

// track-table-observe-row (GUARD-13 / issue #154). Svelte reuses a keyed <tr>
// across pane switches; the one-shot IntersectionObserver action must rebind.
//
// Regression lines:
// - if a keyed DOM row is reused for another pane and still hydrates the previous
//   row, then broken
// - if update() only rewrites the WeakMap and does not re-observe then the
//   one-shot observer stays detached and the new row stays blank
// - if same-reference update re-observes then the same object hydrates on every
//   Svelte action re-eval
// - if off-screen reuse hydrates before intersection then pane switches pay
//   per-row work
// - if pane identity is interpolated into the each-key then every visible row
//   remounts on tab switch and focus drops

const TRACK_TABLE = fileURLToPath(
	new URL('../../src/lib/components/rb/browser/TrackTable.svelte', import.meta.url)
);

/** Host IntersectionObserver: Node has none. Stateful so fire() can prove
 * re-observe (throws when the node is not currently in the observed set). */
class HostIntersectionObserver {
	static instances = [];

	constructor(callback, options = {}) {
		this.callback = callback;
		this.options = options;
		this.observed = new Set();
		HostIntersectionObserver.instances.push(this);
	}

	observe(node) {
		this.observed.add(node);
	}

	unobserve(node) {
		this.observed.delete(node);
	}

	disconnect() {
		this.observed.clear();
	}

	fire(node, isIntersecting = true) {
		if (!this.observed.has(node)) {
			throw new Error('not observed');
		}
		this.callback([{ target: node, isIntersecting }]);
	}
}

function observerFor(node) {
	return HostIntersectionObserver.instances.find((inst) => inst.observed.has(node));
}

function fire(node, isIntersecting = true) {
	const observer = observerFor(node);
	if (!observer) throw new Error('not observed');
	observer.fire(node, isIntersecting);
}

function isObserved(node) {
	return observerFor(node) !== undefined;
}

let previousIntersectionObserver;
let mod;

before(async () => {
	previousIntersectionObserver = globalThis.IntersectionObserver;
	globalThis.IntersectionObserver = HostIntersectionObserver;
	mod = await loadTypeScriptModule('src/lib/components/rb/browser/observe-row.ts');
});

after(() => {
	if (previousIntersectionObserver === undefined) {
		delete globalThis.IntersectionObserver;
	} else {
		globalThis.IntersectionObserver = previousIntersectionObserver;
	}
});

function row(pane) {
	return { stable_id: 'track-1', order: 3, pane };
}

function make() {
	HostIntersectionObserver.instances.length = 0;
	const seen = [];
	const api = mod.createRowVisibilityObserver({
		onRowVisible: (visibleRow) => {
			seen.push(visibleRow);
		}
	});
	return { ...api, seen };
}

// ------------------------------------------------------------ pane reuse

test('reused keyed row hydrates the current pane BrowserRow, not the previous one', () => {
	const { observeRow, seen } = make();
	const node = {};
	const rowA = row('A');
	const rowB = row('B');

	const handle = observeRow(node, rowA);
	assert.equal(typeof handle.update, 'function');

	fire(node);
	assert.deepEqual(seen, [rowA]);
	assert.equal(isObserved(node), false, 'one-shot unobserves after the first hit');

	handle.update(rowB);
	assert.equal(isObserved(node), true, 'identity change must re-observe the reused node');

	fire(node);
	assert.deepEqual(seen, [rowA, rowB]);
});

test('same-reference update after the first fire does not re-observe', () => {
	const { observeRow, seen } = make();
	const node = {};
	const rowA = row('A');

	const handle = observeRow(node, rowA);
	fire(node);
	assert.deepEqual(seen, [rowA]);

	handle.update(rowA);
	assert.equal(isObserved(node), false);

	assert.throws(() => fire(node), /not observed/);
	assert.deepEqual(seen, [rowA]);
});

test('off-screen reuse waits for intersection and does not hydrate from update()', () => {
	const { observeRow, seen } = make();
	const node = {};
	const rowA = row('A');
	const rowB = row('B');

	const handle = observeRow(node, rowA);
	fire(node);
	handle.update(rowB);

	fire(node, false);
	assert.deepEqual(seen, [rowA]);
	assert.equal(isObserved(node), true, 'non-intersecting fire must leave the node observed');
});

test('destroy unobserves so a later fire throws rather than hydrating', () => {
	const { observeRow, seen } = make();
	const node = {};
	const rowA = row('A');

	const handle = observeRow(node, rowA);
	handle.destroy();

	assert.throws(() => fire(node), /not observed/);
	assert.deepEqual(seen, []);
});

// ------------------------------------------------------------ TrackTable wiring

test('TrackTable imports the factory, keeps use:observeRow={row}, and does not key by pane', () => {
	const source = readFileSync(TRACK_TABLE, 'utf8');
	assert.match(
		source,
		/import \{[^}]*createRowVisibilityObserver[^}]*\} from '\.\/virtual-window'/,
		'TrackTable must import createRowVisibilityObserver from ./virtual-window'
	);
	assert.match(source, /use:observeRow=\{row\}/, 'template must still apply use:observeRow={row}');

	const eachLine = source
		.split('\n')
		.find((line) => line.includes('{#each visibleRows as row'));
	assert.ok(eachLine, 'missing {#each visibleRows} in TrackTable');
	assert.match(
		eachLine,
		/`\$\{row\.stable_id\}:\$\{row\.order\}`/,
		'each-key must stay `${row.stable_id}:${row.order}`'
	);
	assert.equal(
		/restoreKey|activePane/.test(eachLine),
		false,
		'each-key must not interpolate restoreKey or activePane'
	);
});
