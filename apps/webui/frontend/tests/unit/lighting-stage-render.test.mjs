// requirement: LIGHT-10573
// [if] a frame sets fixture A to red at intensity 0.5 [then] stage-render emits a disc at A's position with colour rgb(128, 0, 0), [else stop].
// [if] a mover has pan 0 and 270 degrees [then] the beam lines point in opposite directions, [else stop].
// [if] a frames JSON file is loaded in replay mode [then] the page draws without any websocket, [else stop].
// [if] stage-render.ts imports anything (svelte, $app, $lib, the DOM) or names a DOM global [then] this test fails.
// [if] a replay control works only through page handlers (no typed command, IPC or CLI twin) [then] this test fails.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { before, test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadTypeScriptModule } from './load-typescript.mjs';

const REPO = fileURLToPath(new URL('../../../../../', import.meta.url));
const VECTORS_PATH = resolve(REPO, 'tests/lighting/fixtures/stage-replay-vectors.json');

let stage;
let frames;
let stageSession;
let stageIpc;

before(async () => {
	stage = await loadTypeScriptModule('src/lib/lighting/stage-render.ts');
	frames = await loadTypeScriptModule('src/lib/lighting/frames-client.ts');
	stageSession = await loadTypeScriptModule('src/lib/lighting/stage-session.ts');
	stageIpc = await loadTypeScriptModule('src/lib/lighting/stage-ipc.ts');
});

test('red at 0.5 yields disc rgb(128, 0, 0) at patch position', () => {
	const patch = {
		version: 1,
		fixtures: [{ id: 'A', x: 0.25, y: 0.5, groups: [] }]
	};
	const frame = {
		t_ns: 0,
		patch_version: 1,
		fixtures: [
			{
				id: 'A',
				rgb: [255, 0, 0],
				intensity: 0.5,
				pan_deg: null,
				tilt_deg: null,
				strobe_hz: 0
			}
		]
	};
	const size = { width: 400, height: 200, margin: 0 };
	const commands = stage.renderStage(frame, patch, size);
	const discs = commands.filter((c) => c.kind === 'disc');
	assert.equal(discs.length, 1);
	const d = discs[0];
	assert.equal(d.id, 'A');
	assert.equal(d.x, 100);
	assert.equal(d.y, 100);
	assert.equal(d.color, 'rgb(128, 0, 0)');
	assert.equal(
		commands.some((c) => c.kind === 'beam'),
		false
	);
	assert.equal(
		commands.some((c) => c.kind === 'flash'),
		false
	);
});

test('pan 0 and 270 produce opposite beam directions', () => {
	const patch = {
		version: 1,
		fixtures: [
			{ id: 'M0', x: 0.2, y: 0.5, groups: ['movers'] },
			{ id: 'M270', x: 0.8, y: 0.5, groups: ['movers'] }
		]
	};
	const mkFrame = (id, pan) => ({
		t_ns: 0,
		patch_version: 1,
		fixtures: [
			{
				id,
				rgb: [0, 255, 0],
				intensity: 1,
				pan_deg: pan,
				tilt_deg: 135,
				strobe_hz: 0
			}
		]
	});
	const size = { width: 400, height: 200, margin: 0 };
	const beamDot = (commands, id) => {
		const b = commands.find((c) => c.kind === 'beam' && c.id === id);
		assert.ok(b);
		const vx = b.x2 - b.x1;
		const vy = b.y2 - b.y1;
		const len = Math.hypot(vx, vy);
		return { x: vx / len, y: vy / len };
	};
	const c0 = stage.renderStage(mkFrame('M0', 0), patch, size);
	const c270 = stage.renderStage(mkFrame('M270', 270), patch, size);
	const v0 = beamDot(c0, 'M0');
	const v270 = beamDot(c270, 'M270');
	const dot = v0.x * v270.x + v0.y * v270.y;
	assert.ok(dot < -0.999, `dot product ${dot} should be < -0.999`);
	const b0 = stage.beamVector(0, 135);
	const b270 = stage.beamVector(270, 135);
	const dotDirect = b0.dx * b270.dx + b0.dy * b270.dy;
	assert.ok(dotDirect < -0.999);
	const staticOnly = stage.renderStage(
		{
			t_ns: 0,
			patch_version: 1,
			fixtures: [
				{
					id: 'P',
					rgb: [255, 255, 255],
					intensity: 1,
					pan_deg: null,
					tilt_deg: null,
					strobe_hz: 0
				}
			]
		},
		{ version: 1, fixtures: [{ id: 'P', x: 0.5, y: 0.5, groups: [] }] },
		size
	);
	assert.equal(
		staticOnly.some((c) => c.kind === 'beam'),
		false
	);
});

