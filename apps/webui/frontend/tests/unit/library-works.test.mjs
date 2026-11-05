import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
	EMPTY_OVERLAY,
	classicalArtist,
	classicalTitle,
	composerOf,
	filedUnderText,
	normalizeOptionalColumns,
	performerOf,
	stackIndicator,
	tracksViaWork,
	workCellText,
	worksSortValue
} from '../../src/lib/rb/library-works.ts';

const work = (over = {}) => ({
	kind: 'classical', title: 'Requiem', composer: 'Mozart', catalogue: 'K. 626', movement: null,
	aliases: [], member_count: 2, ...over
});
const overlay = {
	works: { w: work({ aliases: ['Requiem Mass'] }), dies: work({ title: 'Dies irae', member_count: 1 }),
		solo: work({ title: 'Alone', member_count: 1 }),
		song: work({ kind: 'song', title: 'Crazy in Love', composer: null, catalogue: null }) },
	members: {
		lac: [{ work_id: 'w', role: 'recording', is_primary: true }],
		mash: [
			{ work_id: 'dies', role: 'mashup', is_primary: false },
			{ work_id: 'w', role: 'mashup', is_primary: true }
		],
		one: [{ work_id: 'solo', role: 'remix', is_primary: true }],
		cil: [{ work_id: 'song', role: 'remix', is_primary: true }],
		remix: [{ work_id: 'w', role: 'remix', is_primary: true }]
	},
	classical: {
		lac: { movement: 'Lacrimosa', ensemble: 'Wiener Philharmoniker', conductor: 'Karajan',
			label: 'DG', recording_year: 1962 }
	}
};

test('a plain track shows no indicator and a one member work shows none either', () => {
	assert.equal(stackIndicator(EMPTY_OVERLAY, 'x', false), null);
	assert.equal(stackIndicator(overlay, 'one', false), null);
});
test('the indicator counts members, and marks suggestions only with a question mark', () => {
	assert.equal(stackIndicator(overlay, 'lac', false), '▸ 2');
	assert.equal(stackIndicator(overlay, 'x', true), '▸ ?');
	assert.equal(stackIndicator(overlay, 'lac', true), '▸ 2');
});
test('a mashup Work cell names the primary first', () => {
	assert.equal(workCellText(overlay, 'mash'), 'Requiem + Dies irae');
	assert.equal(workCellText(overlay, 'x'), '');
});
test('filed under names the composer when known', () => {
	assert.equal(filedUnderText(overlay, 'lac'), 'filed under Requiem (Mozart)');
	assert.equal(filedUnderText(overlay, 'cil'), 'filed under Crazy in Love');
	assert.equal(filedUnderText(overlay, 'x'), '');
});
test('classical title follows Movement - Work, Catalogue', () => {
	assert.equal(classicalTitle(overlay, 'lac', 'stored'), 'Lacrimosa - Requiem, K. 626');
});
test('classical title falls back to Work, Catalogue then the stored title', () => {
	const noMovement = { ...overlay, classical: {} };
	assert.equal(classicalTitle(noMovement, 'lac', 'stored'), 'Requiem, K. 626');
	assert.equal(classicalTitle(overlay, 'x', 'stored'), 'stored');
});
test('a song work never reshapes a title or an artist', () => {
	assert.equal(classicalTitle(overlay, 'cil', 'Halo'), 'Halo');
	assert.equal(classicalArtist(overlay, 'cil', 'Beyonce'), 'Beyonce');
});
test('a remix filed under a classical work keeps its own title and artist', () => {
	assert.equal(classicalTitle(overlay, 'remix', 'Lacrimosa (Leblanc Remix)'), 'Lacrimosa (Leblanc Remix)');
	assert.equal(classicalArtist(overlay, 'remix', 'Leblanc'), 'Leblanc');
});
test('a movement equal to the work name is not repeated', () => {
	const o = { ...overlay, works: { w: work({ title: 'Lacrimosa', movement: 'Lacrimosa' }) } };
	assert.equal(classicalTitle(o, 'lac', 's'), 'Lacrimosa, K. 626');
});
test('classical artist is Composer - Performer and never empty', () => {
	assert.equal(
		classicalArtist(overlay, 'lac', 's'),
		'Mozart - Wiener Philharmoniker, Karajan'
	);
	assert.equal(classicalArtist(overlay, 'x', 'stored'), 'stored');
	assert.equal(classicalArtist(overlay, 'x', ''), '');
});
test('empty optional values stay empty, never null or undefined text', () => {
	assert.equal(composerOf(overlay, 'x'), '');
	assert.equal(performerOf(overlay, 'x'), '');
});
test('search matches work titles and aliases, not unrelated tracks', () => {
	assert.deepEqual([...tracksViaWork(overlay, 'requiem m')].sort(), ['lac', 'mash', 'remix']);
	assert.equal(tracksViaWork(overlay, 'nothing').size, 0);
	assert.equal(tracksViaWork(overlay, '  ').size, 0);
});
test('with the layout on, Title sorts by what is displayed', () => {
	const row = { stable_id: 'lac', title: 'Zzz', artist: 'Zzz' };
	assert.equal(worksSortValue(overlay, true, row, 'title'), 'lacrimosa - requiem, k. 626');
	assert.equal(worksSortValue(overlay, false, row, 'title'), undefined);
});
test('optional columns sort, and a missing value is null so it sorts last', () => {
	const row = { stable_id: 'lac' };
	assert.equal(worksSortValue(overlay, false, row, 'label'), 'DG');
	assert.equal(worksSortValue(overlay, false, row, 'year'), 1962);
	assert.equal(worksSortValue(overlay, false, { stable_id: 'x' }, 'label'), null);
	assert.equal(worksSortValue(overlay, false, row, 'bpm'), undefined);
});
test('optional columns keep table order and refuse a non-list', () => {
	assert.deepEqual(normalizeOptionalColumns(['year', 'work', 'nope']), ['work', 'year']);
	assert.throws(() => normalizeOptionalColumns('work'));
});
