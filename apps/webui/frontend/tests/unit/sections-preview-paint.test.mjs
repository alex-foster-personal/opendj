// requirement: STRUCTUI-12787
// [if] segment display preview rows paint invisible canvases [then] this fails, [else stop].
import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const W = 200;
const H = 72;
const MIN_INK = 0.08;

test('STRUCTUI-12787 previews paint visible marks for every enum option', async () => {
	const catalog = await loadTypeScriptModule('src/lib/settings/catalog.ts');
	const paintMod = await loadTypeScriptModule('src/lib/settings/sections-preview-paint.ts');
	const prefsMod = await loadTypeScriptModule('src/lib/rb/section-overlay-prefs.ts');
	const defaults = { ...prefsMod.SECTION_OVERLAY_PREF_DEFAULTS, show_sections: true };
	const rows = catalog.SETTINGS_CATALOG.filter((d) => d.preview === 'sections');
	assert.ok(rows.length >= 8, `expected segment display rows, saw ${rows.length}`);
	let painted = 0;
	for (const def of rows) {
		const base = { ...defaults };
		if (def.control.kind === 'enum') {
			for (const { value } of def.control.options) {
				base[def.id] = value;
				const ink = _paintInk(paintMod, base);
				assert.ok(ink >= MIN_INK * W * H, `${def.id}=${value} painted too little (${ink})`);
				painted += 1;
			}
		} else if (def.control.kind === 'boolean') {
			for (const value of [false, true]) {
				base[def.id] = value;
				const ink = _paintInk(paintMod, base);
				assert.ok(ink >= MIN_INK * W * H, `${def.id}=${value} painted too little (${ink})`);
				painted += 1;
			}
		}
	}
	assert.ok(painted > 0, 'no previews painted');
});

test('STRUCTUI-12787 show_segments off preview differs from on', async () => {
	const paintMod = await loadTypeScriptModule('src/lib/settings/sections-preview-paint.ts');
	const prefsMod = await loadTypeScriptModule('src/lib/rb/section-overlay-prefs.ts');
	const on = { ...prefsMod.SECTION_OVERLAY_PREF_DEFAULTS, show_sections: true };
	const off = { ...prefsMod.SECTION_OVERLAY_PREF_DEFAULTS, show_sections: false };
	assert.notEqual(_paintInk(paintMod, on), _paintInk(paintMod, off));
});

function _paintInk(paintMod, prefs) {
	let area = 0;
	const ctx = {
		fillStyle: '',
		strokeStyle: '',
		lineWidth: 1,
		globalAlpha: 1,
		clearRect() {},
		fillRect(_x, _y, w, h) {
			if (ctx.globalAlpha > 0.2) area += Math.max(0, w) * Math.max(0, h);
		},
		stroke() {},
		strokeRect(_x, _y, w, h) {
			if (ctx.globalAlpha > 0.2) area += Math.max(0, w) * Math.max(0, h) * 0.5;
		},
		beginPath() {},
		moveTo() {},
		lineTo() {},
		fillText() {
			area += 40;
		},
		font: '',
		setLineDash() {}
	};
	paintMod.paintSectionsPreview(ctx, W, H, prefs);
	return area;
}
