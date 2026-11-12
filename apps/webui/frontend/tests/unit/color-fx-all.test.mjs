// requirement: COLORFX-13
// [if] deck all targets space while channels differ [then] each follows COLORFX-8341 switch rule
// [if] color_fx deck all with value or effect next is sent [then] the parser refuses with the reason
// [if] one dispatch of deck all effect space [then] all four channels read space and dispatch returns every view
// [if] color_fx deck all is queued [then] it claims scopes 1 through 4
// [if] Rust engine mode is on [then] color_fx deck all is refused by name
import assert from 'node:assert/strict';
import { after, before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let pure;
let all;
let live;
let ipc;
let rust;

before(async () => {
	({ pure, all, live, ipc, rust } = await loadTypeScriptModule(
		'tests/unit/fixtures/color-fx-entry.ts',
		{
			viteApiBase: 'http://127.0.0.1:9'
		}
	));
});

after(() => {
	live._resetColorFxForTests();
});

test('COLORFX-13: planColorFxAll applies the switch rule per channel', () => {
	const now = {
		1: { effect: 'filter', knob: 0.3 },
		2: { effect: 'space', knob: 0.8 },
		3: { effect: 'echo', knob: 0.5 },
		4: { effect: 'space', knob: 0.2 }
	};
	const cmd = { type: 'color_fx', deck: 'all', effect: 'space' };
	const plans = all.planColorFxAll(now, cmd);
	assert.deepEqual(plans[1], { effect: 'space', value: 0.5, switched: true });
	assert.deepEqual(plans[2], { effect: 'space', value: 0.8, switched: false });
	assert.deepEqual(plans[3], { effect: 'space', value: 0.5, switched: true });
	assert.deepEqual(plans[4], { effect: 'space', value: 0.2, switched: false });
});

test('COLORFX-13: colorFxAllCommands fans out in deck order with echo_beats', () => {
	const cmds = all.colorFxAllCommands({
		type: 'color_fx',
		deck: 'all',
		effect: 'echo',
		echo_beats: 0.5
	});
	assert.deepEqual(
		[1, 2, 3, 4].map((d) => cmds[d]),
		[
			{ type: 'color_fx', deck: 1, effect: 'echo', echo_beats: 0.5 },
			{ type: 'color_fx', deck: 2, effect: 'echo', echo_beats: 0.5 },
			{ type: 'color_fx', deck: 3, effect: 'echo', echo_beats: 0.5 },
			{ type: 'color_fx', deck: 4, effect: 'echo', echo_beats: 0.5 }
		]
	);
});

test('COLORFX-13: parser refusals for deck all', () => {
	assert.deepEqual(
		pure.parseColorFxCommand({ type: 'color_fx', deck: 'all', effect: 'echo', echo_beats: 0.5 }),
		{
			type: 'color_fx',
			deck: 'all',
			effect: 'echo',
			echo_beats: 0.5
		}
	);
	for (const [bad, pattern] of [
		[{ type: 'color_fx', deck: 'all', effect: 'space', value: 0.7 }, /own knob/],
		[{ type: 'color_fx', deck: 'all', effect: 'next' }, /ambiguous/],
		[{ type: 'color_fx', deck: 'all', echo_beats: 0.5 }, /needs an effect/]
	]) {
		assert.throws(
			() => pure.parseColorFxCommand(bad),
			(e) => e instanceof TypeError && pattern.test(e.message)
		);
	}
});

test('COLORFX-13: dispatcher applies one effect to every channel', async () => {
	live._resetColorFxForTests();
	const hadWindow = 'window' in globalThis;
	if (!hadWindow) globalThis.window = {};
	const hadLocation = 'location' in globalThis;
	if (!hadLocation) globalThis.location = new URL('http://127.0.0.1/performance');
	const uninstall = ipc.installPerformanceBrowserIpc();
	const dispatch = (command) => globalThis.window.musicDjToolsPerformance.dispatch(command);
	try {
		await dispatch({ type: 'color_fx', deck: 1, effect: 'filter', value: 0.3 });
		await dispatch({ type: 'color_fx', deck: 2, effect: 'space', value: 0.8 });
		await dispatch({ type: 'color_fx', deck: 3, effect: 'echo', value: 0.5 });
		await dispatch({ type: 'color_fx', deck: 4, effect: 'filter', value: 0.2 });
		await dispatch({ type: 'color_fx', deck: 'all', effect: 'space' });
		const after = ipc.queryPerformanceState();
		for (const d of [1, 2, 3, 4]) {
			assert.equal(after.color_fx[d].effect, 'space', `deck ${d}`);
		}
		assert.equal(after.mixer.channels[2].filter, 0.8, 'deck 2 already on space keeps knob');
		assert.equal(after.mixer.channels[1].filter, 0.5, 'deck 1 switches to centre');
	} finally {
		uninstall();
		if (!hadWindow) delete globalThis.window;
		if (!hadLocation) delete globalThis.location;
		live._resetColorFxForTests();
	}
});

test('COLORFX-13: dispatcher refuses deck all value and next', async () => {
	live._resetColorFxForTests();
	const hadWindow = 'window' in globalThis;
	if (!hadWindow) globalThis.window = {};
	const hadLocation = 'location' in globalThis;
	if (!hadLocation) globalThis.location = new URL('http://127.0.0.1/performance');
	const uninstall = ipc.installPerformanceBrowserIpc();
	const dispatch = (command) => globalThis.window.musicDjToolsPerformance.dispatch(command);
	try {
		live.applyColorFxSettings({ type: 'color_fx', deck: 1, effect: 'filter' });
		live.applyColorFxSettings({ type: 'color_fx', deck: 2, effect: 'echo' });
		for (const cmd of [
			{ type: 'color_fx', deck: 'all', effect: 'next' },
			{ type: 'color_fx', deck: 'all', effect: 'space', value: 0.2 }
		]) {
			await assert.rejects(dispatch(cmd), /deck all/);
		}
		assert.equal(live.colorFx[1].effect, 'filter');
		assert.equal(live.colorFx[2].effect, 'echo');
	} finally {
		uninstall();
		if (!hadWindow) delete globalThis.window;
		if (!hadLocation) delete globalThis.location;
		live._resetColorFxForTests();
	}
});

test('COLORFX-13: deck all claims all four queue scopes', () => {
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'color_fx', deck: 'all', effect: 'space' }),
		[1, 2, 3, 4]
	);
	assert.deepEqual(
		ipc.performanceCommandQueueScopes({ type: 'color_fx', deck: 2, effect: 'space' }),
		[2]
	);
});

test('COLORFX-13: Rust engine mode refuses color_fx deck all by name', async () => {
	rust.rustMode.enabled = true;
	try {
		await assert.rejects(
			rust.executeInRustEngine({ type: 'color_fx', deck: 'all', effect: 'space' }),
			/RUST_ENGINE_UNAVAILABLE/
		);
	} finally {
		rust.rustMode.enabled = false;
	}
});
