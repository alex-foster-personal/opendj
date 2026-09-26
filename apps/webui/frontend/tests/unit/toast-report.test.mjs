/**
 * The copyable toast report: its id, its client detection, its payload and its
 * clipboard write.
 *
 * Regression lines:
 * - if two toasts in one session share an id then the id cannot identify which
 *   raising a report is about, which is the whole reason it is printed
 * - if the id drops its session token then it restarts at 1 every page load and
 *   matches every Nth toast of every other session in the log directory
 * - if a Chrome user agent reports as Safari then the report sends whoever
 *   reads it to reproduce the bug on a browser that was never involved
 * - if the packaged app reports as Safari then the app-vs-browser question the
 *   client field exists to answer is answered wrongly, because the Tauri
 *   webview's user agent says Safari
 * - if an unrecognized user agent is bucketed into a known family then the
 *   report states a confident falsehood instead of an honest unknown
 * - if the payload loses the id, the timestamp or any environment field then
 *   the copy is missing something that was explicitly asked for
 * - if the payload grows a field beyond machine/user/client/page then a report
 *   pasted into an issue carries something nobody agreed to publish
 * - if a missing clipboard API resolves instead of throwing then a copy that
 *   did nothing is indistinguishable from one that worked
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let report;

before(async () => {
	report = await loadTypeScriptModule('src/lib/toast-report.ts');
});

//-----------------------------------------------------------------------------
// the id
//-----------------------------------------------------------------------------

test('an id names one raising: prefix, session token, sequence', () => {
	const id = report.formatToastId('k3f9a2', 7);
	assert.equal(id, 't-k3f9a2-7');
	assert.ok(id.startsWith(`${report.TOAST_ID_PREFIX}-`), 'the prefix is what makes it greppable');
});

test('the session token is what stops ids colliding across page loads', () => {
	// The id this replaced was a bare counter, so `3` matched every third toast
	// of every session in a week of logs.
	const a = report.formatToastId('aaaaaa', 3);
	const b = report.formatToastId('bbbbbb', 3);
	assert.notEqual(a, b, 'the same sequence in two sessions must not produce the same id');
});

test('two tokens minted in one process differ', () => {
	const tokens = new Set();
	for (let i = 0; i < 200; i += 1) tokens.add(report.newToastSessionToken());
	assert.ok(tokens.size > 190, `expected near-unique tokens, got ${tokens.size} distinct of 200`);
});

test('a token is minted even where crypto is absent, rather than throwing', () => {
	const token = report.newToastSessionToken(undefined);
	assert.equal(typeof token, 'string');
	assert.ok(token.length > 0);
});

test('a sequence that is not a positive integer is refused, not silently coerced', () => {
	assert.throws(() => report.formatToastId('abc', 0), RangeError);
	assert.throws(() => report.formatToastId('abc', -1), RangeError);
	assert.throws(() => report.formatToastId('abc', 1.5), RangeError);
	assert.throws(() => report.formatToastId('', 1));
});

//-----------------------------------------------------------------------------
// which client
//-----------------------------------------------------------------------------

const SAFARI =
	'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15';
const CHROME =
	'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36';
const FIREFOX = 'Mozilla/5.0 (Macintosh; Intel Mac OS X 14.6; rv:130.0) Gecko/20100101 Firefox/130.0';
const EDGE =
	'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36 Edg/128.0.0.0';

test('Safari is named with its version', () => {
	const client = report.describeToastClient({ userAgent: SAFARI });
	assert.equal(client.name, 'Safari');
	assert.equal(client.version, '18.1');
});

test('Chrome is not reported as Safari, though its user agent says Safari too', () => {
	const client = report.describeToastClient({ userAgent: CHROME });
	assert.equal(
		client.name,
		'Chrome',
		'every Chrome user agent contains the token Safari; matching on that is the trap'
	);
	assert.equal(client.version, '128.0.0.0');
});

test('Edge is not reported as Chrome, though its user agent says Chrome too', () => {
	assert.equal(report.describeToastClient({ userAgent: EDGE }).name, 'Edge');
});

test('Firefox is named with its version', () => {
	const client = report.describeToastClient({ userAgent: FIREFOX });
	assert.equal(client.name, 'Firefox');
	assert.equal(client.version, '130.0');
});

test('the packaged app wins over the user agent, which is the whole point', () => {
	// The Tauri shell renders in a WebKit webview whose user agent says Safari.
	// Reporting Safari here would answer "app or browser" backwards.
	const client = report.describeToastClient({
		shellStamp: { app_version: '0.4.1', git_sha: 'abc1234' },
		userAgent: SAFARI
	});
	assert.equal(client.name, 'Open DJ desktop app');
	assert.equal(client.version, '0.4.1');
});

test('a shell with no app version falls back to its sha, not to a blank', () => {
	const client = report.describeToastClient({
		shellStamp: { app_version: null, git_sha: 'abc1234' },
		userAgent: SAFARI
	});
	assert.equal(client.version, 'abc1234');
});

test('an unrecognized agent reports itself rather than guessing a family', () => {
	const client = report.describeToastClient({ userAgent: 'SomeKiosk/3 (embedded)' });
	assert.equal(client.name, 'SomeKiosk/3 (embedded)');
	assert.equal(client.version, report.UNKNOWN);
});

test('no user agent at all is an honest unknown', () => {
	assert.deepEqual(report.describeToastClient({ userAgent: null }), {
		name: report.UNKNOWN,
		version: report.UNKNOWN
	});
});

//-----------------------------------------------------------------------------
// the payload
//-----------------------------------------------------------------------------

const INPUT = {
	id: 't-k3f9a2-7',
	kind: 'error',
	headline: 'Deck 2 could not be set as master',
	message: 'Deck 2 could not be set as master',
	clientEventId: 'client-uuid-1',
	errorId: 'eid-abc',
	sentryEventId: 'sentry-xyz',
	createdAt: '2026-08-31T14:50:06.001Z',
	env: {
		machine: 'maintainer-macbook-air',
		user: 'maintainer',
		client: { name: 'Safari', version: '18.1' },
		url: 'http://127.0.0.1:8585/performance'
	}
};

test('the payload carries everything that was asked for', () => {
	const text = report.buildToastReport(INPUT);
	assert.match(text, /^id: t-k3f9a2-7$/m, 'the id is the reason the rest is findable');
	assert.match(text, /^client_event_id: client-uuid-1$/m);
	assert.match(text, /^error_id: eid-abc$/m);
	assert.match(text, /^sentry_event_id: sentry-xyz$/m);
	assert.match(text, /^when: 2026-08-31T14:50:06\.001Z$/m);
	assert.match(text, /^headline: Deck 2 could not be set as master$/m);
	assert.match(text, /^message: Deck 2 could not be set as master$/m);
	assert.match(text, /^machine: maintainer-macbook-air$/m);
	assert.match(text, /^user: maintainer$/m);
	assert.match(text, /^client: Safari 18\.1$/m, 'client AND version, per the ask');
	assert.match(text, /^kind: error$/m);
});

test('the id leads the payload and is repeated as a search instruction', () => {
	const text = report.buildToastReport(INPUT);
	assert.ok(text.startsWith('id: t-k3f9a2-7'), 'a reader skimming a paste must hit the id first');
	assert.match(text, /find in logs: search t-k3f9a2-7/);
});

test('the payload carries nothing beyond the closed list', () => {
	const keys = report
		.buildToastReport(INPUT)
		.split('\n')
		.map((line) => line.slice(0, line.indexOf(':')));
	assert.deepEqual(keys, [
		'id',
		'client_event_id',
		'error_id',
		'sentry_event_id',
		'when',
		'kind',
		'headline',
		'message',
		'machine',
		'user',
		'client',
		'page',
		'find in logs'
	]);
});

test('extras append after page and before find-in-logs', () => {
	const text = report.buildToastReport({
		...INPUT,
		extras: { ctx_source: 'toast', deck_1: 'stable=none bpm=? sync=off' }
	});
	const lines = text.split('\n');
	const pageIdx = lines.findIndex((line) => line.startsWith('page:'));
	const findIdx = lines.findIndex((line) => line.startsWith('find in logs:'));
	assert.ok(pageIdx >= 0 && findIdx > pageIdx);
	assert.equal(lines[pageIdx + 1], 'ctx_source: toast');
	assert.equal(lines[pageIdx + 2], 'deck_1: stable=none bpm=? sync=off');
	assert.equal(lines[findIdx], `find in logs: search ${INPUT.id}`);
});

//-----------------------------------------------------------------------------
// the clipboard
//-----------------------------------------------------------------------------

test('a written report reaches the clipboard verbatim', async () => {
	const written = [];
	await report.writeToastReport('body', { writeText: async (t) => void written.push(t) }, true);
	assert.deepEqual(written, ['body']);
});

test('an absent clipboard API throws rather than resolving silently', async () => {
	await assert.rejects(
		() => report.writeToastReport('body', undefined, true),
		(err) => {
			assert.equal(err.name, 'ClipboardUnavailableError');
			return true;
		},
		'a silent no-op is the failure this replaces: the user only finds out when they paste'
	);
});

test('an insecure origin says so, and names the fix', async () => {
	await assert.rejects(
		() => report.writeToastReport('body', undefined, false),
		(err) => {
			assert.match(err.message, /secure context/);
			assert.match(err.message, /localhost or 127\.0\.0\.1/, 'the reader must learn what to do');
			return true;
		}
	);
});

test('a clipboard that rejects surfaces the reason, not a swallowed catch', async () => {
	await assert.rejects(
		() =>
			report.writeToastReport(
				'body',
				{
					writeText: async () => {
						throw new Error('NotAllowedError: no user gesture');
					}
				},
				true
			),
		(err) => {
			assert.equal(err.name, 'ClipboardUnavailableError');
			assert.match(err.message, /no user gesture/);
			return true;
		}
	);
});
