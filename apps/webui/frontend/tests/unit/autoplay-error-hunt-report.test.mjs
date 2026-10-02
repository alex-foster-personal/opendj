import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { fileURLToPath } from 'node:url';

import { loadRuneModule } from './load-rune-module.mjs';
import { loadTypeScriptModule } from './load-typescript.mjs';

const {
	allowlistedSignatures,
	buildHuntReport,
	formatReport,
	groupBySignature,
	loadAllowlist,
	NEGATIVE_CONTROL_NEEDLE,
	normalizeSignature,
	unexpectedSignatures
} = await loadTypeScriptModule('tests/e2e/support/autoplay-error-hunt-report.ts');

function event(overrides = {}) {
	return {
		ts: overrides.ts ?? '2026-09-11T02:00:00.000Z',
		kind: overrides.kind ?? 'pageerror',
		message: overrides.message ?? 'boom',
		action: overrides.action ?? 'crossfader',
		route: overrides.route ?? '/performance',
		url: overrides.url,
		status: overrides.status,
		method: overrides.method
	};
}

test('xrun telemetry with different counts groups to one signature', () => {
	const grouped = groupBySignature([
		event({
			kind: 'console.error',
			message:
				'[perf-event] xrun: 2 xrun(s) in 2000ms over 691 audio callbacks; worst gap 21ms against a 16ms late-callback threshold'
		}),
		event({
			kind: 'console.error',
			message:
				'[perf-event] xrun: 1 xrun(s) in 2001ms over 686 audio callbacks; worst gap 40ms against a 14.5ms late-callback threshold'
		})
	]);
	assert.equal(grouped.length, 1);
	assert.equal(grouped[0].count, 2);
	assert.match(grouped[0].signature, /^console\.error:\[perf-event\] xrun:/);
	assert.doesNotMatch(grouped[0].signature, /\d/);
});

test('the same message with different loopback ports groups to one signature', () => {
	const grouped = groupBySignature([
		event({
			kind: 'console.error',
			message: 'failed http://127.0.0.1:8703/api/v1/tracks/deadbeefcafebabe/artwork',
			url: 'http://127.0.0.1:8703/api/v1/tracks/deadbeefcafebabe/artwork'
		}),
		event({
			kind: 'console.error',
			message: 'failed http://127.0.0.1:5326/api/v1/tracks/deadbeefcafebabe/artwork',
			url: 'http://127.0.0.1:5326/api/v1/tracks/deadbeefcafebabe/artwork',
			ts: '2026-09-11T02:00:01.000Z'
		})
	]);
	assert.equal(grouped.length, 1);
	assert.equal(grouped[0].count, 2);
	assert.match(grouped[0].signature, /^console\.error:/);
	assert.doesNotMatch(grouped[0].signature, /:\d{4}/);
	assert.doesNotMatch(grouped[0].signature, /deadbeefcafebabe/);
});

test('HTTP signatures drop the query string and keep method plus status', () => {
	const signature = normalizeSignature(
		event({
			kind: 'http-4xx',
			message: '404 GET http://127.0.0.1:8703/api/v1/tracks/abcdef12/stems?etag=1',
			url: 'http://127.0.0.1:8703/api/v1/tracks/abcdef12/stems?etag=1',
			status: 404,
			method: 'GET'
		})
	);
	assert.equal(signature, 'http-404:GET:/api/v1/tracks/<hash>/stems');
});

test('toast signatures are toast-error:<message>', () => {
	assert.equal(
		normalizeSignature(event({ kind: 'toast-error', message: 'deck 1 failed to decode' })),
		'toast-error:deck 1 failed to decode'
	);
});

test('allowlist match is exact on the normalized signature', () => {
	const grouped = groupBySignature([
		event({
			kind: 'http-4xx',
			message: '404',
			url: 'http://127.0.0.1:8703/api/v1/tracks/abcdef12/stems',
			status: 404,
			method: 'GET'
		})
	]);
	const allowlist = [{ signature: 'http-404:GET:/api/v1/tracks/<hash>/stems', issue: '#1853' }];
	assert.deepEqual(unexpectedSignatures(grouped, allowlist), []);
	assert.equal(allowlistedSignatures(grouped, allowlist).length, 1);
	assert.equal(
		unexpectedSignatures(grouped, [{ signature: 'http-404:GET:/api/v1/stems', issue: '#1853' }]).length,
		1
	);
});

test('an allowlist entry without an issue number throws', () => {
	const dir = mkdtempSync(join(tmpdir(), 'autoplay-error-hunt-allow-'));
	const path = join(dir, 'allow.json');
	writeFileSync(path, JSON.stringify({ entries: [{ signature: 'pageerror:boom' }] }), 'utf-8');
	assert.throws(() => loadAllowlist(path), /mute button/);
});

test('an allowlist entry that mutes the negative control throws', () => {
	const dir = mkdtempSync(join(tmpdir(), 'autoplay-error-hunt-allow-'));
	const path = join(dir, 'allow.json');
	writeFileSync(
		path,
		JSON.stringify({
			entries: [{ signature: `pageerror:${NEGATIVE_CONTROL_NEEDLE}`, issue: '#1' }]
		}),
		'utf-8'
	);
	assert.throws(() => loadAllowlist(path), /negative-control/);
});

test('unexpected is empty iff every grouped signature is allowlisted', () => {
	const grouped = groupBySignature([
		event({ kind: 'stall', message: 'autoplay-idle' }),
		event({ kind: 'pageerror', message: 'boom' })
	]);
	const allowlist = [
		{ signature: 'stall:autoplay-idle', issue: '#1' },
		{ signature: 'pageerror:boom', issue: '#2' }
	];
	assert.deepEqual(unexpectedSignatures(grouped, allowlist), []);
	assert.equal(unexpectedSignatures(grouped, allowlist.slice(0, 1)).length, 1);
});

