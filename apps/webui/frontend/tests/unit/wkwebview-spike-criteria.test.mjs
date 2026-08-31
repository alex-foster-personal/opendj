import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

import {
	AUTOMATION_CLASSES,
	FAILURE_CLASSES,
	SPIKE_CRITERIA,
	VERDICTS,
	criterionById,
	newResultsDocument,
	recordVerdict,
	renderEvidenceMarkdown,
	rollupGate
} from '../manual/wkwebview-spike/spike-criteria.mjs';

// DESK-005 gates the packaged-decode verdict. The failure mode this file
// guards is an evidence document that reads as reassurance: an UNAVAILABLE row
// silently counted as a pass, a missing capability left unnamed, a criterion
// with no verdict rolled up as clean, or a retry overwriting the first failure.
// None of those are audio bugs; all of them would open the gate on a document
// nobody can trust.
//
// Regression lines:
// - if UNAVAILABLE ever rolls up as PASS then a run on a Mac with one output
//   device opens the gate without device evidence
// - if an unrecorded criterion rolls up as PASS then abandoning the run halfway
//   produces a green gate
// - if a second recordVerdict overwrites the first then the first failure is
//   lost, which is exactly what the evidence taxonomy forbids

const HOST = {
	commit_sha: '0000000000000000000000000000000000000000',
	macos_version: 'macOS 15.3 (24D60)',
	webkit_version: 'WebKit 620.1.16',
	hardware: 'MacBook Pro M1 Pro, 16 GB',
	audio_device: 'MacBook Pro Speakers',
	activation_mode: 'auto',
	operator: 'maintainer',
	user_agent: 'Mozilla/5.0 (Macintosh) AppleWebKit/620.1.16'
};

function _fresh() {
	return newResultsDocument(HOST);
}

function _passEverything(document_) {
	for (const criterion of SPIKE_CRITERIA) {
		recordVerdict(document_, { id: criterion.id, verdict: 'PASS', detail: 'observed' });
	}
	return document_;
}

test('criterion registry is well formed and covers the host plus AGENTS.md set', () => {
	const ids = SPIKE_CRITERIA.map((criterion) => criterion.id);
	assert.deepEqual(ids, [...new Set(ids)], 'criterion ids must be unique');
	assert.ok(ids.length >= 13, 'the registry must cover WKV-01 to WKV-13');
	for (const criterion of SPIKE_CRITERIA) {
		assert.match(criterion.id, /^WKV-\d{2}$/);
		assert.ok(criterion.title.length > 0, `${criterion.id} needs a title`);
		assert.ok(criterion.measurement.length > 0, `${criterion.id} needs a measurement`);
		assert.ok(criterion.pass_criterion.length > 0, `${criterion.id} needs a pass criterion`);
		assert.ok(
			AUTOMATION_CLASSES.includes(criterion.automation),
			`${criterion.id} automation class must be known`
		);
		assert.ok(
			FAILURE_CLASSES.includes(criterion.failure_class),
			`${criterion.id} failure class must decide something`
		);
		if (criterion.automation !== 'auto') {
			assert.ok(
				typeof criterion.human_step === 'string' && criterion.human_step.length > 0,
				`${criterion.id} is not automatable and must name the human act`
			);
		}
	}
});

test('every criterion this file claims for the repo invariants is present', () => {
	for (const id of ['WKV-10', 'WKV-11', 'WKV-12', 'WKV-13']) {
		assert.equal(criterionById(id).id, id);
	}
	assert.throws(() => criterionById('WKV-99'), /unknown spike criterion/);
});

test('host metadata is mandatory, so evidence is attributable to a commit and a machine', () => {
	assert.throws(() => newResultsDocument(null), /host metadata object/);
	for (const field of Object.keys(HOST)) {
		const partial = { ...HOST };
		delete partial[field];
		assert.throws(
			() => newResultsDocument(partial),
			new RegExp(field),
			`${field} must be required on a run document`
		);
	}
	assert.throws(() => newResultsDocument({ ...HOST, commit_sha: '   ' }), /commit_sha/);
	assert.equal(_fresh().schema, 'desk-005-wkwebview-spike/1');
});

test('a verdict must be one of the three, with a real observation attached', () => {
	assert.deepEqual(VERDICTS, ['PASS', 'FAIL', 'UNAVAILABLE']);
	const document_ = _fresh();
	assert.throws(
		() => recordVerdict(document_, { id: 'WKV-01', verdict: 'SKIP', detail: 'x' }),
		/must be one of/
	);
	assert.throws(
		() => recordVerdict(document_, { id: 'WKV-01', verdict: 'PASS', detail: '  ' }),
		/detail string/
	);
	assert.equal(document_.rows.length, 0, 'a rejected verdict must not land in the document');
});

test('UNAVAILABLE must name the missing capability', () => {
	const document_ = _fresh();
	assert.throws(
		() => recordVerdict(document_, { id: 'WKV-06', verdict: 'UNAVAILABLE', detail: 'no device' }),
		/must name the missing capability/
	);
	recordVerdict(document_, {
		id: 'WKV-06',
		verdict: 'UNAVAILABLE',
		detail: 'only one output device enumerated',
		missing_capability: 'a second selectable output device'
	});
	assert.equal(document_.rows[0].missing_capability, 'a second selectable output device');
});

