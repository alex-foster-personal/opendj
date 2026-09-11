/**
 * LIVE-01: run only against a locally open Open DJ app.
 *
 * `node --test tests/live/ui-mirror-invariants.mjs` discovers the app from
 * its .engine.lock. OPENDJ_LIVE_LOCK_PATH selects a nonstandard data dir.
 * This tier is deliberately absent from CI because it measures a real page
 * and its real AudioContext, not a fixture or a test server.
 */
import assert from 'node:assert/strict';
import fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import test from 'node:test';

const SAMPLE_MS = 2_000;
const REQUEST_TIMEOUT_MS = 5_000;
const MAX_XRUN_RATE = 0.01;

function _lockCandidates() {
	const dataDir = process.env.MDT_DATA_DIR;
	return [
		process.env.OPENDJ_LIVE_LOCK_PATH,
		dataDir === undefined ? undefined : path.join(dataDir, '.engine.lock'),
		path.join(os.homedir(), 'Library', 'Application Support', 'com.opendj.desktop', '.engine.lock'),
		path.join(process.cwd(), 'data', '.engine.lock')
	].filter((candidate) => candidate !== undefined && candidate !== '');
}

async function _readEngineOrigin(lockCandidates) {
	for (const lockPath of lockCandidates) {
		let raw;
		try {
			raw = await fs.readFile(lockPath, 'utf8');
		} catch (error) {
			if (error && typeof error === 'object' && 'code' in error && error.code === 'ENOENT') continue;
			throw error;
		}
		const lock = JSON.parse(raw);
		if (typeof lock.host !== 'string' || !Number.isInteger(lock.port)) {
			throw new Error(`${lockPath} does not record an engine host and port`);
		}
		if (!['127.0.0.1', 'localhost', '::1'].includes(lock.host)) {
			throw new Error(`${lockPath} names non-loopback host ${lock.host}`);
		}
		if (lock.port < 1 || lock.port > 65_535) {
			throw new Error(`${lockPath} has invalid engine port ${lock.port}`);
		}
		return `http://${lock.host === '::1' ? '[::1]' : lock.host}:${lock.port}`;
	}
	return null;
}

async function _getJson(origin, pathname) {
	return fetch(`${origin}${pathname}`, { signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
}

function _playingDecks(mirror) {
	const decks = mirror.decks;
	if (typeof decks !== 'object' || decks === null) throw new Error('mirror.decks must be an object');
	return Object.entries(decks).filter(([, deck]) => (
		typeof deck === 'object' && deck !== null && deck.playing === true
	));
}

function _number(value, field) {
	if (typeof value !== 'number' || !Number.isFinite(value)) throw new Error(`${field} must be finite number`);
	return value;
}

function _record(value, field) {
	if (typeof value !== 'object' || value === null) throw new Error(`${field} must be an object`);
	return value;
}

export function assertMirrorInvariants(before, after) {
	assert.equal(before.context_state, 'running', 'AudioContext was not running at sample start');
	assert.equal(after.context_state, 'running', 'AudioContext was not running at sample end');
	const beforeXruns = _record(before.xrun_sentinel, 'before xrun sentinel');
	const afterXruns = _record(after.xrun_sentinel, 'after xrun sentinel');
	const callbackDelta = _number(afterXruns.callbacks, 'after xrun callbacks') -
		_number(beforeXruns.callbacks, 'before xrun callbacks');
	assert.ok(callbackDelta > 0, 'xrun sentinel callbacks did not arrive during the sample');
	const xrunDelta = _number(afterXruns.xruns, 'after xruns') - _number(beforeXruns.xruns, 'before xruns');
	assert.ok(xrunDelta >= 0, 'xrun counter went backward during the sample');
	assert.ok(
		xrunDelta / callbackDelta < MAX_XRUN_RATE,
		`xrun rate ${xrunDelta}/${callbackDelta} exceeds ${MAX_XRUN_RATE} per callback`
	);
	for (const [deckId, beforeDeck] of _playingDecks(before)) {
		const afterDeck = _record(after.decks, 'after decks')[deckId];
		assert.equal(afterDeck?.playing, true, `deck ${deckId} stopped while sampled`);
		const beforePosition = _number(_record(beforeDeck.position, `deck ${deckId} start position`).ms, `deck ${deckId} start position`);
		const afterPosition = _number(_record(afterDeck.position, `deck ${deckId} end position`).ms, `deck ${deckId} end position`);
		assert.ok(afterPosition > beforePosition, `deck ${deckId} playhead did not advance over ${SAMPLE_MS}ms`);
		assert.ok(_number(_record(before.master, 'start master').rms, 'start master RMS') > 0, 'master RMS was 0 at sample start while a deck was playing');
		assert.ok(_number(_record(after.master, 'end master').rms, 'end master RMS') > 0, 'master RMS was 0 at sample end while a deck was playing');
	}
}

export async function runLiveTier({ lockCandidates = _lockCandidates() } = {}) {
	const origin = await _readEngineOrigin(lockCandidates);
	if (origin === null) return { status: 'skipped', reason: 'no .engine.lock found' };
	let health;
	try {
		health = await _getJson(origin, '/api/v1/health');
	} catch (error) {
		if (error instanceof TypeError || (error instanceof Error && error.name === 'TimeoutError')) {
			return { status: 'skipped', reason: `engine from lock is unreachable: ${error.message}` };
		}
		throw error;
	}
	if (!health.ok) throw new Error(`engine health returned HTTP ${health.status}`);
	const first = await _getJson(origin, '/api/v1/state/ui-mirror');
	if (!first.ok) throw new Error(`ui mirror returned HTTP ${first.status}; open the performance page`);
	const before = await first.json();
	await new Promise((resolve) => setTimeout(resolve, SAMPLE_MS));
	const second = await _getJson(origin, '/api/v1/state/ui-mirror');
	if (!second.ok) throw new Error(`ui mirror returned HTTP ${second.status} after ${SAMPLE_MS}ms`);
	assertMirrorInvariants(before, await second.json());
	return { status: 'passed' };
}

test('LIVE-01 mirror invariants', async (context) => {
	const result = await runLiveTier();
	if (result.status === 'skipped') context.skip(result.reason);
});