test('stall and silence kinds are findings when not allowlisted', () => {
	const grouped = groupBySignature([
		event({ kind: 'stall', message: 'autoplay-banner' }),
		event({ kind: 'silence', message: 'deck-1-frozen' })
	]);
	const unexpected = unexpectedSignatures(grouped, []);
	assert.equal(unexpected.length, 2);
	assert.deepEqual(
		unexpected.map((finding) => finding.signature).sort(),
		['silence:deck-1-frozen', 'stall:autoplay-banner']
	);
});

test('the negative-control message is a distinct signature', () => {
	const grouped = groupBySignature([
		event({ kind: 'pageerror', message: 'autoplay-error-hunt negative control' }),
		event({
			kind: 'unhandledrejection',
			message: 'Error: autoplay-error-hunt negative control rejection'
		}),
		event({ kind: 'pageerror', message: 'some other boom' })
	]);
	assert.equal(grouped.length, 3);
	assert.ok(grouped.some((finding) => finding.signature.includes(NEGATIVE_CONTROL_NEEDLE)));
	assert.ok(grouped.some((finding) => finding.signature.includes('negative control rejection')));
	assert.ok(grouped.some((finding) => finding.signature === 'pageerror:some other boom'));
});

test('b91c8ba9b84b autoplay arm failure surfaces in console and hunt report', async () => {
	const stallEntry = [
		"export { noteAutoPlaySilentIdle, clearAutoPlayStall } from '$lib/rb/autoplay-stall.svelte';",
		"export { readPerfEvents } from '$lib/rb/perf-event-log';"
	].join('\n');
	const stallMod = await loadRuneModule(stallEntry);
	const calls = { error: [] };
	const realError = console.error;
	console.error = (...args) => {
		calls.error.push(args.join(' '));
	};
	try {
		stallMod.clearAutoPlayStall();
		stallMod.noteAutoPlaySilentIdle({ source_stable_id: 'src-b91c8ba9b84b', blocked: [] });
	} finally {
		console.error = realError;
		stallMod.clearAutoPlayStall();
	}
	const message = calls.error.find((line) => line.startsWith('[autoplay]'));
	assert.ok(message, `expected production [autoplay] console.error, saw: ${calls.error.join(' | ')}`);
	assert.match(message, /no-deck-playing after idle timeout/);
	const perfRows = stallMod.readPerfEvents().filter((row) => row.kind === 'autoplay-stall');
	assert.ok(perfRows.length >= 1, 'autoplay-stall perf row must carry the same diagnostic for engine.log');
	assert.match(perfRows.at(-1).message, /\[autoplay\] arm failed/);
	const ts = perfRows.at(-1).t;
	const events = [
		event({ ts, kind: 'console.error', message, action: 'autoplay-arm' }),
		event({ ts, kind: 'stall', message: 'autoplay-idle', action: 'autoplay-arm' })
	];
	const grouped = groupBySignature(events);
	assert.equal(grouped.length, 2);
	for (const finding of grouped) {
		assert.equal(finding.first_seen_at, ts);
		assert.ok(finding.examples.message.length > 0);
	}
	const report = buildHuntReport({
		minutesRequested: 2,
		seed: 4083,
		startedAt: ts,
		endedAt: '2026-09-02T16:58:14.435Z',
		unknownReason: null,
		events,
		allowlist: [],
		actionsRan: ['autoplay-arm']
	});
	assert.equal(report.status, 'FAIL');
	assert.equal(report.unexpected.length, 2);
	assert.match(formatReport(report.findings), /console\.error:/);
	assert.match(formatReport(report.findings), /stall:autoplay-idle/);
});

test('buildHuntReport FAILs on unexpected findings and PASSes when allowlisted', () => {
	const events = [event({ kind: 'stall', message: 'autoplay-idle' })];
	const failed = buildHuntReport({
		minutesRequested: 2,
		seed: 1853,
		startedAt: '2026-09-11T02:00:00.000Z',
		endedAt: '2026-09-11T02:02:00.000Z',
		unknownReason: null,
		events,
		allowlist: [],
		actionsRan: ['crossfader']
	});
	assert.equal(failed.status, 'FAIL');
	assert.equal(failed.unexpected.length, 1);
	const passed = buildHuntReport({
		minutesRequested: 2,
		seed: 1853,
		startedAt: '2026-09-11T02:00:00.000Z',
		endedAt: '2026-09-11T02:02:00.000Z',
		unknownReason: null,
		events,
		allowlist: [{ signature: 'stall:autoplay-idle', issue: '#1853' }],
		actionsRan: ['crossfader']
	});
	assert.equal(passed.status, 'PASS');
	assert.equal(passed.unexpected.length, 0);
	assert.match(formatReport(failed.findings), /stall:autoplay-idle/);
});

test('the committed allowlist does not mute Chromium 401 console.error (#1876)', () => {
	const path = fileURLToPath(new URL('../e2e/autoplay-error-hunt.allow.json', import.meta.url));
	const entries = loadAllowlist(path);
	assert.equal(
		entries.some((entry) => entry.issue === '#1876'),
		false,
		'#1876 is burned down: signed-out GET /auth/me is HTTP 200 so Chromium has no 401 to log'
	);
	assert.equal(
		entries.some(
			(entry) =>
				entry.signature ===
				'console.error:Failed to load resource: the server responded with a status of 401 (Unauthorized)'
		),
		false
	);
});