test('a criterion cannot be re-recorded, so the first failure survives a retry', () => {
	const document_ = _fresh();
	recordVerdict(document_, { id: 'WKV-02', verdict: 'FAIL', detail: 'clock regressed after seek' });
	assert.throws(
		() => recordVerdict(document_, { id: 'WKV-02', verdict: 'PASS', detail: 'passed on retry' }),
		/already has a recorded verdict/
	);
	assert.equal(document_.rows.length, 1);
	assert.equal(document_.rows[0].verdict, 'FAIL');
});

test('rollup opens the gate only when every criterion passed', () => {
	const gate = rollupGate(_passEverything(_fresh()));
	assert.equal(gate.verdict, 'PASS');
	assert.equal(gate.phase_4_entry_gate, 'open');
	assert.deepEqual(gate.failed, []);
	assert.deepEqual(gate.unavailable, []);
	assert.deepEqual(gate.unrecorded, []);
});

test('an empty or partial run is INCOMPLETE with the gap named, never PASS', () => {
	const empty = rollupGate(_fresh());
	assert.equal(empty.verdict, 'INCOMPLETE');
	assert.equal(empty.phase_4_entry_gate, 'closed');
	assert.equal(empty.unrecorded.length, SPIKE_CRITERIA.length);

	const partial = _fresh();
	for (const criterion of SPIKE_CRITERIA.slice(0, 3)) {
		recordVerdict(partial, { id: criterion.id, verdict: 'PASS', detail: 'observed' });
	}
	const gate = rollupGate(partial);
	assert.equal(gate.verdict, 'INCOMPLETE');
	assert.deepEqual(gate.unrecorded, SPIKE_CRITERIA.slice(3).map((criterion) => criterion.id));
});

test('one UNAVAILABLE keeps the gate closed even when nothing failed', () => {
	const document_ = _fresh();
	for (const criterion of SPIKE_CRITERIA) {
		if (criterion.id === 'WKV-08') {
			recordVerdict(document_, {
				id: criterion.id,
				verdict: 'UNAVAILABLE',
				detail: 'machine could not be slept during the session',
				missing_capability: 'a sleep/wake cycle on the run machine'
			});
			continue;
		}
		recordVerdict(document_, { id: criterion.id, verdict: 'PASS', detail: 'observed' });
	}
	const gate = rollupGate(document_);
	assert.equal(gate.verdict, 'INCOMPLETE');
	assert.equal(gate.phase_4_entry_gate, 'closed');
	assert.deepEqual(gate.unavailable, ['WKV-08']);
	assert.deepEqual(gate.failed, []);
});

test('a failure is classified so the result decides host versus tuning', () => {
	const hostFail = _fresh();
	recordVerdict(hostFail, {
		id: 'WKV-01',
		verdict: 'FAIL',
		detail: 'stretch processor construction timed out'
	});
	const hostGate = rollupGate(hostFail);
	assert.equal(hostGate.verdict, 'FAIL');
	assert.deepEqual(hostGate.host_disqualifying, ['WKV-01']);

	const tuningFail = _fresh();
	recordVerdict(tuningFail, {
		id: 'WKV-10',
		verdict: 'FAIL',
		detail: 'BAR sync landed on beat 3 against beat 1'
	});
	const tuningGate = rollupGate(tuningFail);
	assert.equal(tuningGate.verdict, 'FAIL');
	assert.deepEqual(tuningGate.host_disqualifying, []);
});

test('rendered evidence carries the host header, every criterion row and no invented pass', () => {
	const document_ = _fresh();
	recordVerdict(document_, {
		id: 'WKV-01',
		verdict: 'FAIL',
		detail: 'timed out | with a pipe and\na newline',
		evidence:
			'apps/webui/frontend/test-results/wkwebview-spike/2026-01-01--abc1234/console.log'
	});
	const markdown = renderEvidenceMarkdown(document_);
	assert.match(markdown, /# DESK-005 WKWebView spike evidence/);
	assert.match(markdown, /desk-005-wkwebview-spike\/1/);
	for (const value of Object.values(HOST)) {
		assert.ok(markdown.includes(value), `evidence header must carry ${value}`);
	}
	for (const criterion of SPIKE_CRITERIA) {
		assert.ok(markdown.includes(criterion.id), `${criterion.id} must appear in the table`);
	}
	assert.match(markdown, /NOT RECORDED/, 'unrecorded criteria must render as unrecorded');
	assert.match(markdown, /timed out \\\| with a pipe and a newline/, 'cells must be escaped');
	assert.match(markdown, /Host-disqualifying failures/);
	assert.equal(markdown.split('\n').filter((line) => line.startsWith('| WKV-')).length, SPIKE_CRITERIA.length);
});

test('the harness and the criteria module hold no U+2014 or U+2013 characters and no synthetic audio', () => {
	const files = ['spike-criteria.mjs', 'harness.mjs', 'README.md'].map((name) =>
		fileURLToPath(new URL(`../manual/wkwebview-spike/${name}`, import.meta.url))
	);
	for (const file of files) {
		const text = readFileSync(file, 'utf8');
		assert.doesNotMatch(text, /[\u2013\u2014]/, `${file} must not contain an U+2014 or U+2013 characters`);
		assert.doesNotMatch(
			text,
			/OscillatorNode|createOscillator|createBufferSource\(/,
			`${file} must not synthesize audio; the spike uses the real library and the real graph`
		);
	}
});
