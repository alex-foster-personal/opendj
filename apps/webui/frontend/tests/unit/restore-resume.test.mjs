// Mon 5 Oct 2026, silver preview: a pane opened with d1..d4 in the URL loaded
// d1 and d2, lost the mirror lease to another window, and every later deck
// failed "performance session restore was disposed". Pressing "Take control"
// re-promoted it with deck restore skipped, so its snapshot writer published
// the half-restored engine and the URL dropped d3 and d4 for good.
//
// requirement: AGENT-18
// [if] a re-promoted tab's first restore was cut short [then] it loads the URL's empty decks, each its own id
// [if] a deck is already loaded when the restore resumes [then] it is left alone
// [if] the resume runs [then] the URL keeps all four ids
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let session;
let deeplink;

before(async () => {
	({ session } = await loadTypeScriptModule('tests/unit/fixtures/performance-session-restore-entry.ts'));
	deeplink = await loadTypeScriptModule('src/lib/rb/performance-deeplink.ts');
});

const URL_IDS = { 1: 'sid-one', 2: 'sid-two', 3: 'sid-three', 4: 'sid-four' };
const SEARCH = `?d1=${URL_IDS[1]}&d2=${URL_IDS[2]}&d3=${URL_IDS[3]}&d4=${URL_IDS[4]}`;

function deck(stable_id) {
	return {
		stable_id,
		playing: false,
		position_ms: 0,
		pitch: 1,
		pitch_range: 8,
		quantize_enabled: true,
		beat_sync_enabled: false,
		master_tempo_enabled: true,
		key_sync_enabled: false,
		stems: {
			available_controls: ['vocal', 'instrumental', 'drums'],
			controls: {
				vocal: { muted: false, solo: false, gain: 0.5 },
				instrumental: { muted: false, solo: false, gain: 0.5 },
				drums: { muted: false, solo: false, gain: 0.5 }
			}
		}
	};
}

function engine(loaded) {
	const channel = { trim: 0.5, eq_high: 0.5, eq_mid: 0.5, eq_low: 0.5, filter: 0.5, fader: 1, assign: 'THRU' };
	return {
		browser: { active_playlist: null },
		master_deck: null,
		mixer: { crossfader: 0.5, master: 0.5, channels: { 1: channel, 2: channel, 3: channel, 4: channel } },
		decks: { 1: deck(loaded[1] ?? null), 2: deck(loaded[2] ?? null), 3: deck(loaded[3] ?? null), 4: deck(loaded[4] ?? null) }
	};
}

/** Run one restore install against a fake engine; returns what it dispatched and the final URL. */
async function run(loaded, restoreOpts) {
	globalThis.window = {};
	const state = engine(loaded);
	const dispatched = [];
	const location = { pathname: '/performance', search: SEARCH, href: `http://127.0.0.1/performance${SEARCH}` };
	try {
		const dispose = session.installPerformanceSessionRestore({
			location,
			storage: { getItem: () => null, setItem: () => {} },
			replaceState: () => {},
			dispatch: async (command) => {
				dispatched.push(command);
				if (command.type === 'load') state.decks[command.deck].stable_id = command.stable_id;
				return state;
			},
			query: () => state,
			document: { hidden: false, addEventListener: () => {}, removeEventListener: () => {} },
			window: { addEventListener: () => {}, removeEventListener: () => {} },
			setInterval: () => 1,
			clearInterval: () => {},
			commandSession: () => 1,
			operatorMaster: () => null,
			...restoreOpts
		});
		for (let i = 0; i < 30; i += 1) await new Promise((resolve) => setImmediate(resolve));
		dispose();
	} finally {
		delete globalThis.window;
	}
	return {
		loads: dispatched.filter((c) => c.type === 'load').map((c) => ({ deck: c.deck, stable_id: c.stable_id })),
		mixer: dispatched.filter((c) => c.type === 'crossfader' || c.type === 'master_volume'),
		urlIds: deeplink.parseLv2Ids(location.search)
	};
}

test('a resumed restore loads only the empty URL decks, each with its own id', async () => {
	const out = await run({ 1: URL_IDS[1], 2: URL_IDS[2] }, { resumeInterruptedRestore: true });
	assert.deepEqual(out.loads, [
		{ deck: 3, stable_id: URL_IDS[3] },
		{ deck: 4, stable_id: URL_IDS[4] }
	]);
	assert.deepEqual(out.mixer, [], 'a resume never replays the mixer over a live set');
	assert.deepEqual(out.urlIds, URL_IDS, 'the URL keeps all four ids');
});

test('a resume never replaces a deck that already holds a track', async () => {
	const out = await run({ 1: URL_IDS[1], 2: URL_IDS[2], 3: 'operator-choice' }, { resumeInterruptedRestore: true });
	assert.deepEqual(out.loads, [{ deck: 4, stable_id: URL_IDS[4] }]);
	assert.equal(out.urlIds[3], 'operator-choice');
});

test('mutation control: the old re-promotion (deck restore skipped) drops d3 and d4 from the URL', async () => {
	const out = await run({ 1: URL_IDS[1], 2: URL_IDS[2] }, { skipDeckRestore: true });
	assert.deepEqual(out.loads, []);
	assert.deepEqual(out.urlIds, { 1: URL_IDS[1], 2: URL_IDS[2] });
});

test('the settled callback fires after a resume that ran to its end', async () => {
	let settled = 0;
	await run({}, { resumeInterruptedRestore: true, onDeckRestoreSettled: () => (settled += 1) });
	assert.equal(settled, 1);
});
