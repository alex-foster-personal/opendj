import assert from 'node:assert/strict';
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';

/**
 * Play from USB, lane D (specs/usb-play-from-stick.md 4b "Frontend guards"):
 * a stick id must never produce a request to /api/v1/tracks/{usb-...}.
 *
 * Source scan over every src file: each place that builds a /api/v1/tracks/
 * URL from an id (a template literal interpolating into the path, a string
 * concatenation onto it, or a typed client call with a path parameter) must
 * either go through `trackApiPath` (which never produces that prefix for a
 * stick id, so it never matches here) or sit in a function that checks THAT
 * id BEFORE the URL is built (`isUsbTrackId(id`, or the
 * `refuseStickRead(id`/`refuseStickWrite(id` guards). A function is any
 * `function` declaration, arrow-function binding or method; the guard only
 * counts from the nearest such header before the site, and only when its
 * first argument is the same variable the URL interpolates. The few
 * library-only sites a stick id cannot reach are listed below with the
 * reason.
 *
 * Regression lines:
 * - if a new per-track builder interpolates an id into /api/v1/tracks/ with no usb guard then this scan fails and names file:line and function
 * - if the scanner stops finding the known guarded sites then it has gone blind (positive control fails)
 * - if the scanner stops flagging an unguarded synthetic builder then it can no longer fail (negative control fails)
 * - if an arrow-function or method builder after a guarded function borrows that function's guard then an unguarded builder passes
 * - if a string-concatenated /api/v1/tracks/ URL is not a site then an unguarded builder passes
 * - if a guard on a different variable counts as guarding the URL's id then an unguarded builder passes
 */

const SRC = fileURLToPath(new URL('../../src', import.meta.url));

/** Not scanned: the generated schema (paths only, no requests) and the one
 * helper that owns the routing decision. */
const NOT_SCANNED = new Set(['lib/api-types.ts', 'lib/rb/track-source.ts']);

/** file#function -> why a stick id cannot reach it. */
const LIBRARY_ONLY_SITES = new Map([
	[
		'routes/dedup/dedup-api.ts#dedupArtworkUrl',
		'duplicate groups are built from library rows only; a stick id is never a dedup candidate'
	]
]);

