// requirement: Trackify decks never probe, fetch, decode or adopt stems
// (PERFMODE-15, issue #3974), while Gig keeps its lazy stem upgrade (STEM-37).
//
// Measured on silver Fri 25 Sep 2026: in a 1 h unattended Trackify run the deck
// requested stems/vocals|drums|bass|other for f58d2482 and decoded PCM rose
// from 86 MB to 428 MB. Trackify is a listening player with no stems, so the
// mode, not each call site, must switch stem decode off.
//
// [if] a Trackify session is mounted [then] the stem decode policy is blocked,
//   and it is released when the session tears down [⛔️ if Gig inherits it]
// [if] the lazy stem upgrade runs while Trackify is mounted [then] no /stems
//   request is made and the deck settles `unavailable` naming Trackify
// [if] no Trackify session is mounted (Gig) [then] the same upgrade still
//   probes the stem bundle (control: a gate that is always on fails here)
// [if] an upgrade is already probing when Trackify mounts [then] it stops
//   before fetching any stem part
// [if] any code path can reach a stem probe, fetch, decode or processor swap
//   [then] it goes through the gated upgrade or drain in the engine
import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { afterEach, before, beforeEach, describe, it } from 'node:test';

import { importBundledSource } from './import-bundled-source.mjs';
import { bundleTypeScriptModule } from './load-typescript.mjs';

const SID = '3746c6dcb689588e5c75c9ecca659a497542e237';
const READY_MANIFEST = {
	schema: 1,
	stable_id: SID,
	source: 'demucs',
	model: 'hdemucs_mmi',
	layout: 'demucs4',
	sample_rate_hz: 44100,
	frame_count: 11579904,
	channel_count: 2,
	parts: {
		vocals: { media_type: 'audio/mpeg' },
		drums: { media_type: 'audio/mpeg' },
		bass: { media_type: 'audio/mpeg' },
		other: { media_type: 'audio/mpeg' }
	}
};
const MIX_BUFFER = { sampleRate: 44100, length: 11579904, duration: 11579904 / 44100 };
const SRC_ROOT = fileURLToPath(new URL('../../src', import.meta.url));

/** @type {string} */
let bundleText;
/** @type {Record<string, any>} */
let entry;
/** Every URL the code under test fetched, in order. */
let fetched = [];
/** Per-test override for the stem manifest probe response. */
let stemProbe = null;

const json = (body) =>
	new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });

function stemRequests() {
	return fetched.filter((url) => /\/stems(\/|$|\?)/.test(url));
}

function installFakeEngine() {
	entry.engine.load = async () => {};
	entry.engine.unload = async () => {};
	entry.engine.play = async (deckId) => {
		entry.deckStates[deckId].playing = true;
	};
	entry.engine.pause = async (deckId) => {
		entry.deckStates[deckId].playing = false;
	};
	entry.engine.dispose = async () => {};
}