test('strobe flash timing', () => {
	const patch = {
		version: 1,
		fixtures: [{ id: 'S', x: 0.5, y: 0.5, groups: ['strobes'] }]
	};
	const frame = {
		t_ns: 0,
		patch_version: 1,
		fixtures: [
			{
				id: 'S',
				rgb: [255, 255, 255],
				intensity: 1,
				pan_deg: null,
				tilt_deg: null,
				strobe_hz: 2
			}
		]
	};
	const size = { width: 400, height: 200 };
	const at0 = stage.renderStage(frame, patch, size, { timeS: 0 });
	const flashes0 = at0.filter((c) => c.kind === 'flash');
	assert.equal(flashes0.length, 1);
	assert.equal(flashes0[0].color, 'rgb(255, 255, 255)');
	const at025 = stage.renderStage(frame, patch, size, { timeS: 0.25 });
	assert.equal(
		at025.some((c) => c.kind === 'flash'),
		false
	);
	const noStrobe = { ...frame, fixtures: [{ ...frame.fixtures[0], strobe_hz: 0 }] };
	assert.equal(
		stage.renderStage(noStrobe, patch, size, { timeS: 0 }).some((c) => c.kind === 'flash'),
		false
	);
	const off = {
		...frame,
		fixtures: [{ ...frame.fixtures[0], intensity: 0, strobe_hz: 10 }]
	};
	assert.equal(
		stage.renderStage(off, patch, size, { timeS: 0 }).some((c) => c.kind === 'flash'),
		false
	);
});

test('replay openReplay without websocket', () => {
	const saved = globalThis.WebSocket;
	class ThrowSocket {
		constructor() {
			throw new Error('replay must not open a socket');
		}
	}
	try {
		globalThis.WebSocket = ThrowSocket;
		const replayText = JSON.stringify({
			patch: {
				version: 1,
				fixtures: [
					{ id: 'A', x: 0.3, y: 0.5, groups: [] },
					{ id: 'B', x: 0.7, y: 0.5, groups: [] }
				]
			},
			frames: [
				{
					t_ns: 0,
					patch_version: 1,
					fixtures: [
						{
							id: 'A',
							rgb: [255, 0, 0],
							intensity: 1,
							pan_deg: null,
							tilt_deg: null,
							strobe_hz: 0
						}
					]
				},
				{
					t_ns: 5e8,
					patch_version: 1,
					fixtures: [
						{
							id: 'A',
							rgb: [0, 0, 255],
							intensity: 1,
							pan_deg: null,
							tilt_deg: null,
							strobe_hz: 0
						}
					]
				}
			]
		});
		const src = frames.openReplay(replayText);
		assert.equal(src.frameAt(0.1).index, 0);
		assert.equal(src.frameAt(0.6).index, 1);
		const size = { width: 400, height: 200 };
		const sig0 = stage.stageSignature(stage.renderStage(src.frameAt(0.1).frame, src.patch, size));
		const sig1 = stage.stageSignature(stage.renderStage(src.frameAt(0.6).frame, src.patch, size));
		assert.ok(sig0.length > 0);
		assert.ok(sig1.length > 0);
		assert.notEqual(sig0, sig1);

		const bareArray = JSON.stringify([
			{
				t_ns: 0,
				patch_version: null,
				fixtures: [
					{
						id: 'X',
						rgb: [10, 20, 30],
						intensity: 1,
						pan_deg: null,
						tilt_deg: null,
						strobe_hz: 0
					},
					{
						id: 'Y',
						rgb: [40, 50, 60],
						intensity: 1,
						pan_deg: null,
						tilt_deg: null,
						strobe_hz: 0
					}
				]
			}
		]);
		const bare = frames.openReplay(bareArray);
		const cmds = stage.renderStage(bare.frameAt(0).frame, bare.patch, size);
		const discs = cmds.filter((c) => c.kind === 'disc');
		assert.equal(discs.length, 2);
		const xs = discs.map((d) => d.x);
		for (const x of xs) {
			assert.ok(x > 0 && x < size.width);
		}
		assert.notEqual(xs[0], xs[1]);
	} finally {
		globalThis.WebSocket = saved;
	}
});

