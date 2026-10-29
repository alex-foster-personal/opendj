import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const m = await loadTypeScriptModule('src/lib/settings/panel-offset.ts');
const OVERLAY = readFileSync(
	fileURLToPath(new URL('../../src/lib/components/settings/SettingsOverlay.svelte', import.meta.url)),
	'utf8'
);
const BASE = { left: 300, top: 100, width: 400 };
const VIEW = { width: 1000, height: 800 };

// requirement: MIXGRP-09
test('a move inside the window lands where it was asked', () => {
	assert.deepEqual(m.clampPanelOffset({ dx: 50, dy: -40 }, BASE, VIEW), { dx: 50, dy: -40 });
	assert.deepEqual(m.clampPanelOffset({ dx: 0, dy: 0 }, BASE, VIEW), { dx: 0, dy: 0 });
});

// requirement: MIXGRP-09
test('a move past any edge stops with 64 px still on screen, never above the top', () => {
	const keep = m.PANEL_KEEP_PX;
	assert.equal(keep, 64);
	assert.deepEqual(m.clampPanelOffset({ dx: 1e9, dy: 1e9 }, BASE, VIEW), {
		dx: VIEW.width - keep - BASE.left,
		dy: VIEW.height - keep - BASE.top
	});
	assert.deepEqual(m.clampPanelOffset({ dx: -1e9, dy: -1e9 }, BASE, VIEW), {
		dx: keep - BASE.width - BASE.left,
		dy: -BASE.top
	});
	assert.throws(() => m.clampPanelOffset({ dx: Number.NaN, dy: 0 }, BASE, VIEW), RangeError);
});

// requirement: MIXGRP-09
test('the header drag and the agent bridge share one placement rule (Sol P1, PR #6829)', () => {
	assert.match(OVERLAY, /function placePanel\(want: PanelOffset\): PanelOffset \{[\s\S]*?clampPanelOffset\(/);
	assert.match(OVERLAY, /function onHeadPointerMove[\s\S]*?placePanel\(\{ dx: panelDrag\.dx \+/);
	assert.match(OVERLAY, /attachSettingsPanel\(\{ offset: \(\) => \(\{ dx: panelDx, dy: panelDy \}\), place: placePanel \}\)/);
	assert.match(OVERLAY, /if \(panelEl === null\) throw new Error\('settings panel is not open'\)/);
	// The bridge is not owned by the lazily mounted component any more.
	assert.doesNotMatch(OVERLAY, /__mdtSettingsPanel =/);
	assert.doesNotMatch(OVERLAY, /const keep = 64/);
	const store = readFileSync(
		fileURLToPath(new URL('../../src/lib/settings/overlay.svelte.ts', import.meta.url)),
		'utf8'
	);
	assert.match(store, /import '\.\/panel-offset';/, 'the page-lifetime store no longer installs the bridge');
});

// requirement: MIXGRP-09
test('the bridge outlives the panel: closed calls throw the closed-panel error, open calls place it', () => {
	// Sol P1, PR #6829: unmounting the panel deleted the bridge, so a closed
	// call hit an undefined property instead of the documented error.
	const b = m.settingsPanelBridge;
	assert.throws(() => b.moveBy(10, 10), /settings panel is not open/);
	assert.throws(() => b.reset(), /settings panel is not open/);
	assert.deepEqual(b.offset(), { dx: 0, dy: 0 });
	let at = { dx: 0, dy: 0 };
	const detach = m.attachSettingsPanel({
		offset: () => at,
		place: (want) => (at = m.clampPanelOffset(want, BASE, VIEW))
	});
	assert.deepEqual(b.moveBy(20, -30), { dx: 20, dy: -30 });
	assert.deepEqual(b.moveBy(1e9, 0).dx, VIEW.width - m.PANEL_KEEP_PX - BASE.left, 'the bridge escaped the drag bounds');
	assert.deepEqual(b.reset(), { dx: 0, dy: 0 });
	detach();
	assert.throws(() => b.moveBy(1, 1), /settings panel is not open/, 'a detached panel still took moves');
	// A stale detach must not unhook a newer panel.
	const detach2 = m.attachSettingsPanel({ offset: () => at, place: (want) => (at = want) });
	detach();
	assert.deepEqual(b.moveBy(5, 5), { dx: 5, dy: 5 });
	detach2();
});
