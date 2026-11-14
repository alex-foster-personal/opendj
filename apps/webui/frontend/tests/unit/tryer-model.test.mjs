// requirement: TRYER-14
// tryer-model.ts createAuditioner: a new job never resumes or hears the previous job's audio.
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let model;

before(async () => {
	model = await loadTypeScriptModule('src/lib/tryer/tryer-model.ts');
});

// A rerender (TryerPanel render()) calls auditioner.reset() before the new job's first play. These pin that the
// reset leaves no old playhead or old media event able to touch the new job's audition.

class FakeAudio extends EventTarget {
	constructor(url, playResult) {
		super();
		this.url = url;
		this.currentTime = 0;
		this.paused = true;
		this.playResult = playResult;
	}
	play() {
		this.paused = false;
		return this.playResult();
	}
	pause() {
		this.paused = true;
	}
}

function auditionRig(playResult = () => Promise.resolve()) {
	const made = [];
	const ended = [];
	const failures = [];
	const auditioner = model.createAuditioner(
		(url) => {
			const audio = new FakeAudio(url, playResult);
			made.push(audio);
			return audio;
		},
		{ onEnded: () => ended.push('ended'), onFailure: (id, reason) => failures.push({ id, reason }) }
	);
	return { auditioner, made, ended, failures };
}

test('[if] a new render starts while an old candidate is playing [then] the new job starts at 0 and old events are ignored, [else stop].', () => {
	const { auditioner, made, ended } = auditionRig();
	auditioner.play('c2', 'http://e/job1/c2.wav');
	made[0].currentTime = 42;
	assert.equal(auditioner.position('c2'), 42, 'control: the old candidate is mid-play');
	auditioner.reset();
	assert.equal(made[0].paused, true, 'the old render stops when the new one starts');
	assert.equal(auditioner.position('c2'), null, 'no playhead is drawn from the old job');
	made[0].dispatchEvent(new Event('ended'));
	assert.deepEqual(ended, [], "the old render's end does not stop the panel");
	auditioner.play('c2', 'http://e/job2/c2.wav');
	assert.equal(made.length, 2, 'the new job plays a fresh element');
	assert.equal(made[1].currentTime, 0, "the new job does not inherit the old candidate's playhead");
	made[1].dispatchEvent(new Event('ended'));
	assert.deepEqual(ended, ['ended'], "control: the current render's end is reported");
});

test('[if] an old audio element fails after a new candidate begins [then] no failure reaches the new job, [else stop].', async () => {
	const rejections = [];
	const { auditioner, made, failures } = auditionRig(
		() => new Promise((_resolve, reject) => rejections.push(reject))
	);
	auditioner.play('c1', 'http://e/job1/c1.wav');
	auditioner.reset();
	auditioner.play('c3', 'http://e/job2/c3.wav');
	made[0].dispatchEvent(new Event('error'));
	rejections[0](new Error('old render aborted'));
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(failures, [], 'a late error or rejection from the old element changes nothing');
	made[1].dispatchEvent(new Event('error'));
	rejections[1](new Error('new render failed'));
	await new Promise((resolve) => setImmediate(resolve));
	assert.deepEqual(
		failures.map((failure) => failure.id),
		['c3', 'c3'],
		"control: the new candidate's own error and rejection are reported"
	);
});

test('[if] a new job reuses candidate id c1 [then] position and failures follow the new element only, [else stop].', () => {
	const { auditioner, made, failures } = auditionRig();
	auditioner.play('c1', 'http://e/job1/c1.wav');
	made[0].currentTime = 30;
	auditioner.reset();
	auditioner.play('c1', 'http://e/job2/c1.wav');
	assert.equal(made.length, 2, 'same id, new job: a fresh element');
	assert.equal(made[1].url, 'http://e/job2/c1.wav');
	assert.equal(auditioner.position('c1'), 0, "c1's playhead is the new render's, not job1's 30 s");
	made[0].currentTime = 55;
	assert.equal(auditioner.position('c1'), 0, "the old element's clock never drives the new c1");
	made[0].dispatchEvent(new Event('error'));
	assert.deepEqual(failures, [], "job1's c1 failing is not job2's c1 failing");
	made[1].dispatchEvent(new Event('error'));
	assert.deepEqual(
		failures.map((failure) => failure.id),
		['c1'],
		"control: the new c1's own error is reported"
	);
});
