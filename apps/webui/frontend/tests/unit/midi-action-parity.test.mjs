// midi-action-parity.test.mjs -- the Rust engine installs any map whose action
// types the page can emit (#10276). On Fri 6 Nov 2026 a corrected XDJ-AZ map
// installed stripped to 119 of 154 controls because the engine refused the
// page-only types. This test reads both sources and fails when a type is added
// to one side only, and when the parity fixture the engine test installs
// misses a page type.
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';

const TYPES_TS = new URL('../../src/lib/rb/midi/midi-types.ts', import.meta.url);
const PARSER_RS = new URL('../../../../audio-engine/src/midi.rs', import.meta.url);
const FIXTURE = new URL(
	'../../../../audio-engine/tests/fixtures/midi/all-page-actions.json',
	import.meta.url
);

// Types the engine parses that the page cannot emit yet, each with the issue
// that adds the page side. An entry the page now emits fails the test so the
// list cannot go stale.
const ENGINE_AHEAD = new Map([]);

function _pageActionTypes() {
	const src = readFileSync(TYPES_TS, 'utf8');
	return new Set([...src.matchAll(/^\ttype: '([a-z_]+)';$/gm)].map((m) => m[1]));
}

function _engineActionTypes() {
	const src = readFileSync(PARSER_RS, 'utf8');
	const parserStart = src.indexOf('fn action_of(');
	const parserEnd = src.indexOf('\nimpl DeviceMap', parserStart);
	assert.ok(
		parserStart > 0 && parserEnd > parserStart,
		'midi.rs: fn action_of followed by impl DeviceMap'
	);
	const parser = src.slice(parserStart, parserEnd);
	// Match arms on a snake_case literal are action types; the single-word
	// mixer targets (trim, eq, fader, filter) are not.
	const driven = [...parser.matchAll(/^\s*"([a-z]+(?:_[a-z]+)+)" =>/gm)].map((m) => m[1]);
	const pageStart = src.indexOf('const PAGE_ACTIONS: &[&str] = &[');
	const pageEnd = src.indexOf('];', pageStart);
	assert.ok(pageStart > 0 && pageEnd > pageStart, 'midi.rs: const PAGE_ACTIONS list');
	const pageOnly = [...src.slice(pageStart, pageEnd).matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
	return {
		driven: new Set(driven),
		pageOnly: new Set(pageOnly),
		all: new Set([...driven, ...pageOnly])
	};
}

test('every page action type is parsed by the engine, driven or page-only', () => {
	const page = _pageActionTypes();
	const engine = _engineActionTypes();
	assert.ok(page.size >= 27, `midi-types.ts declares ${page.size} action types`);
	const missing = [...page].filter((t) => !engine.all.has(t));
	assert.deepEqual(
		missing,
		[],
		'page types the engine would refuse: add them to PAGE_ACTIONS or the parser'
	);
	const overlap = [...engine.driven].filter((t) => engine.pageOnly.has(t));
	assert.deepEqual(overlap, [], 'a type is either driven by the engine or page-only, not both');
});

test('every engine action type is one the page can emit, or is listed as engine-ahead', () => {
	const page = _pageActionTypes();
	const engine = _engineActionTypes();
	const extra = [...engine.all].filter((t) => !page.has(t) && !ENGINE_AHEAD.has(t));
	assert.deepEqual(
		extra,
		[],
		'engine types the page never emits: add the page side or list the issue in ENGINE_AHEAD'
	);
	const stale = [...ENGINE_AHEAD.keys()].filter((t) => page.has(t));
	assert.deepEqual(stale, [], 'the page emits these now: remove them from ENGINE_AHEAD');
	for (const [t, issue] of ENGINE_AHEAD) {
		assert.ok(
			engine.all.has(t),
			`${t} (${issue}) is listed as engine-ahead but the engine does not parse it`
		);
	}
});

test('the engine parity fixture carries every page action type exactly once', () => {
	const page = _pageActionTypes();
	const fixture = JSON.parse(readFileSync(FIXTURE, 'utf8'));
	const counts = new Map();
	for (const b of fixture.bindings) counts.set(b.action.type, (counts.get(b.action.type) ?? 0) + 1);
	const missing = [...page].filter((t) => !counts.has(t));
	assert.deepEqual(missing, [], 'page types the fixture never installs');
	const unknown = [...counts.keys()].filter((t) => !page.has(t));
	assert.deepEqual(unknown, [], 'fixture types the page cannot emit');
	// mixer_channel appears once per target (trim, eq, filter); everything else once.
	const repeated = [...counts].filter(([t, n]) => n > 1 && t !== 'mixer_channel').map(([t]) => t);
	assert.deepEqual(repeated, [], 'a type bound more than once');
	assert.equal(counts.get('mixer_channel'), 3, 'mixer_channel covers trim, eq and filter');
});
