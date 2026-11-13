// [if] color or beat FX commands map [then] rows use fx.N ids and agent source when agent-command, [else stop].
import assert from 'node:assert/strict';
import test from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const fx = await loadTypeScriptModule('src/lib/sets/fx-control-events.ts');
const ids = await loadTypeScriptModule('src/lib/sets/control_ids.ts');
const colorFx = await loadTypeScriptModule('src/lib/rb/color-fx.ts');

const stamp = { sample: 96_000, sample_rate: 48_000 };

test('color_fx effect select and wet map to fx.N ids', () => {
	const command = { type: 'color_fx', deck: 1, effect: 'space', value: 0.4 };
	const plan = colorFx.planColorFxSwitch('filter', 0.5, command);
	const rows = fx.eventsFromColorFxCommand(
		command,
		plan,
		'user-ui',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(rows.length, 2);
	assert.equal(rows[0].control, ids.FX_COLOR_SELECT[1]);
	assert.equal(rows[1].control, ids.FX_COLOR_WET[1]);
});

test('agent-order FX changes map to source agent', () => {
	const command = { type: 'color_fx', deck: 2, value: 0.2 };
	const plan = colorFx.planColorFxSwitch('filter', 0.5, command);
	const rows = fx.eventsFromColorFxCommand(
		command,
		plan,
		'agent-command',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(fx.mapControlEventSource('agent-command'), 'agent');
	assert.ok(rows.every((r) => r.source === 'agent'));
});

test('beat_fx on select and wet map to fx.0 ids', () => {
	const rows = fx.eventsFromBeatFxCommand(
		{ type: 'beat_fx', enabled: true, effect: 'echo', level: 0.6 },
		'user-ui',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.deepEqual(
		rows.map((r) => r.control),
		[ids.BEAT_FX_ON, ids.BEAT_FX_SELECT, ids.BEAT_FX_WET]
	);
});

test('beat_fx release is a trigger on fx.0.release', () => {
	const rows = fx.eventsFromBeatFxReleaseCommand(
		{ type: 'beat_fx_release', held: true },
		'user-ui',
		stamp,
		'sess',
		'2026-11-10T12:00:00Z'
	);
	assert.equal(rows[0].control, ids.BEAT_FX_RELEASE);
	assert.equal(rows[0].kind, 'trigger');
});

test('continuous wet coalesces to first and last inside a quantum', () => {
	const coalescer = new fx.FxControlEventCoalescer(10);
	const base = {
		v: 2,
		session_id: 's',
		sample: 1,
		sample_rate: 48_000,
		wall_clock: 't',
		source: 'user',
		kind: 'set'
	};
	coalescer.push({ ...base, seq: 0, control: ids.FX_COLOR_WET[1], value: 0.1 }, 0);
	coalescer.push({ ...base, seq: 1, control: ids.FX_COLOR_WET[1], value: 0.9 }, 5);
	const out = coalescer.flush(10, true);
	assert.equal(out.length, 2);
	assert.equal(out[0].value, 0.1);
	assert.equal(out[1].value, 0.9);
});
