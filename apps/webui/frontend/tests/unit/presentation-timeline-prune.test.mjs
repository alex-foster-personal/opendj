// requirement: RBPERF-12367
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let presentation;

before(async () => {
	presentation = await loadTypeScriptModule('src/lib/player/transport/presentation.ts');
});

const baseSchedule = (revision, startContextTime, startPositionSec = revision) => ({
	revision,
	active: true,
	loop: null,
	startContextTime,
	startPositionSec,
	tempoRatio: 1
});

test('[if] acknowledged revisions precede the active segment [then] they are removed after observe, [else stop]', () => {
	const timeline = presentation.createPresentedTransportTimeline(0);
	for (let revision = 1; revision <= 3; revision += 1) {
		presentation.acknowledgePresentedTransportSchedule(timeline, baseSchedule(revision, revision));
		presentation.observePresentedTransportTimeline(
			timeline,
			{ contextTime: revision + 0.25, performanceTime: (revision + 0.25) * 1000 },
			20
		);
	}
	presentation.acknowledgePresentedTransportSchedule(timeline, baseSchedule(4, 4));
	assert.deepEqual(
		timeline.schedules.map(({ revision }) => revision),
		[3, 4],
		'only the effective current segment and the not-yet-presented future revision remain'
	);
});

test('[if] a future acknowledged revision exists [then] it stays mirrored, [else stop]', () => {
	const timeline = presentation.createPresentedTransportTimeline(0);
	presentation.acknowledgePresentedTransportSchedule(timeline, baseSchedule(1, 1));
	presentation.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 1.5, performanceTime: 1500 },
		20
	);
	presentation.acknowledgePresentedTransportSchedule(timeline, {
		...baseSchedule(2, 3),
		active: false
	});
	assert.deepEqual(
		timeline.schedules.map(({ revision }) => revision),
		[1, 2],
		'current and future acknowledged revisions coexist'
	);
});

test('[if] the active segment is the only acknowledged revision [then] one schedule remains, [else stop]', () => {
	const timeline = presentation.createPresentedTransportTimeline(0);
	presentation.acknowledgePresentedTransportSchedule(timeline, baseSchedule(1, 1));
	presentation.observePresentedTransportTimeline(
		timeline,
		{ contextTime: 1.25, performanceTime: 1250 },
		20
	);
	assert.equal(timeline.schedules.length, 1);
	assert.equal(timeline.schedules[0].revision, 1);
});

test('[if] an unacknowledged revision is present [then] superseded rows are pruned, [else stop]', () => {
	const timeline = presentation.createPresentedTransportTimeline(2);
	presentation.acknowledgePresentedTransportSchedule(timeline, baseSchedule(1, 10, 4));
	presentation.acknowledgePresentedTransportSchedule(timeline, baseSchedule(2, 10, 8));
	assert.equal(timeline.schedules.length, 1);
	assert.ok(
		timeline.schedules.every((row) => row.supersededByRevision === null),
		'superseded schedule history is not retained'
	);
});

test('[if] ten thousand revisions are presented [then] schedules stay bounded with future truth, [else stop]', () => {
	const timeline = presentation.createPresentedTransportTimeline(0);
	for (let revision = 1; revision <= 10_000; revision += 1) {
		presentation.acknowledgePresentedTransportSchedule(timeline, baseSchedule(revision, revision));
		presentation.observePresentedTransportTimeline(
			timeline,
			{ contextTime: revision + 0.25, performanceTime: (revision + 0.25) * 1000 },
			20_000
		);
		assert.ok(timeline.schedules.length <= 1, `revision ${revision} leaked schedule history`);
	}

	presentation.acknowledgePresentedTransportSchedule(timeline, {
		revision: 10_001,
		active: false,
		loop: null,
		startContextTime: 10_002,
		startPositionSec: 10_001.25,
		tempoRatio: 1
	});
	assert.deepEqual(
		timeline.schedules.map(({ revision }) => revision),
		[10_000, 10_001],
		'the effective current revision and needed future revision remain mirrored'
	);
});