describe('Trackify never decodes stems; Gig still upgrades lazily (PERFMODE-15)', { concurrency: false }, () => {
	before(async () => {
		bundleText = await bundleTypeScriptModule('tests/unit/fixtures/trackify-no-stems-entry.ts', {
			dev: true
		});
		globalThis.__musicDjToolsTestState = (value) => value;
		globalThis.__musicDjToolsTestState.snapshot = (value) =>
			value === undefined ? undefined : JSON.parse(JSON.stringify(value));
	});

	beforeEach(async () => {
		entry = await importBundledSource(bundleText, 'trackify-no-stems-entry');
		fetched = [];
		stemProbe = null;
		globalThis.window = {};
		globalThis.fetch = async (input) => {
			const url = typeof input === 'string' ? input : input.url;
			fetched.push(url);
			// A stem PART request answers 500 so a leaked fetch fails fast and
			// visibly instead of hanging in decode with no AudioContext.
			if (url.includes(`/tracks/${SID}/stems/`)) return new Response('stem part', { status: 500 });
			if (url.includes(`/tracks/${SID}/stems`)) {
				if (stemProbe !== null) return stemProbe();
				return json({ status: 'unavailable', code: 'STEM_BUNDLE_NOT_FOUND', message: 'no stem bundle' });
			}
			return json({ items: [], next_cursor: null });
		};
		entry.e2ePrimeTrackifyFeed([]);
		entry.resetLibraryModeRuntimeForTest();
		installFakeEngine();
		entry.deckStates[1].stable_id = SID;
	});

	afterEach(() => {
		delete globalThis.window;
	});

	it('a mounted Trackify session blocks stem decode, and its teardown releases the block', async () => {
		assert.equal(entry.stemDecodeBlockReason(), null, 'nothing may block stems before Trackify mounts');
		const uninstall = entry.installTrackifySession();
		try {
			assert.match(entry.stemDecodeBlockReason() ?? '', /Trackify/);
		} finally {
			await uninstall();
		}
		// Control for the overshoot direction: a block that outlives Trackify
		// would take stems away from the Gig session that mounts next.
		assert.equal(entry.stemDecodeBlockReason(), null, 'Gig must not inherit the Trackify block');
	});

	it('the per-upgrade block check settles only the load it belongs to', () => {
		const deck = { stems: { status: 'loading' } };
		let current = true;
		const blocked = entry.stemBlockCheck(deck, () => current);
		assert.equal(blocked(), false, 'no block: the upgrade may run');
		assert.equal(deck.stems.status, 'loading', 'no block: the deck state is untouched');
		const release = entry.blockStemDecode('Trackify mode has no stems (test)');
		try {
			current = false;
			assert.equal(blocked(), true, 'a superseded upgrade must still stop');
			// Control for the overshoot direction: the deck has moved on to another
			// track, so this upgrade must not write its state over the new load's.
			assert.equal(deck.stems.status, 'loading', 'a superseded upgrade must not settle the deck');
			current = true;
			assert.equal(blocked(), true);
			assert.equal(deck.stems.status, 'unavailable');
			assert.match(deck.stems.error ?? '', /stems disabled: Trackify/);
		} finally {
			release();
		}
	});

	it('with Trackify mounted, the lazy stem upgrade makes no stem request and settles the deck unavailable', async () => {
		const uninstall = entry.installTrackifySession();
		try {
			await entry.upgradeDeckStemsForTest(1, SID, MIX_BUFFER);
			assert.deepEqual(stemRequests(), [], 'a Trackify deck must not probe or fetch stems');
			assert.equal(entry.deckStates[1].stems.status, 'unavailable');
			assert.match(entry.deckStates[1].stems.error ?? '', /Trackify/);
		} finally {
			await uninstall();
		}
	});

	it('control: with no Trackify session (Gig), the same upgrade still probes the stem bundle lazily', async () => {
		await entry.upgradeDeckStemsForTest(1, SID, MIX_BUFFER);
		assert.deepEqual(stemRequests().map((url) => url.replace(/^.*\/api\//, '/api/')), [
			`/api/v1/tracks/${SID}/stems`
		]);
		assert.equal(entry.deckStates[1].stems.status, 'unavailable');
		assert.doesNotMatch(entry.deckStates[1].stems.error ?? '', /Trackify/);
	});

	it('an upgrade already probing when Trackify mounts stops before fetching any stem part', async () => {
		let answerProbe = () => {};
		stemProbe = () =>
			new Promise((resolve) => {
				answerProbe = () => resolve(json(READY_MANIFEST));
			});
		const upgrade = entry.upgradeDeckStemsForTest(1, SID, MIX_BUFFER);
		await new Promise((resolve) => setImmediate(resolve));
		assert.equal(stemRequests().length, 1, 'the probe must be in flight before Trackify mounts');

		const uninstall = entry.installTrackifySession();
		try {
			answerProbe();
			await upgrade;
			const parts = stemRequests().filter((url) => url.includes(`/stems/`));
			assert.deepEqual(parts, [], 'no stem part may be fetched once Trackify has mounted');
			assert.equal(entry.deckStates[1].stems.status, 'unavailable');
			assert.match(entry.deckStates[1].stems.error ?? '', /Trackify/);
		} finally {
			await uninstall();
		}
	});
});

// ------------------------------------------------------------------ the class

/** Body of the first function whose declaration starts with `signature`,
 * found by brace matching from its opening brace. */
function functionBody(source, signature) {
	const start = source.indexOf(signature);
	assert.ok(start >= 0, `${signature} not found - this test is reading the wrong file`);
	const open = source.indexOf('{', source.indexOf(')', start));
	let depth = 0;
	for (let i = open; i < source.length; i += 1) {
		if (source[i] === '{') depth += 1;
		else if (source[i] === '}') {
			depth -= 1;
			if (depth === 0) return { start: open, end: i, text: source.slice(open, i + 1) };
		}
	}
	throw new Error(`unbalanced braces after ${signature}`);
}

function sourceFiles(dir) {
	const out = [];
	for (const name of readdirSync(dir)) {
		const path = join(dir, name);
		if (statSync(path).isDirectory()) out.push(...sourceFiles(path));
		else if (/\.(ts|svelte)$/.test(name) && !name.endsWith('.d.ts') && name !== 'api-types.ts') out.push(path);
	}
	return out;
}

/** Call sites (not declarations) of `callee(` in `text`. */
function callSites(text, callee) {
	const sites = [];
	const pattern = new RegExp(`(?<![\\w.])${callee.replace('.', '\\.')}\\(`, 'g');
	for (const match of text.matchAll(pattern)) {
		const lineStart = text.lastIndexOf('\n', match.index) + 1;
		const line = text.slice(lineStart, text.indexOf('\n', match.index));
		if (/^\s*(export\s+)?(async\s+)?function\s/.test(line) || /^\s*(\*|\/\/)/.test(line)) continue;
		sites.push(match.index);
	}
	return sites;
}

/** Uses of `name` as a value (a call, or handing it on as a callback), with
 * import/re-export statements, its own declaration and comments removed. */
function valueReferences(text, name) {
	const code = text
		.replace(/(?:import|export)\s[^;]*?from\s*['"][^'"]+['"];?/gs, '')
		.replace(/\/\*[\s\S]*?\*\//g, '')
		.replace(/\/\/[^\n]*/g, '');
	const pattern = new RegExp(`(?<![\\w.])${name.replace('.', '\\.')}(?![\\w])`, 'g');
	return [...code.matchAll(pattern)].filter((match) => {
		const lineStart = code.lastIndexOf('\n', match.index) + 1;
		const line = code.slice(lineStart, code.indexOf('\n', match.index));
		return !/^\s*(export\s+)?(async\s+)?function\s/.test(line);
	}).length;
}

describe('every stem decode entry point goes through the gated engine functions (PERFMODE-15 class)', () => {
	const ENGINE = join(SRC_ROOT, 'lib/rb/audio-engine.svelte.ts');
	const engineSource = readFileSync(ENGINE, 'utf8');
	const upgrade = functionBody(engineSource, 'async function _upgradeDeckStems(');
	const drain = functionBody(engineSource, 'function _drainPendingStemUpgrade(');

	it('outside the engine, only the stem API helpers themselves reach a stem probe, fetch or decode', () => {
		const callers = new Map();
		for (const file of sourceFiles(SRC_ROOT)) {
			const text = readFileSync(file, 'utf8');
			for (const callee of [
				'probeStemArtifact',
				'awaitStemArtifact',
				'fetchStemAudioArrayBuffers',
				'decodeStemBuffers',
				'AlignedStemDeckProcessor.create'
			]) {
				if (valueReferences(text, callee) === 0) continue;
				const rel = file.slice(SRC_ROOT.length + 1);
				callers.set(rel, [...(callers.get(rel) ?? []), callee]);
			}
		}
		// A new caller anywhere is a new stem decode entry point: it must be
		// gated on the stem decode policy and added here on purpose.
		assert.deepEqual(Object.fromEntries([...callers].sort()), {
			'lib/rb/audio-engine.svelte.ts': [
				'awaitStemArtifact',
				'fetchStemAudioArrayBuffers',
				'decodeStemBuffers',
				'AlignedStemDeckProcessor.create'
			],
			'lib/rb/stem-hydrate-wait.ts': ['probeStemArtifact']
		});
	});

	it('inside the engine, every stem probe, fetch, decode and processor create is in _upgradeDeckStems', () => {
		for (const callee of [
			'awaitStemArtifact',
			'fetchStemAudioArrayBuffers',
			'decodeStemBuffers',
			'AlignedStemDeckProcessor.create'
		]) {
			const sites = callSites(engineSource, callee);
			assert.ok(sites.length > 0, `${callee} not called at all - the funnel check would pass vacuously`);
			for (const at of sites) {
				assert.ok(at > upgrade.start && at < upgrade.end, `${callee} called outside _upgradeDeckStems`);
			}
		}
	});

	it('_upgradeDeckStems settles a blocked deck before it probes, and its stale check reads the policy', () => {
		const probe = upgrade.text.indexOf('awaitStemArtifact(');
		const gate = upgrade.text.indexOf('if (_stemDecodeBlocked()) return;');
		assert.match(upgrade.text, /const _stemDecodeBlocked = stemBlockCheck\(st, \(\) => token === rt\.loadToken\);/, '_upgradeDeckStems never reads the policy');
		assert.ok(gate >= 0 && gate < probe, 'the blocked check must return before the probe');
		assert.match(upgrade.text, /const stale = \(\): boolean =>[^\n]*_stemDecodeBlocked\(\)/);
	});

	// Found by the exact-SHA evidence run on PR #4039: Trackify loads pass
	// `stems: false` (#3975), so `_upgradeDeckStems` never runs for them, and the
	// deck published the `loading` placeholder with nothing left to settle it.
	it('a load with stems off publishes a settled unavailable state, never the loading placeholder', () => {
		const loadStart = engineSource.indexOf('async load(deck: DeckId, stable_id: string, options: DeckLoadOptions = {})');
		assert.ok(loadStart > 0, 'load() not found');
		const load = engineSource.slice(loadStart, engineSource.indexOf('\n\t}\n', loadStart));
		const assignments = [...load.matchAll(/candidateStemState = ([^;]+);/g)].map((match) => match[1]);
		assert.ok(assignments.length > 0, 'load() never sets the candidate stem state');
		for (const value of assignments) {
			assert.match(
				value,
				/^loadStems \? loadingStemDeckState\(\) : \(stemsBlockedState\(\) \?\? unavailableStemDeckState\('stems disabled for this load'\)\)$/,
				`a stems-off load can publish \`${value}\`, a loading state nothing will settle`
			);
		}
	});

	it('a held stem upgrade is only ever adopted by the gated upgrade or the gated drain', () => {
		const sites = callSites(engineSource, '_adoptStemProcessor');
		assert.ok(sites.length > 0, '_adoptStemProcessor not called at all');
		for (const at of sites) {
			const inUpgrade = at > upgrade.start && at < upgrade.end;
			const inDrain = at > drain.start && at < drain.end;
			assert.ok(inUpgrade || inDrain, '_adoptStemProcessor called outside the gated functions');
		}
		const policyRead = drain.text.indexOf('stemsBlockedState()');
		const adopt = drain.text.indexOf('_adoptStemProcessor(');
		assert.ok(policyRead >= 0 && policyRead < adopt, 'the drain must read the policy before adopting');
	});
});
