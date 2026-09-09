import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdtempSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';
import { before, describe, it } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

/**
 * CAPTURED PREFS BLOBS LOAD: a blob a PREVIOUS build persisted must load
 * cleanly in the current one, byte-for-byte as captured, never regenerated
 * from current DEFAULTS.
 *
 * Why this axis exists (Tue 1 Sep 2026): auto_play_enabled=true was written by
 * every prior session, then the feed-snapshot feature changed what being
 * enabled-at-mount MEANS, and the composed system froze an empty AutoPlay feed
 * for a whole live set. Nothing re-tests old persisted state when a new state
 * machine lands under it. These tests are that re-test.
 *
 * The three fixtures are REAL captures pulled from this machine's browser
 * Local Storage, and they happen to form a schema ladder: 8 keys written
 * before AutoPlay existed, 10 with AutoPlay half-added, 15 with the current
 * shape. Each is verified against fixtures/manifest.json before it is parsed
 * and is loaded through Node's REAL Web Storage in a child process rather than
 * a hand-written Map. All three constraints were Codex findings on #701: an
 * unchecked fixture means a green light from a rewritten baseline, a
 * test-owned Storage means a green light from test-owned behavior, and a
 * hand-built blob means a green light from state no build ever wrote.
 *
 * [if] a captured blob throws in _load [then] an upgrade wipes user prefs - broken.
 * [if] the 8-key blob loads but AutoPlay keys come back undefined rather than
 *   defaulted [then] an old profile lands in an unusable state - broken.
 * [if] a captured value silently maps to a different semantic [then] only a
 *   lifecycle test on the loaded values catches it - see the feed test below.
 */

const FIXTURES = fileURLToPath(new URL('./fixtures/', import.meta.url));
const PROBE = fileURLToPath(new URL('./prefs-golden-probe.mjs', import.meta.url));
const SUPPORTED_MANIFEST_VERSION = 2;

/** The manifest, refused unless its version is the one this file understands. */
function verifiedManifest() {
	const manifest = JSON.parse(readFileSync(join(FIXTURES, 'manifest.json'), 'utf8'));
	assert.equal(
		manifest.version,
		SUPPORTED_MANIFEST_VERSION,
		`fixture manifest declares version ${manifest.version}, this file supports ` +
			`${SUPPORTED_MANIFEST_VERSION}. Failing closed rather than reading a format ` +
			'whose meaning is not known.'
	);
	return manifest;
}

/** One capture's exact bytes, after its sha256 and its recorded length match. */
function verifiedCapture(manifest, name) {
	const entry = manifest.files[name];
	assert.ok(entry, `${name} is not listed in the fixture manifest`);
	const bytes = readFileSync(join(FIXTURES, name));
	assert.equal(
		bytes.length,
		entry.bytes,
		`${name} is ${bytes.length} bytes, manifest says ${entry.bytes}. These are ` +
			'byte-for-byte captures of what localStorage held; a length change means ' +
			'the file was rewritten, most likely reformatted.'
	);
	assert.equal(
		createHash('sha256').update(bytes).digest('hex'),
		entry.sha256,
		`${name} does not match its manifest checksum. Captures are IMMUTABLE: ` +
			'regenerating one to make a test pass deletes the only evidence of what ' +
			'the old build wrote.'
	);
	return bytes.toString('utf8');
}

/**
 * Load the production prefs module against real Web Storage seeded with `blob`.
 *
 * A child process, because real `localStorage` needs flags the unit runner does
 * not set. Each call gets its own storage file, so no test can observe
 * another's writes.
 */
function loadThroughRealStorage(storageKey, blob) {
	const store = join(mkdtempSync(join(tmpdir(), 'mdt-prefs-')), 'localstorage.db');
	const proc = spawnSync(
		process.execPath,
		['--experimental-webstorage', `--localstorage-file=${store}`, PROBE, storageKey, blob],
		{ encoding: 'utf8', timeout: 120000 }
	);
	assert.equal(proc.status, 0, `probe failed:\n${proc.stderr}`);
	return JSON.parse(proc.stdout);
}

const MANIFEST = verifiedManifest();
const STORAGE_KEY = MANIFEST.storage_key;
const CAPTURES = Object.keys(MANIFEST.files).map((name) => {
	const text = verifiedCapture(MANIFEST, name);
	return { name, text, entry: MANIFEST.files[name], parsed: JSON.parse(text) };
});

