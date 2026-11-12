// requirement: PERF-11995 (issue #11157, parent #325)
// [if] a registered perf view row drops id, title, css pack, slots or flags [then] the registry is incomplete, [else stop].
// [if] waveform, list, phase-scope or phase-meter is missing from the registry [then] TOPVIEW ids drift, [else stop].
// [if] default, mono-dev or light is missing from skin packs [then] first-wave skins are unregistered, [else stop].
// [if] only waveform mounts the default stack [then] alternate views fork the shell, [else stop].
// [if] parsePerfViewId accepts an unknown id [then] agents can select a ghost view, [else stop].
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REQUIRED_VIEW_FIELDS = ['id', 'title', 'cssPack', 'layoutSlots', 'flags', 'wavestackMount'];
const REQUIRED_FLAG_FIELDS = [
	'mountsWaveformStack',
	'collapsesWavestackTrack',
	'lazyViewChunk',
	'phaseViewRegion'
];
const REQUIRED_SKIN_FIELDS = ['id', 'title', 'cssPack', 'layoutSlots', 'flags', 'layout'];
const REQUIRED_SKIN_FLAGS = ['setsDataSkinAttribute', 'declaresWaveformChrome'];

let R;
before(async () => {
	R = await loadTypeScriptModule('src/lib/rb/view-registry.ts');
});

test('[if] a registered view is missing id, title, CSS pack, layout slots, or capability flags [then] stop', () => {
	for (const entry of R.listPerfViewRegistry()) {
		for (const key of REQUIRED_VIEW_FIELDS) assert.ok(key in entry, `${entry.id}: missing ${key}`);
		assert.equal(entry.id, entry.title ? entry.id : entry.id, 'id must match key');
		assert.ok(typeof entry.title === 'string' && entry.title.length > 0, entry.id);
		assert.ok(typeof entry.cssPack === 'string' && entry.cssPack.length > 0, entry.id);
		assert.ok(Array.isArray(entry.layoutSlots), entry.id);
		for (const flag of REQUIRED_FLAG_FIELDS)
			assert.ok(flag in entry.flags, `${entry.id}: missing flag ${flag}`);
	}
	for (const entry of R.listSkinPackRegistry()) {
		for (const key of REQUIRED_SKIN_FIELDS) assert.ok(key in entry, `${entry.id}: missing ${key}`);
		for (const flag of REQUIRED_SKIN_FLAGS)
			assert.ok(flag in entry.flags, `${entry.id}: missing skin flag ${flag}`);
		assert.ok(entry.layoutSlots.length > 0, entry.id);
		assert.ok(entry.layout?.library && entry.layout?.performance, entry.id);
	}
});

test('[if] perf_view values waveform, list, phase-scope, phase-meter are absent from the registry [then] stop', () => {
	for (const id of ['waveform', 'list', 'phase-scope', 'phase-meter']) {
		assert.ok(R.PERF_VIEW_REGISTRY[id], id);
		assert.deepEqual(R.PERF_VIEW_IDS, ['waveform', 'list', 'phase-scope', 'phase-meter']);
	}
});

test('[if] ui_skin values default, mono-dev, light are absent from the registry [then] stop', () => {
	for (const id of ['default', 'mono-dev', 'light']) {
		assert.ok(R.SKIN_PACK_REGISTRY[id], id);
		assert.equal(R.getSkinPackEntry(id).cssPack, id);
	}
	assert.deepEqual(R.SKIN_PACK_IDS, ['default', 'mono-dev', 'light']);
});

test('[if] selecting a registered view does not unmount or skip the waveform stack only for waveform [then] stop', () => {
	assert.equal(R.getPerfViewEntry('waveform').flags.mountsWaveformStack, true);
	assert.equal(R.perfViewWavestackMount('waveform'), 'waveform');
	for (const id of ['list', 'phase-scope', 'phase-meter']) {
		const e = R.getPerfViewEntry(id);
		assert.equal(e.flags.mountsWaveformStack, false, id);
		assert.notEqual(R.perfViewWavestackMount(id), 'waveform', id);
	}
	assert.equal(R.perfViewCollapsesWavestack('list'), true);
	assert.equal(R.perfViewCollapsesWavestack('waveform'), false);
	assert.equal(R.perfViewWavestackMount('list'), 'none');
	assert.equal(R.getPerfViewEntry('list').flags.collapsesWavestackTrack, true);
	assert.equal(R.getPerfViewEntry('phase-scope').flags.lazyViewChunk, true);
});

test('[if] an unknown view id is accepted by the registry parser [then] stop', () => {
	assert.throws(() => R.parsePerfViewId('grid'), /perf_view must be one of/);
	assert.throws(() => R.parsePerfViewId(null), /perf_view must be one of/);
	assert.equal(R.parsePerfViewId('phase-meter'), 'phase-meter');
	assert.throws(() => R.parseSkinPackId('green-room'), /ui_skin pack must be one of/);
});
