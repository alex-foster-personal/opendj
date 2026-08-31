import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

// A deck-load ring row used to carry stage DURATIONS only, so "was that load
// stemmed or mix-only?" had no answer in the row. Classifying a run of loads
// meant re-deriving it by hand, off-machine, from which stage names happened
// to appear (fetchStems/decodeStems present => stemmed). That turned one perf
// question into JSONL archaeology over ssh. The row now states it at write
// time:
// - if a stemmed load stops recording stemmed=1 then the classification is
//   back to guessing from stage names -- broken
// - if a mix-only load records anything other than stemmed=0 then mix-only
//   loads are indistinguishable from pre-field rows -- broken
// - if a load that failed BEFORE the probe answered records stemmed=0 then the
//   ring invents a mix-only verdict nobody measured -- broken
// - if an unrecognized stem status silently reports mix-only instead of
//   throwing then the ring lies about what played -- broken
// - if labels leak into stages then anything summing stages as durations trips
//   over a string -- broken

const READY_ROFORMER = {
	status: 'ready',
	source: 'roformer',
	model: 'mel-band-roformer',
	layout: 'roformer2',
	error: null
};

const MIX_ONLY = {
	status: 'unavailable',
	source: null,
	model: null,
	layout: null,
	error: 'STEM_BUNDLE_NOT_FOUND: no stem bundle exists'
};

let _ring = null;

/** The in-memory ring is the authoritative copy and is readable immediately;
 * the localStorage flush is deliberately coalesced onto a timer so it stays
 * off the gesture path, so asserting on storage here would race that timer. */
async function _load() {
	const log = await loadTypeScriptModule('src/lib/rb/perf-event-log.ts');
	_ring = log;
	return { mod: log, log };
}

function _lastRow() {
	const rows = _ring.readPerfEvents();
	assert.notEqual(rows.length, 0, 'the ring recorded nothing');
	return rows[rows.length - 1];
}

/** Drive the real write path and return the row it appended. */
function _writeAndRead(mod, log, stems, stages = { total: 3813, probeStem: 1990 }) {
	const realInfo = console.info;
	console.info = () => {};
	try {
		mod.recordDeckLoadTiming('deck-load sid=deadbeef', stages, 1, stems);
	} finally {
		console.info = realInfo;
	}
	return _lastRow();
}

test('a stemmed load records stemmed=1 plus the layout that actually played', async () => {
	const { mod, log } = await _load();
	const row = _writeAndRead(mod, log, READY_ROFORMER);
	assert.equal(row.stages.stemmed, 1);
	assert.deepEqual(row.labels, {
		stemLayout: 'roformer2',
		stemSource: 'roformer',
		stemModel: 'mel-band-roformer'
	});
	// The layout is read from what the deck published, never assumed to be the
	// 4-part default: a row claiming demucs4 for a 2-part bundle would
	// misreport which controls the deck could actually drive.
	assert.match(row.message, /stemLayout=roformer2/);
});

test('a mix-only load records stemmed=0 and an explicit layout of none', async () => {
	const { mod, log } = await _load();
	const row = _writeAndRead(mod, log, MIX_ONLY);
	assert.equal(row.stages.stemmed, 0);
	// Explicit 'none', not an absent key: that is what lets a reader tell a
	// mix-only load apart from a row written before this field existed.
	assert.deepEqual(row.labels, { stemLayout: 'none' });
});

test('a load that failed before the probe answered withholds the flag', async () => {
	const { mod, log } = await _load();
	// error === null is the deck's initial stem state: the probe never
	// resolved, so there is no stemmed answer to record.
	const row = _writeAndRead(mod, log, { ...MIX_ONLY, error: null });
	assert.equal('stemmed' in row.stages, false, 'an unmeasured verdict must not be fabricated');
	assert.deepEqual(row.labels, { stemLayout: 'unresolved' });
});

test('a broken stem bundle records mix-only playback and says why', async () => {
	const { mod, log } = await _load();
	const row = _writeAndRead(mod, log, { ...MIX_ONLY, status: 'error', error: 'alignment mismatch' });
	assert.equal(row.stages.stemmed, 0);
	assert.deepEqual(row.labels, { stemLayout: 'error' });
});

test('an unrecognized stem status throws instead of defaulting to mix-only', async () => {
	const { mod, log } = await _load();
	assert.throws(
		() => _writeAndRead(mod, log, { ...MIX_ONLY, status: 'partially-ready' }),
		/unhandled stem status/,
		'an unknown state must fail loudly, never be recorded as stemmed=0'
	);
});

test('labels stay out of stages, and rows written without them keep their shape', async () => {
	const { mod, log } = await _load();
	const row = _writeAndRead(mod, log, READY_ROFORMER);
	// stages stays a pure ms-per-stage map (plus the numeric stemmed flag), so
	// anything summing or charting it is safe.
	assert.deepEqual(row.stages, { total: 3813, probeStem: 1990, stemmed: 1 });

	// Additive: a caller that passes no labels writes the row it always did.
	const realInfo = console.info;
	console.info = () => {};
	try {
		log.recordPerfTiming('audio-context', { ctxCreate: 12 }, null);
	} finally {
		console.info = realInfo;
	}
	const plain = _lastRow();
	assert.equal('labels' in plain, false);
	assert.deepEqual(plain.stages, { ctxCreate: 12 });
});

test('the caller stages object is not mutated by recording a row', async () => {
	const { mod, log } = await _load();
	// audio-engine copies stages into DeckState BEFORE the ring write; a
	// mutating recorder would make those two disagree depending on ordering.
	const stages = { total: 100 };
	_writeAndRead(mod, log, READY_ROFORMER, stages);
	assert.deepEqual(stages, { total: 100 });
});
