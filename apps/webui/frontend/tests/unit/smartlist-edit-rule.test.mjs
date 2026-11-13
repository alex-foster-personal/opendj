/**
 * Issue #10024: a smartlist's rule editor opens from its tree row in a modal,
 * and the row menu offers Refresh now (rekordbox Edit Intelligent Playlist, mimicked).
 *
 * WHAT A PASS HERE DOES NOT COVER: the registry and command catalogue run for
 * real; the modal and the tree row render through Svelte SSR only, so the
 * double-click, Escape and the save round trip are not dispatched (e2e not run).
 */
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { before, test } from 'node:test';

import { loadSvelteSsrModule } from './load-svelte-ssr.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

let registry;
let commands;
let ssr;

before(async () => {
	registry = await loadTypeScriptModule('src/lib/context-menu/registry.ts');
	commands = await loadTypeScriptModule('src/lib/context-menu/commands.ts');
	ssr = await loadSvelteSsrModule(
		[
			"export { default as SmartlistRuleModal } from '$lib/components/rb/SmartlistRuleModal.svelte';",
			"export { default as TreeSmartlistSectionView } from '$lib/components/rb/browser/TreeSmartlistSectionView.svelte';",
			"export { render } from 'svelte/server';"
		].join('\n')
	);
});

const noop = () => {};

// REQ: SMART-14445
test('the smartlist tree menu offers Edit rule... and Refresh now ahead of Rename', () => {
	const items = registry.menuFor({ kind: 'smartlist' }, { refreshing: false });
	const ids = items.map((item) => item.id);
	assert.deepEqual(ids.slice(ids.indexOf('edit-rule'), ids.indexOf('rename') + 1), [
		'edit-rule',
		'refresh',
		'rename'
	]);
	assert.equal(items.find((item) => item.id === 'edit-rule').label, 'Edit rule...');
	assert.equal(items.find((item) => item.id === 'refresh').label, 'Refresh now');
	assert.ok(
		items.every((item) => item.enabled),
		'if an item is disabled with no refresh running then broken'
	);
});

test('Refresh now is disabled with a reason while a refresh is in flight', () => {
	const refresh = registry
		.menuFor({ kind: 'smartlist' }, { refreshing: true })
		.find((item) => item.id === 'refresh');
	assert.equal(refresh.enabled, false);
	assert.match(refresh.disabledReason, /already running/);
});

test('both items have HTTP twins that exist in the API contract (agent parity)', () => {
	const openapi = JSON.parse(
		readFileSync(new URL('../../../openapi.json', import.meta.url), 'utf8')
	);
	const twins = {
		'edit-rule': { method: 'PUT', path: '/api/v1/smartlists/{smartlist_id}' },
		refresh: { method: 'GET', path: '/api/v1/smartlists/{smartlist_id}/tracks' }
	};
	for (const item of registry.menuFor({ kind: 'smartlist' }, { refreshing: false })) {
		const want = twins[item.id];
		if (want === undefined) continue;
		const command = commands.MENU_COMMANDS[item.command];
		assert.equal(command.via, 'http');
		assert.equal(command.method, want.method, `${item.id} method`);
		assert.equal(command.path, want.path, `${item.id} path`);
		assert.ok(
			openapi.paths[want.path]?.[want.method.toLowerCase()],
			`if ${want.method} ${want.path} is not a real route then broken`
		);
	}
});

test('the modal is a labelled dialog over the browser hosting the shared rule editor', () => {
	const html = ssr.render(ssr.SmartlistRuleModal, {
		props: { smartlist: { id: 'sl-1', name: 'Peak time' }, onclose: noop }
	}).body;
	assert.match(html, /role="dialog"[^>]*aria-modal="true"[^>]*aria-label="Edit rule: Peak time"/);
	assert.match(html, /data-testid="smartlist-rule-modal" data-smartlist-id="sl-1"/);
	assert.match(
		html,
		/Loading\.\.\./,
		'if the modal does not mount the editor (which loads the rule) then broken'
	);
});

test('a smartlist tree row still renders with its id once the double-click handler is wired', () => {
	const rows = [{ id: 'sl-1', name: 'Peak time', rule_summary: 'bpm >= 126', count: 4 }];
	const html = ssr.render(ssr.TreeSmartlistSectionView, {
		props: {
			open: true,
			rows,
			error: null,
			selectedId: null,
			selectable: true,
			editingId: null,
			editDraft: '',
			ontoggle: noop,
			onclickrow: noop,
			ondblclickrow: noop,
			onmenukey: noop,
			oncontextmenurow: noop,
			oneditdraft: noop,
			onrenamekeydown: noop,
			onrenamecommit: noop
		}
	}).body;
	assert.match(html, /data-testid="smartlist-row" data-smartlist-id="sl-1"/);
});
