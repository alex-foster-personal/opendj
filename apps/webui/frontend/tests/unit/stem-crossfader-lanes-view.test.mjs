// requirement: STEMPERF-14650
/** Stem crossfader lanes pure view (src/lib/rb/stem-crossfader-lanes-view.ts). */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let NO_DECK_REASON, laneDragIntent, laneRelinkIntent, stemCrossfaderLanesView;
before(async () => {
	({ NO_DECK_REASON, laneDragIntent, laneRelinkIntent, stemCrossfaderLanesView } =
		await loadTypeScriptModule('src/lib/rb/stem-crossfader-lanes-view.ts'));
});

const DEMUCS = ['vocal', 'instrumental', 'drums', 'bass', 'other'];
const ROFORMER = ['vocal', 'instrumental'];

test('two demucs4 decks give all five lanes in order', () => {
	/** [if] two demucs4 decks do not get five ordered lanes [then] a drivable stem has no lane, [else stop]. */
	const v = stemCrossfaderLanesView({ a: DEMUCS, b: DEMUCS, hover: true });
	assert.deepEqual(
		v.lanes.map((l) => l.stem),
		DEMUCS
	);
	assert.deepEqual(v.inert, []);
	assert.equal(v.reason, null);
});

test('one roformer2 deck limits lanes to vocal and instrumental', () => {
	/** [if] a roformer2 deck yields drums/bass/other lanes [then] lanes would drive nothing, [else stop]. */
	const v = stemCrossfaderLanesView({ a: DEMUCS, b: ROFORMER, hover: true });
	assert.deepEqual(
		v.lanes.map((l) => l.stem),
		['vocal', 'instrumental']
	);
	assert.deepEqual(v.inert, [
		{ stem: 'drums', reason: 'mixed into INST' },
		{ stem: 'bass', reason: 'mixed into INST' },
		{ stem: 'other', reason: 'mixed into INST' }
	]);
});

test('no deck on a bus gives no lanes and one reason', () => {
	/** [if] a missing deck still yields lanes or many reasons [then] the placeholder is wrong, [else stop]. */
	const v = stemCrossfaderLanesView({ a: DEMUCS, b: null, hover: true });
	assert.deepEqual(v.lanes, []);
	assert.deepEqual(v.inert, []);
	assert.equal(v.reason, NO_DECK_REASON);
	assert.equal(v.open, false);
});

test('lanes open on hover or while any lane is unlinked, with glyphs', () => {
	/** [if] lanes show without hover while all linked, or hide while one is unlinked [then] deck floors move, [else stop]. */
	assert.equal(stemCrossfaderLanesView({ a: DEMUCS, b: DEMUCS, hover: false }).open, false);
	assert.equal(stemCrossfaderLanesView({ a: DEMUCS, b: DEMUCS, hover: true }).open, true);
	const v = stemCrossfaderLanesView({
		a: DEMUCS,
		b: DEMUCS,
		hover: false,
		lanes: { vocal: { linked: false, value: 1 } }
	});
	assert.equal(v.open, true);
	assert.equal(v.lanes[0].glyph, 'unlink');
	assert.equal(v.lanes[0].value, 1);
	assert.ok(v.lanes.slice(1).every((l) => l.linked && l.glyph === 'link'));
});

test('drag unlinks with a clamped value, double-click relinks', () => {
	/** [if] gestures map to the wrong stem_crossfader payload [then] lanes would not drive the bus, [else stop]. */
	assert.deepEqual(laneDragIntent('drums', 0.3), {
		type: 'stem_crossfader',
		stem: 'drums',
		value: 0.3,
		linked: false
	});
	assert.equal(laneDragIntent('drums', 1.7).value, 1);
	assert.equal(laneDragIntent('drums', -2).value, 0);
	assert.deepEqual(laneRelinkIntent('vocal'), {
		type: 'stem_crossfader',
		stem: 'vocal',
		linked: true
	});
});
