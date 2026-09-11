import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let wheelLayout;

const RESPONSE = {
	schema_version: 1,
	axis: 'play_count',
	axes: [],
	selected_axis_enabled: true,
	selected_axis_reason: null,
	total_tracks: 3,
	unclassified_track_count: 0,
	families: [
		{
			name: 'techno',
			color: '#5ec8ff',
			track_count: 2,
			genres: [
				{
					tag: 'Peak Time Techno',
					track_count: 2,
					tracks: [
						{
							stable_id: 't-1',
							title: 'One',
							artist: 'A',
							genre: 'Peak Time Techno',
							axis_value: 40,
							axis_title: '40 plays'
						},
						{
							stable_id: 't-2',
							title: 'Two',
							artist: 'B',
							genre: 'Peak Time Techno',
							axis_value: 10,
							axis_title: '10 plays'
						}
					]
				}
			]
		},
		{
			name: 'house',
			color: '#ffb454',
			track_count: 1,
			genres: [
				{
					tag: 'Jackin House',
					track_count: 1,
					tracks: [
						{
							stable_id: 't-3',
							title: 'Three',
							artist: 'C',
							genre: 'Jackin House',
							axis_value: null,
							axis_title: null
						}
					]
				}
			]
		}
	]
};

before(async () => {
	wheelLayout = await loadTypeScriptModule('src/routes/library-wheel/wheel-layout.ts');
});

test('buildWheelLayout splits the full circle proportional to real track_count', () => {
	const layout = wheelLayout.buildWheelLayout(RESPONSE);

	assert.equal(layout.families.length, 2);
	const techno = layout.families.find((f) => f.title.startsWith('techno'));
	const house = layout.families.find((f) => f.title.startsWith('house'));

	// techno holds 2 of 3 tracks -> 2/3 of the circle; house holds 1/3.
	assert.ok(Math.abs(techno.endAngle - techno.startAngle - (2 / 3) * 2 * Math.PI) < 1e-9);
	assert.ok(Math.abs(house.endAngle - house.startAngle - (1 / 3) * 2 * Math.PI) < 1e-9);
	assert.equal(techno.startAngle, 0);
	assert.ok(Math.abs(house.endAngle - 2 * Math.PI) < 1e-9);
});

test('buildWheelLayout emits one track wedge per real track, never a demo node', () => {
	const layout = wheelLayout.buildWheelLayout(RESPONSE);
	const stableIds = layout.tracks.map((t) => t.stableId).sort();
	assert.deepEqual(stableIds, ['t-1', 't-2', 't-3']);
});

test('a track with a real axis_value renders past the band floor, proportional to the axis max', () => {
	const layout = wheelLayout.buildWheelLayout(RESPONSE);
	const hi = layout.tracks.find((t) => t.stableId === 't-1'); // 40 plays, max in this payload
	const lo = layout.tracks.find((t) => t.stableId === 't-2'); // 10 plays

	assert.ok(hi.outerRadius > lo.outerRadius);
	assert.ok(Math.abs(hi.outerRadius - 1.0) < 1e-9); // top of the max value sits at the band ceiling
});

test('a null axis_value (disabled axis) renders at the band floor, never a fabricated size', () => {
	const layout = wheelLayout.buildWheelLayout(RESPONSE);
	const noAxis = layout.tracks.find((t) => t.stableId === 't-3');
	assert.equal(noAxis.outerRadius, noAxis.innerRadius);
});

test('wedgePath returns an empty string for a zero-span wedge instead of a degenerate arc', () => {
	const zeroSpan = {
		stableId: null,
		title: 'empty',
		color: '#fff',
		startAngle: 1,
		endAngle: 1,
		innerRadius: 0,
		outerRadius: 1,
		track: null
	};
	assert.equal(wheelLayout.wedgePath(zeroSpan), '');
});

test('wedgePath produces a well-formed SVG path string for a real wedge', () => {
	const wedge = {
		stableId: null,
		title: 'quarter',
		color: '#fff',
		startAngle: 0,
		endAngle: Math.PI / 2,
		innerRadius: 0.2,
		outerRadius: 0.5,
		track: null
	};
	const d = wheelLayout.wedgePath(wedge);
	assert.match(d, /^M /);
	assert.match(d, /Z$/);
});