let autoPlay;
/** name -> what the production loader produced from that capture. */
const loaded = new Map();

before(async () => {
	autoPlay = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
	for (const c of CAPTURES) loaded.set(c.name, loadThroughRealStorage(STORAGE_KEY, c.text));
});

describe('captured prefs blobs load in the current build', () => {
	it('the fixture set really is a schema ladder, not three copies of one shape', () => {
		// A control on the EVIDENCE rather than on the loader. If every capture
		// carried the same keys, the suite below would look like three tests and
		// be one, and the 8-key backward-compatibility claim would be empty.
		const keyCounts = CAPTURES.map((c) => Object.keys(c.parsed).length);
		assert.deepEqual(keyCounts, [8, 10, 15], 'captures must span three schema eras');
		assert.deepEqual(
			keyCounts,
			CAPTURES.map((c) => c.entry.keys),
			'each capture must hold the key count the manifest recorded for it'
		);
		const oldest = new Set(Object.keys(CAPTURES[0].parsed));
		assert.equal(oldest.has('auto_play_enabled'), false, 'the 8-key capture predates AutoPlay');
		assert.equal(oldest.has('last_playlist'), false, 'and predates last_playlist');
	});

	for (const { name, entry } of CAPTURES) {
		it(`${name} (${entry.keys} keys, ${entry.bytes} bytes) loads and keeps its values`, () => {
			const got = loaded.get(name);
			const want = CAPTURES.find((c) => c.name === name).parsed;
			for (const [key, value] of Object.entries(want)) {
				assert.deepEqual(
					got[key],
					value,
					`${key} did not survive the load from ${name}`
				);
			}
		});
	}

	it('a capture written before AutoPlay existed still yields usable AutoPlay prefs', () => {
		// The failure this guards is not a throw. An 8-key blob that loads but
		// leaves auto_play_enabled undefined puts an old profile into a state no
		// control can render, so the value must come back DEFAULTED rather than
		// missing - and it must be the off-by-default value, because silently
		// switching AutoPlay on for an old profile is the incident this PR is
		// about, in reverse.
		const got = loaded.get('prefs-blob-capture-6e8bf3c9.json');
		assert.equal(typeof got.auto_play_enabled, 'boolean');
		assert.equal(typeof got.auto_play_enforce_order, 'boolean');
		assert.equal(typeof got.auto_play_maximize_reach, 'boolean');
		assert.equal(got.usb_toast_enabled !== undefined, true, 'usb prefs must default too');
	});

	it.skip(
		'UNAVAILABLE: ignores keys a later build wrote (downgrade tolerance) - ' +
			'no captured blob carries a key this build does not declare. All 16 real ' +
			'localStorage captures on this machine hold current or older key sets, ' +
			'which is what you would expect since no later build has shipped. An ' +
			'earlier revision spread a captured blob and added an invented ' +
			'a_key_from_a_later_build to stand in for one; that fabricated the state ' +
			'under test and is gone (Codex, #701). Un-skip when a real payload from a ' +
			'later build exists to capture.',
		() => {}
	);

	it('lifecycle: a captured auto_play_enabled=true composes with the feed snapshot', () => {
		// The incident path in one place: OLD persisted enabled=true observed
		// against an EMPTY view at mount must not arm the snapshot. This is the
		// cross-layer test neither the prefs suite nor the snapshot suite could
		// express alone, and it runs on the value that came back through real
		// storage rather than on a literal.
		const got = loaded.get('prefs-blob-capture-28fe8192.json');
		assert.equal(got.auto_play_enabled, true, 'the capture must actually carry enabled=true');

		const snap = autoPlay.createAutoPlayFeedSnapshot();
		// Pin 0e5fa1 added the playlist scope: the same playlist across both
		// observations, so this still exercises hydration and not a switch.
		const atMount = snap.step(got.auto_play_enabled, 'playlist:vas', []);
		assert.equal(atMount.snapshotted, false, 'persisted-on + empty view must not freeze');
		assert.equal(snap.active, false);

		const hydrated = snap.step(got.auto_play_enabled, 'playlist:vas', [
			{ stable_id: 'a', key: '8A', bpm: 128, file_exists: true }
		]);
		assert.equal(hydrated.snapshotted, true, 'first rows take the activation snapshot');
	});
});
