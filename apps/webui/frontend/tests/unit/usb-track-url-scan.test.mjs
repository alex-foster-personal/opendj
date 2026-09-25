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
 * URL from an id (a template literal interpolating into the path, or a typed
 * client call with a path parameter) must either go through `trackApiPath`
 * (which never produces that prefix for a stick id, so it never matches here)
 * or sit in a function that checks the id BEFORE the URL is built
 * (`isUsbTrackId(`, or the `refuseStickRead(`/`refuseStickWrite(` guards).
 * The few library-only sites a stick id cannot reach are listed below with
 * the reason.
 *
 * Regression lines:
 * - if a new per-track builder interpolates an id into /api/v1/tracks/ with no usb guard then this scan fails and names file:line and function
 * - if the scanner stops finding the known guarded sites then it has gone blind (positive control fails)
 * - if the scanner stops flagging an unguarded synthetic builder then it can no longer fail (negative control fails)
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

const ID_URL_BUILDERS = [
	// `/api/v1/tracks/${...}` or `${BASE}/api/v1/tracks/${...}`
	/`(?:\$\{[A-Za-z_$][\w$.]*\})?\/api\/v1\/tracks\/\$\{/g,
	// api.GET('/api/v1/tracks/{stable_id}...') and siblings
	/\bapi\.(?:GET|POST|PUT|PATCH|DELETE)\(\s*'\/api\/v1\/tracks\/\{/g
];

const FUNCTION_HEADER = /(?:^|\n)[\t ]*(?:export\s+)?(?:async\s+)?function\s*\*?\s*([\w$]+)/g;

const USB_GUARD = /\b(?:isUsbTrackId|refuseStickRead|refuseStickWrite)\(/;

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

function enclosingFunction(code, index) {
	let found = { name: '(module scope)', start: 0 };
	for (const header of code.matchAll(FUNCTION_HEADER)) {
		if (header.index >= index) break;
		found = { name: header[1], start: header.index };
	}
	return found;
}

/** Every id-built /api/v1/tracks/ site in one file, with whether its
 * enclosing function runs a usb guard before the URL. */
function trackUrlSites(file, text) {
	const code = withoutComments(text);
	const sites = [];
	for (const pattern of ID_URL_BUILDERS) {
		for (const match of code.matchAll(pattern)) {
			const fn = enclosingFunction(code, match.index);
			sites.push({
				site: `${file}#${fn.name}`,
				line: code.slice(0, match.index).split('\n').length,
				guarded: USB_GUARD.test(code.slice(fn.start, match.index))
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