test('stage-render.ts has no imports or banned globals', () => {
	const text = readFileSync('src/lib/lighting/stage-render.ts', 'utf8');
	assert.match(text, /export function renderStage/);
	const noBlockComments = text.replace(/\/\*[\s\S]*?\*\//g, '');
	const noLineComments = noBlockComments.replace(/\/\/.*$/gm, '');
	assert.doesNotMatch(noLineComments, /^\s*import\b/m);
	assert.doesNotMatch(noLineComments, /\bimport\(/);
	const banned =
		/\b(document|window|HTMLCanvasElement|HTMLElement|CanvasRenderingContext2D|requestAnimationFrame|svelte)\b/;
	assert.doesNotMatch(noLineComments, banned);
});

test('parseFrameMessage and parsePatch', () => {
	assert.throws(
		() =>
			frames.parseFrameMessage({
				t_ns: 0,
				fixtures: [{ id: 'a', rgb: [1, 2], intensity: 1 }]
			}),
		/fixtures\[0\]\.rgb/
	);
	const parsed = frames.parseFrameMessage({
		t_ns: 1,
		fixtures: [{ id: 'a', rgb: '#ff0000', intensity: 0.5 }]
	});
	assert.deepEqual(parsed.fixtures[0].rgb, [255, 0, 0]);
	assert.equal(parsed.fixtures[0].strobe_hz, 0);
	const p1 = frames.parsePatch({
		fixtures: [{ id: 'a', position: [0.1, 0.2], groups: [] }]
	});
	assert.equal(p1.fixtures[0].x, 0.1);
	const p2 = frames.parsePatch({
		fixtures: [{ id: 'b', position: { x: 0.3, y: 0.4 }, groups: [] }]
	});
	assert.equal(p2.fixtures[0].y, 0.4);
});

test('connectFrames with fake socket', () => {
	const urls = [];
	let fake = null;
	const factory = (url) => {
		urls.push(url);
		fake = {
			onopen: null,
			onmessage: null,
			onclose: null,
			onerror: null,
			closed: false,
			send() {},
			close() {
				this.closed = true;
			}
		};
		return fake;
	};
	const statuses = [];
	let gotFrame = null;
	const stop = frames.connectFrames(
		(frame) => {
			gotFrame = frame;
		},
		(st) => statuses.push(st),
		{ url: 'ws://test/x', socketFactory: factory }
	);
	assert.deepEqual(urls, ['ws://test/x']);
	const good = JSON.stringify({
		t_ns: 2,
		patch_version: null,
		fixtures: [{ id: 'z', rgb: [0, 0, 0], intensity: 1, strobe_hz: 0 }]
	});
	fake.onmessage({ data: good });
	assert.ok(gotFrame !== null);
	assert.equal(gotFrame.fixtures[0].id, 'z');
	const beforeErr = statuses.length;
	fake.onmessage({ data: '{"not":"a frame"}' });
	assert.ok(statuses.slice(beforeErr).some((s) => s.state === 'error'));
	stop();
	assert.equal(fake.closed, true);
});

function replayQueryAt(text, at_s) {
	let s = stageSession.newStageSession();
	s = stageSession.applyStageCommand(s, { type: 'load_replay', text, name: 'vectors.json' });
	s = stageSession.applyStageCommand(s, { type: 'seek', elapsed_s: at_s });
	s = stageSession.applyStageCommand(s, { type: 'pause' });
	return stageSession.stageQuery(s, { frame: null, patch: null });
}

test('stage-session reducer replay controls', () => {
	const text = JSON.stringify({
		frames: [
			{
				t_ns: 0,
				fixtures: [{ id: 'a', rgb: [0, 0, 0], intensity: 1, strobe_hz: 0 }]
			},
			{
				t_ns: 2e9,
				fixtures: [{ id: 'a', rgb: [0, 0, 0], intensity: 1, strobe_hz: 0 }]
			}
		]
	});
	let s = stageSession.newStageSession();
	s = stageSession.applyStageCommand(s, { type: 'load_replay', text });
	assert.equal(s.mode, 'replay');
	assert.equal(s.playing, true);
	assert.equal(s.elapsed_s, 0);
	s = stageSession.applyStageCommand(s, { type: 'toggle_play' });
	assert.equal(s.playing, false);
	s = stageSession.applyStageCommand(s, { type: 'play' });
	assert.equal(s.playing, true);
	s = stageSession.applyStageCommand(s, { type: 'seek', elapsed_s: -5 });
	assert.equal(s.elapsed_s, 0);
	s = stageSession.applyStageCommand(s, { type: 'seek', elapsed_s: 99 });
	assert.equal(s.elapsed_s, 2);
	s = stageSession.applyStageCommand(s, { type: 'pause' });
	s = stageSession.applyStageCommand(s, { type: 'tick', dt_s: 0.5 });
	assert.equal(s.elapsed_s, 2);
	s = stageSession.applyStageCommand(s, { type: 'play' });
	s = stageSession.applyStageCommand(s, { type: 'tick', dt_s: 1 });
	assert.equal(s.elapsed_s, 2);
	s = stageSession.applyStageCommand(s, { type: 'go_live' });
	assert.equal(s.mode, 'live');
	assert.equal(s.replay, null);
	s = stageSession.applyStageCommand(s, { type: 'go_live' });
	assert.equal(s.mode, 'live');
	assert.throws(() => stageSession.applyStageCommand(s, { type: 'play' }), /no replay/);
	assert.throws(
		() => stageSession.applyStageCommand(s, { type: 'load_replay', text: '{"frames":[]}' }),
		/frame list is empty/
	);
	assert.throws(() => stageSession.parseStageCommand({ type: 'nope' }), TypeError);
	assert.throws(() => stageSession.parseStageCommand({ type: 'seek', elapsed_s: NaN }), TypeError);
});

test('stageQuery JSON round-trip and patch_source', () => {
	const liveFrame = {
		t_ns: 0,
		patch_version: 1,
		fixtures: [
			{ id: 'L', rgb: [1, 2, 3], intensity: 1, pan_deg: null, tilt_deg: null, strobe_hz: 0 }
		]
	};
	const livePatch = { version: 1, fixtures: [{ id: 'L', x: 0.5, y: 0.5, groups: [] }] };
	const liveQ = stageSession.stageQuery(stageSession.newStageSession(), {
		frame: liveFrame,
		patch: livePatch
	});
	assert.deepEqual(JSON.parse(JSON.stringify(liveQ)), { ...liveQ });
	assert.equal(liveQ.frame?.fixtures[0].id, 'L');
	const withPatch = JSON.stringify({
		patch: { version: 2, fixtures: [{ id: 'A', x: 0.1, y: 0.2, groups: [] }] },
		frames: [
			{
				t_ns: 0,
				fixtures: [{ id: 'A', rgb: [1, 1, 1], intensity: 1, strobe_hz: 0 }]
			}
		]
	});
	let s = stageSession.applyStageCommand(stageSession.newStageSession(), {
		type: 'load_replay',
		text: withPatch
	});
	const qReplay = stageSession.stageQuery(s, { frame: null, patch: livePatch });
	assert.equal(qReplay.patch_source, 'replay');
	const bare = JSON.stringify([
		{
			t_ns: 0,
			fixtures: [{ id: 'X', rgb: [1, 1, 1], intensity: 1, strobe_hz: 0 }]
		}
	]);
	s = stageSession.applyStageCommand(stageSession.newStageSession(), {
		type: 'load_replay',
		text: bare
	});
	const qLivePatch = stageSession.stageQuery(s, { frame: null, patch: livePatch });
	assert.equal(qLivePatch.patch_source, 'live');
	s = stageSession.applyStageCommand(stageSession.newStageSession(), {
		type: 'load_replay',
		text: bare
	});
	const qNone = stageSession.stageQuery(s, { frame: null, patch: null });
	assert.equal(qNone.patch_source, null);
});

test('stage IPC dispatch and install guard', () => {
	let s = stageSession.newStageSession();
	const owner = {
		dispatch(cmd) {
			s = stageSession.applyStageCommand(s, cmd);
			return stageSession.stageQuery(s, { frame: null, patch: null });
		},
		query() {
			return stageSession.stageQuery(s, { frame: null, patch: null });
		},
		signature: () => 'sig'
	};
	const ipc = stageIpc.createLightingStageIpc(owner);
	assert.equal(ipc.version, 1);
	assert.throws(() => ipc.load_replay(42), TypeError);
	assert.throws(() => ipc.dispatch({ type: 'nope' }), TypeError);
	const text = JSON.stringify({
		frames: [{ t_ns: 0, fixtures: [{ id: 'a', rgb: [0, 0, 0], intensity: 1, strobe_hz: 0 }] }]
	});
	ipc.load_replay(text);
	const viaSeek = ipc.seek(0);
	const viaDispatch = ipc.dispatch({ type: 'seek', elapsed_s: 0 });
	assert.deepEqual(viaSeek, viaDispatch);
	const obj = { frames: JSON.parse(text).frames };
	assert.deepEqual(ipc.load_replay(obj).replay_name, ipc.load_replay(text).replay_name);
	assert.throws(() => stageIpc.installLightingStageIpc(ipc), /browser window/);
});

test('stage replay vectors match stageQuery', () => {
	const vectors = JSON.parse(readFileSync(VECTORS_PATH, 'utf8'));
	for (const entry of vectors.queries) {
		const doc = entry.which === 'replay_doc' ? vectors.replay_doc : vectors.bare_frames;
		const got = replayQueryAt(JSON.stringify(doc), entry.at_s);
		assert.deepEqual(got, entry.expected);
	}
	for (const err of vectors.errors) {
		assert.throws(
			() => replayQueryAt(err.text, 0),
			(e) => e.message === err.message
		);
	}
});

test('fetchPatch 404 no_patch includes code in error', async () => {
	await assert.rejects(
		() =>
			frames.fetchPatch(async () => ({
				ok: false,
				status: 404,
				json: async () => ({ detail: { code: 'no_patch', message: 'missing' } })
			})),
		(err) => err.message.includes('no_patch')
	);
});

test('parsePatch accepts daemon patch body shape', () => {
	const body = {
		version: 'a1b2c3d4',
		name: 'club',
		fixtures: [{ id: 'par-1', x: 0.1, y: 0.2, groups: ['pars'] }]
	};
	const patch = frames.parsePatch(body);
	assert.equal(patch.version, 'a1b2c3d4');
	assert.equal(patch.fixtures.length, 1);
	assert.equal(patch.fixtures[0].id, 'par-1');
	assert.equal(patch.fixtures[0].x, 0.1);
});

test('stage-session.ts has no DOM imports or globals', () => {
	const text = readFileSync('src/lib/lighting/stage-session.ts', 'utf8');
	const noBlockComments = text.replace(/\/\*[\s\S]*?\*\//g, '');
	const noLineComments = noBlockComments.replace(/\/\/.*$/gm, '');
	for (const line of noLineComments.split('\n')) {
		const m = line.match(/^\s*import\b.*from\s+['"]([^'"]+)['"]/);
		if (m) {
			assert.match(m[1], /^\.\/(frames-client|stage-render)$/);
		}
	}
	const banned = /\b(document|window|HTMLCanvasElement|requestAnimationFrame|WebSocket|svelte)\b/;
	assert.doesNotMatch(noLineComments, banned);
});
