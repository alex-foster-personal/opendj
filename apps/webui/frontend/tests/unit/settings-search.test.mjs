import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('filterSettings matches keywords and synonyms', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	const dark = mod.filterSettings('dark mode', { hideTodo: true, group: null });
	assert.ok(dark.keyword.some((s) => s.id === 'theme'));

	const broken = mod.filterSettings('missing files', { hideTodo: true, group: null });
	assert.ok(broken.keyword.some((s) => s.id === 'hide_broken_links'));

	const sync = mod.filterSettings('writeback rekordbox', { hideTodo: false, group: null });
	assert.ok(sync.keyword.some((s) => s.id === 'auto_sync'));
});

test('filterSettings hideTodo drops unimplemented rows', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	const all = mod.filterSettings('', { hideTodo: false, group: null });
	const slim = mod.filterSettings('', { hideTodo: true, group: null });
	assert.ok(all.all.length > slim.all.length);
	assert.ok(slim.all.every((s) => s.implemented));
});

test('filterSettings appends AI ids without duplicates', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	const r = mod.filterSettings('theme', {
		hideTodo: true,
		group: null,
		aiIds: ['theme', 'beat_sync_max', 'nope']
	});
	assert.ok(r.keyword.some((s) => s.id === 'theme'));
	assert.ok(r.aiExtra.some((s) => s.id === 'beat_sync_max'));
	assert.equal(
		r.all.filter((s) => s.id === 'theme').length,
		1,
		'theme must not duplicate'
	);
});

test('filterSettings finds DECKUX-02 jog radial waveform setting', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	const hits = mod.filterSettings('radial jog waveform', { hideTodo: true, group: null });
	const def = hits.keyword.find((s) => s.id === 'jog_radial_waveform');
	assert.ok(def, 'jog_radial_waveform must be searchable');
	assert.equal(def.implemented, true);
	assert.equal(def.control.kind, 'boolean');
});

test('filterSettings finds pin 862cd3 deck layout settings, implemented via existing controls', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');

	const layout = mod.filterSettings('more less deck layout', { hideTodo: true, group: null });
	const layoutDef = layout.keyword.find((s) => s.id === 'deck_layout');
	assert.ok(layoutDef, 'deck_layout must be searchable by "more less deck layout"');
	assert.equal(layoutDef.implemented, true);
	assert.equal(layoutDef.control.kind, 'enum');

	const animate = layout.keyword.find((s) => s.id === 'deck_layout_animate');
	assert.ok(animate, 'deck_layout_animate must be searchable alongside deck_layout');
	assert.equal(animate.implemented, true);
	assert.equal(animate.control.kind, 'boolean');

	const duration = mod.filterSettings('duration transition', { hideTodo: true, group: null });
	const durationDef = duration.keyword.find((s) => s.id === 'deck_layout_duration_ms');
	assert.ok(durationDef, 'deck_layout_duration_ms must be searchable by "duration transition"');
	assert.equal(durationDef.implemented, true);
	assert.equal(durationDef.control.kind, 'enum');
});

test('perf_tier is searchable under tier and performance', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/search.ts');
	const tier = mod.filterSettings('tier', { hideTodo: true, group: null });
	assert.ok(tier.keyword.some((s) => s.id === 'perf_tier'));
	const perf = mod.filterSettings('performance', { hideTodo: true, group: 'performance' });
	assert.ok(perf.keyword.some((s) => s.id === 'perf_tier'));
});

test('keyboard helpers move and toggle booleans', async () => {
	const mod = await loadTypeScriptModule('src/lib/settings/keyboard.ts');
	assert.equal(mod.moveSelection(0, 1, 3), 1);
	assert.equal(mod.moveSelection(2, 1, 3), 2);
	assert.equal(mod.moveSelection(2, 1, 3, true), 0);
	assert.equal(mod.booleanKeyAction(' '), 'toggle');
	assert.equal(mod.booleanKeyAction('ArrowRight'), 'on');
	assert.equal(mod.booleanKeyAction('ArrowLeft'), 'off');
	assert.equal(mod.applyBooleanAction(false, 'toggle'), true);
	assert.equal(mod.applyBooleanAction(true, 'off'), false);
});