const ID_EXPRESSION = /^\s*(?:encodeURIComponent\(\s*)?([A-Za-z_$][\w$.]*)/;

/** Each pattern's group 1 is where the id expression starts. */
const ID_URL_BUILDERS = [
	// `/api/v1/tracks/${...}` or `${BASE}/api/v1/tracks/${...}`
	{ pattern: /`(?:\$\{[A-Za-z_$][\w$.]*\})?\/api\/v1\/tracks\/\$\{([^}]*)\}/g, idOf: _idOfExpression },
	// '/api/v1/tracks/' + id, `${BASE}/api/v1/tracks/` + encodeURIComponent(id)
	{ pattern: /\/api\/v1\/tracks\/['"`]\s*\+([^;\n]*)/g, idOf: _idOfExpression },
	// api.GET('/api/v1/tracks/{stable_id}...', { params: { path: { stable_id } } }) and siblings
	{ pattern: /\bapi\.(?:GET|POST|PUT|PATCH|DELETE)\(\s*'\/api\/v1\/tracks\/\{([\w$]+)\}/g, idOf: _idOfPathParam }
];

const FUNCTION_HEADERS = [
	// function name(
	/(?:^|\n)[\t ]*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([\w$]+)/g,
	// const name = (...) => / const name = async id =>
	/(?:^|\n)[\t ]*(?:export\s+)?(?:const|let|var)\s+([\w$]+)\s*(?::[^=\n]*)?=\s*(?:async\s*)?(?:\([^)]*\)|[\w$]+)\s*(?::[^=]*?)?=>/g,
	// name: (...) => inside an object literal
	/(?:^|\n)[\t ]*([\w$]+)\s*:\s*(?:async\s*)?\([^)]*\)\s*(?::[^=]*?)?=>/g,
	// name(...) { as a class or object method (control keywords excluded below)
	/(?:^|\n)[\t ]*(?:(?:public|private|protected|static|async|get|set)\s+)*([\w$]+)\s*\([^)]*\)\s*(?::[^{;\n]*)?\{/g
];
const NOT_A_FUNCTION = new Set(['if', 'for', 'while', 'switch', 'catch', 'with', 'return', 'function']);

const GUARD_NAMES = '(?:isUsbTrackId|refuseStickRead|refuseStickWrite)';

//-----------------------------------------------------------------------------
// _helpers
//-----------------------------------------------------------------------------

/** Blanks comments while keeping every newline, so reported line numbers
 * stay true. A `//` right after ':' or a quote is part of a URL, not a comment. */
function withoutComments(text) {
	const blank = (match) => match.replace(/[^\n]/g, ' ');
	return text
		.replace(/\/\*[\s\S]*?\*\//g, blank)
		.replace(/<!--[\s\S]*?-->/g, blank)
		.replace(/(^|[^:'"`\\])(\/\/[^\n]*)/g, (_, lead, comment) => lead + blank(comment));
}

function _idOfExpression(match) {
	return ID_EXPRESSION.exec(match[1])?.[1] ?? null;
}

/** The variable a typed client call passes for `{name}`: `path: { name }` or
 * `path: { name: variable }` within the call. */
function _idOfPathParam(match, code) {
	const name = match[1];
	const call = code.slice(match.index, match.index + 600);
	const param = new RegExp(`path:\\s*\\{[^}]*?\\b${name}\\b(?:\\s*:\\s*([A-Za-z_$][\\w$.]*))?`).exec(call);
	if (param === null) return null;
	return param[1] ?? name;
}

function _escaped(text) {
	return text.replace(/[.$]/g, '\\$&');
}

function enclosingFunction(code, index) {
	let found = { name: '(module scope)', start: 0 };
	for (const pattern of FUNCTION_HEADERS) {
		for (const header of code.matchAll(pattern)) {
			if (header.index >= index) break;
			if (NOT_A_FUNCTION.has(header[1]) || header.index < found.start) continue;
			found = { name: header[1], start: header.index };
		}
	}
	return found;
}

/** Every id-built /api/v1/tracks/ site in one file, with whether its
 * enclosing function guards that same id before the URL. An id the scan
 * cannot name (a computed expression) is never counted as guarded. */
function trackUrlSites(file, text) {
	const code = withoutComments(text);
	const sites = [];
	for (const { pattern, idOf } of ID_URL_BUILDERS) {
		for (const match of code.matchAll(pattern)) {
			const fn = enclosingFunction(code, match.index);
			const id = idOf(match, code);
			const guard = id === null ? null : new RegExp(`\\b${GUARD_NAMES}\\(\\s*${_escaped(id)}\\s*[,)]`);
			sites.push({
				site: `${file}#${fn.name}`,
				line: code.slice(0, match.index).split('\n').length,
				guarded: guard !== null && guard.test(code.slice(fn.start, match.index))
			});
		}
	}
	return sites;
}

function* sourceFiles(dir) {
	for (const entry of readdirSync(dir, { withFileTypes: true })) {
		const path = join(dir, entry.name);
		if (entry.isDirectory()) yield* sourceFiles(path);
		else if (/\.(ts|js|svelte)$/.test(entry.name)) yield path;
	}
}

function scanSrc() {
	const sites = [];
	for (const path of sourceFiles(SRC)) {
		const file = relative(SRC, path);
		if (NOT_SCANNED.has(file)) continue;
		sites.push(...trackUrlSites(file, readFileSync(path, 'utf8')));
	}
	return sites;
}

//-----------------------------------------------------------------------------
// the scan
//-----------------------------------------------------------------------------

test('[if] any src file builds a /api/v1/tracks/ URL from an id without a usb guard [then] this names it', () => {
	const violations = scanSrc()
		.filter(({ site, guarded }) => !guarded && !LIBRARY_ONLY_SITES.has(site))
		.map(({ site, line }) => `${site} (line ${line})`);
	assert.deepEqual(
		violations,
		[],
		'route the id through trackApiPath (src/lib/rb/track-source.ts), or refuse a stick id (refuseStickRead / refuseStickWrite / isUsbTrackId) before building the URL'
	);
});

test('[if] a library-only allowance names a site that no longer exists [then] the allowance is stale and fails', () => {
	const found = new Set(scanSrc().map(({ site }) => site));
	for (const site of LIBRARY_ONLY_SITES.keys()) assert.ok(found.has(site), `stale allowance: ${site}`);
});

//-----------------------------------------------------------------------------
// controls
//-----------------------------------------------------------------------------

test('positive control: the scan finds the known guarded builders, so a clean result is not a blind one', () => {
	const guarded = new Set(scanSrc().filter((s) => s.guarded).map((s) => s.site));
	for (const site of [
		'lib/rb/api-rb.ts#fetchRbMeta',
		'lib/rb/api-rb.ts#saveHotCue',
		'lib/rb/api-rb.ts#clearHotCue',
		'lib/rb/api-rb.ts#restoreHotCue',
		'lib/rb/api-rb.ts#fetchTrackLyrics',
		'lib/rb/api-rb.ts#probeStemArtifact',
		'lib/rb/api-rb.ts#stemAudioUrl',
		'lib/rb/api-track-reveal.ts#revealTrack',
		'lib/components/rb/wave/stem-waveform-cache.svelte.ts#fetchStemWaveform',
		'lib/api.ts#getTrack',
		'lib/api.ts#patchTrack',
		'lib/api-karaoke.ts#getTrackLyricsWords',
		'lib/api-karaoke.ts#putLyricOverride',
		'lib/rb/auto-cues-api.ts#fetchAutoCues',
		'lib/rb/beatgrid-fallback-api.ts#fetchBeatgridFallback',
		'lib/rb/track-playlists.ts#listTrackPlaylists',
		'lib/rb/track-library.ts#removeFromLibrary'
	]) {
		assert.ok(guarded.has(site), `the scan did not find guarded site ${site}`);
	}
});

test('negative control: an unguarded synthetic builder is flagged, a guarded one and a comment are not', () => {
	const unguarded = trackUrlSites(
		'synthetic.ts',
		[
			'export function stickAudio(id: string): string {',
			'\treturn `${API_BASE}/api/v1/tracks/${encodeURIComponent(id)}/audio`;',
			'}',
			'export async function stickCues(stable_id: string) {',
			"\treturn api.GET(\n\t\t'/api/v1/tracks/{stable_id}/auto-cues', { params: { path: { stable_id } } });",
			'}'
		].join('\n')
	);
	assert.deepEqual(
		unguarded.map(({ site, line, guarded }) => ({ site, line, guarded })),
		[
			{ site: 'synthetic.ts#stickAudio', line: 2, guarded: false },
			{ site: 'synthetic.ts#stickCues', line: 5, guarded: false }
		]
	);

	const guardedLater = trackUrlSites(
		'synthetic.ts',
		[
			'export function first(id: string): string {',
			"\trefuseStickRead(id, 'x');",
			'\treturn "ok";',
			'}',
			'export function second(id: string): string {',
			'\treturn `/api/v1/tracks/${id}/anlz`;',
			'}'
		].join('\n')
	);
	assert.deepEqual(
		guardedLater.map(({ site, guarded }) => ({ site, guarded })),
		[{ site: 'synthetic.ts#second', guarded: false }],
		'a guard in a DIFFERENT function must not count'
	);

	const clean = trackUrlSites(
		'synthetic.ts',
		[
			'/** GET `/api/v1/tracks/${id}/audio` in a doc comment. */',
			'// `/api/v1/tracks/${id}` in a line comment',
			'export function routed(id: string): string {',
			"\treturn `${API_BASE}${trackApiPath(id, '/audio')}`;",
			'}'
		].join('\n')
	);
	assert.deepEqual(clean, [], 'comments and trackApiPath callers are not sites');
});

test('negative control: builders the first scan could not see are flagged (review of lane D, three blind spots)', () => {
	const guardedFunctionThen = (builder) =>
		['export function listTrackPlaylists(id: string) {', "\trefuseStickRead(id, 'x');", '\treturn 1;', '}', ...builder].join('\n');
	const cases = {
		// (1) an arrow or method after a guarded function must not borrow its guard
		'arrow binding': guardedFunctionThen([
			'export const arrowProbe = (id: string): string => `/api/v1/tracks/${encodeURIComponent(id)}/audio`;'
		]),
		'object method': guardedFunctionThen(['const o = {', '\tbuild(id: string) {', '\t\treturn `/api/v1/tracks/${id}`;', '\t}', '};']),
		'object arrow property': guardedFunctionThen(['const o = {', '\tbuild: (id: string) => `/api/v1/tracks/${id}`', '};']),
		// (2) string concatenation is a builder too
		'quoted concatenation': ['export function a(id: string) {', "\treturn '/api/v1/tracks/' + encodeURIComponent(id);", '}'].join('\n'),
		'template concatenation': ['export function a(id: string) {', "\treturn `${API_BASE}/api/v1/tracks/` + id + '/audio';", '}'].join('\n'),
		// (3) a guard on another variable does not guard this id
		'guard on another variable': [
			'export function d(id: string, other: string) {',
			'\tif (isUsbTrackId(other)) return null;',
			'\treturn `/api/v1/tracks/${id}/x`;',
			'}'
		].join('\n'),
		'typed call guarded on another variable': [
			'export async function t(stable_id: string, other: string) {',
			"\trefuseStickRead(other, 'x');",
			"\treturn api.GET('/api/v1/tracks/{stable_id}', { params: { path: { stable_id } } });",
			'}'
		].join('\n')
	};
	for (const [name, text] of Object.entries(cases)) {
		const sites = trackUrlSites('synthetic.ts', text);
		assert.equal(sites.length, 1, `${name}: expected exactly one site, got ${JSON.stringify(sites)}`);
		assert.equal(sites[0].guarded, false, `${name}: an unguarded builder was counted as guarded`);
	}

	// Control in the other direction: a typed call whose path param is bound
	// to a renamed variable is guarded when THAT variable is checked.
	const renamed = trackUrlSites(
		'synthetic.ts',
		[
			'export async function t(sid: string) {',
			"\trefuseStickRead(sid, 'x');",
			"\treturn api.GET('/api/v1/tracks/{stable_id}', { params: { path: { stable_id: sid } } });",
			'}'
		].join('\n')
	);
	assert.deepEqual(renamed.map(({ guarded }) => guarded), [true], 'a guard on the bound variable counts');
});
