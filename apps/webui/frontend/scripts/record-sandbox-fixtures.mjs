#!/usr/bin/env node
// Record real daemon responses into src/sandbox/recorded/ for Storybook
// contracts. Fixtures are CAPTURED, not hand-typed: point this at a daemon
// serving a fixture library (see `library` in src/sandbox/recordings.json),
// and it writes one JSON file per entry with the status, content type and
// body the daemon actually returned.
//
//   node scripts/record-sandbox-fixtures.mjs --base http://127.0.0.1:8767 \
//     --manifest /abs/lyrics-words-manifest.json [--only name,...]
//
// --manifest is the JSON the builder wrote with its own --manifest flag. The
// recorder refuses to write unless every recording targets a track the
// manifest names, each of those tracks answered 2xx at least once, and the
// words response carries the manifest's first line: a daemon serving any
// other library fails those checks instead of overwriting the captures. Each
// written file records the sha256 of the manifest and of the builder source,
// and its own sha256 is pinned in src/sandbox/recorded-checksums.json.
//
// Fails loudly on an unreachable daemon, a body that is not JSON where the
// content type says it is, or a GET/PUT/POST answered with anything but JSON
// (a representation the replay cannot reproduce); only HEAD, which has no
// body, records `body: null`. It never writes a partial set silently.
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, renameSync, rmSync, writeFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

const ROOT = fileURLToPath(new URL('../src/sandbox/', import.meta.url));
const FRONTEND = fileURLToPath(new URL('../', import.meta.url));
const TRACK_ID = /\/tracks\/([0-9a-f]{40})\//;

/** Throws unless the captured responses came from the library `manifest`
 * describes. Checks presence, not absence: every captured route must have
 * answered 2xx, and each lyrics route must carry the manifest's own content
 * (the words track's first line and word, line and verdict counts, the
 * wordless track's empty words, and its line cache's lines and source). One 2xx per track is not enough: a 200 audio
 * probe beside 404 lyrics routes would otherwise record the errors. */
export function checkProvenance({ manifest, captured }) {
	for (const key of ['words_stable_id', 'wordless_stable_id', 'words_first_line', 'line_cache_first_line', 'line_cache_source']) {
		if (typeof manifest?.[key] !== 'string' || manifest[key] === '') {
			throw new Error(`manifest has no ${key}; pass the JSON the fixture builder wrote with --manifest`);
		}
	}
	for (const key of ['words_n_words', 'words_n_lines']) {
		if (!Number.isInteger(manifest[key])) throw new Error(`manifest has no ${key}; pass the JSON the fixture builder wrote with --manifest`);
	}
	if (!Array.isArray(manifest.line_cache_lines)) throw new Error('manifest has no line_cache_lines; pass the JSON the fixture builder wrote with --manifest');
	const ids = new Set([manifest.words_stable_id, manifest.wordless_stable_id]);
	let firstLineChecked = false;
	for (const out of captured) {
		const id = TRACK_ID.exec(out.request.path)?.[1];
		if (!ids.has(id)) throw new Error(`${out.name}: ${out.request.path} targets no track the manifest names`);
		const { status, body } = out.response;
		if (status < 200 || status >= 300) {
			throw new Error(`${out.name}: ${out.request.method} ${out.request.path} answered ${status}, not 2xx; the daemon is not serving this library`);
		}
		const route = out.request.path.slice(out.request.path.indexOf(id) + id.length);
		if (route === '/lyrics/words' && id === manifest.words_stable_id && Array.isArray(body?.lines)) {
			const got = body.lines[0]?.text;
			if (got !== manifest.words_first_line) {
				throw new Error(`${out.name}: first line ${JSON.stringify(got)} is not the manifest's ${JSON.stringify(manifest.words_first_line)}`);
			}
			// The whole payload, not just its first line: a truncated words or
			// lines array, or a verdict counting other data, is not this library.
			const counts = {
				words: [body.words?.length, manifest.words_n_words],
				lines: [body.lines.length, manifest.words_n_lines],
				'verdict.n_words': [body.verdict?.n_words, manifest.words_n_words],
				'verdict.n_lines': [body.verdict?.n_lines, manifest.words_n_lines]
			};
			for (const [what, [have, want]] of Object.entries(counts)) {
				if (have !== want) throw new Error(`${out.name}: ${what} is ${JSON.stringify(have)}, the manifest's is ${JSON.stringify(want)}`);
			}
			if (body.verdict?.verdict !== 'vocal') {
				throw new Error(`${out.name}: verdict ${JSON.stringify(body.verdict?.verdict)} is not "vocal" for the manifest's words track`);
			}
			firstLineChecked = true;
		}
		if (route === '/lyrics/words' && id === manifest.wordless_stable_id) {
			if (!Array.isArray(body?.words) || body.words.length !== 0) {
				throw new Error(`${out.name}: the wordless track answered ${JSON.stringify(body?.words)} for words; the manifest's wordless track has none`);
			}
		}
		if (route === '/lyrics' && id === manifest.wordless_stable_id) {
			const got = body?.lines?.[0]?.text;
			if (got !== manifest.line_cache_first_line) {
				throw new Error(`${out.name}: first cached line ${JSON.stringify(got)} is not the manifest's ${JSON.stringify(manifest.line_cache_first_line)}`);
			}
			if (JSON.stringify(body.lines) !== JSON.stringify(manifest.line_cache_lines)) {
				throw new Error(`${out.name}: cached lines ${JSON.stringify(body.lines)} are not the manifest's ${JSON.stringify(manifest.line_cache_lines)}`);
			}
			if (body.source !== manifest.line_cache_source) {
				throw new Error(`${out.name}: source ${JSON.stringify(body.source)} is not the manifest's ${JSON.stringify(manifest.line_cache_source)}`);
			}
		}
	}
	const wantsLines = captured.some((out) => out.request.path.endsWith('/lyrics/words') && TRACK_ID.exec(out.request.path)?.[1] === manifest.words_stable_id);
	if (wantsLines && !firstLineChecked) throw new Error('the words track answered without lines, so its first line could not be checked');
}

/** `path` with each `{key}` replaced by the manifest's value, so a fresh
 * build of the fixture library (whose stable ids differ) is recorded at its
 * own track ids. Throws on a key the manifest does not carry. */
export function resolvePath(path, manifest) {
	return path.replace(/\{(\w+)\}/g, (_, key) => {
		const value = manifest?.[key];
		if (typeof value !== 'string' || value === '') throw new Error(`${path}: manifest has no ${key}`);
		return value;
	});
}

const sha256 = (path) => createHash('sha256').update(readFileSync(path)).digest('hex');

/** The body to record for one response, or a thrown Error when the response
 * is a representation the sandbox cannot replay. HEAD has no body, so it
 * records null with its content type; anything else must be JSON, because
 * recording null there would replay an empty body as if the daemon sent one. */
export function recordedBody({ name, method, contentType, text }) {
	if (method === 'HEAD') return null;
	if (!contentType?.includes('application/json')) {
		throw new Error(
			`${name}: ${method} answered content-type ${JSON.stringify(contentType)}, not application/json; ` +
				'the sandbox records JSON bodies only (HEAD is the one bodiless exception)'
		);
	}
	return JSON.parse(text);
}

/** A recording name is a file stem under recorded/, never a path. */
export const RECORDING_NAME = /^[a-z0-9]+(?:-[a-z0-9]+)*$/;

/** Writes the captures, their pins and the manifest copy under `root`, all or
 * nothing as far as a failure can be: every output is serialized and staged
 * beside its target first, and only once all of them are on disk is each
 * renamed into place. A name that is not a plain stem is refused before
 * anything is written, so a recordings.json entry cannot reach outside
 * recorded/. */
export function writeRecordedSet({ root, captured, manifestPath, listed }) {
	for (const out of captured) {
		if (typeof out.name !== 'string' || !RECORDING_NAME.test(out.name)) {
			throw new Error(`recording name ${JSON.stringify(out.name)} is not a plain lowercase stem (${RECORDING_NAME}); refusing to write`);
		}
	}
	const pinsPath = `${root}recorded-checksums.json`;
	const pins = JSON.parse(readFileSync(pinsPath, 'utf8'));
	// A pin recordings.json no longer lists goes, and its file with it, so a
	// renamed or removed recording cannot stay a valid replay.
	const dropped = Object.keys(pins.files).filter((file) => !listed.includes(file.replace(/\.json$/, '')));
	for (const file of dropped) delete pins.files[file];
	const outputs = [];
	for (const out of captured) {
		const bytes = `${JSON.stringify(out, null, '\t')}\n`;
		outputs.push([`${root}recorded/${out.name}.json`, bytes]);
		pins.files[`${out.name}.json`] = { sha256: createHash('sha256').update(bytes).digest('hex') };
	}
	outputs.push([pinsPath, `${JSON.stringify(pins, null, '\t')}\n`]);
	// The manifest itself, byte for byte, so a test can re-check every
	// capture's manifest_sha256 against the file it names.
	outputs.push([`${root}recorded-manifest.json`, readFileSync(manifestPath)]);
	const staged = [];
	try {
		for (const [path, bytes] of outputs) {
			const tmp = `${path}.staged-${process.pid}`;
			staged.push(tmp);
			writeFileSync(tmp, bytes);
		}
	} catch (error) {
		for (const tmp of staged) rmSync(tmp, { force: true });
		throw error;
	}
	outputs.forEach(([path], i) => renameSync(staged[i], path));
	for (const file of dropped) rmSync(`${root}recorded/${file}`, { force: true });
}

async function main() {
	function _arg(name) {
		const at = process.argv.indexOf(name);
		if (at === -1) return undefined;
		const value = process.argv[at + 1];
		// A flag with no value (last argument, or followed by another flag)
		// is an error, never "absent": --only alone would record every fixture.
		if (value === undefined || value.startsWith('--') || value === '') {
			console.error(`${name} needs a value`);
			process.exit(2);
		}
		return value;
	}

	const base = _arg('--base');
	const manifestPath = _arg('--manifest');
	const only = _arg('--only')?.split(',');
	if (base === undefined || manifestPath === undefined) {
		console.error('usage: record-sandbox-fixtures.mjs --base http://127.0.0.1:<port> --manifest <builder manifest.json> [--only a,b]');
		process.exit(2);
	}
	const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
	const { recordings } = JSON.parse(readFileSync(`${ROOT}recordings.json`, 'utf8'));
	const badNames = recordings.filter((r) => typeof r.name !== 'string' || !RECORDING_NAME.test(r.name));
	if (badNames.length > 0) {
		console.error(`recordings.json names ${badNames.map((r) => JSON.stringify(r.name)).join(', ')}, not plain lowercase stems (${RECORDING_NAME})`);
		process.exit(2);
	}
	// Every requested name must exist: a misspelling beside a valid name would
	// otherwise record a silent subset and exit 0.
	const unmatched = (only ?? []).filter((name) => !recordings.some((r) => r.name === name));
	if (unmatched.length > 0) {
		console.error(`no recording named ${unmatched.join(', ')} (--only ${only})`);
		process.exit(2);
	}
	const chosen = recordings.filter((r) => only === undefined || only.includes(r.name));
	if (chosen.length === 0) {
		console.error(`no recording matched --only ${only}`);
		process.exit(2);
	}
	const libraries = [...new Set(chosen.map((r) => r.library))];
	if (libraries.length !== 1) {
		console.error(`recordings name ${libraries.length} fixture builders (${libraries.join(', ')}); record one builder's set per --manifest`);
		process.exit(2);
	}
	const provenance = { manifest_sha256: sha256(manifestPath), library_sha256: sha256(`${FRONTEND}${libraries[0]}`) };
	// The builder stamps its own digest into the manifest when it builds the
	// data dir. A manifest from an older builder (or none) means the daemon is
	// serving data this checkout's builder did not make, so the captures would
	// be credited to the wrong builder.
	if (manifest.builder_sha256 !== provenance.library_sha256) {
		console.error(
			`manifest builder_sha256 ${JSON.stringify(manifest.builder_sha256)} is not ${libraries[0]} as checked out (${provenance.library_sha256}); rebuild the fixture data dir with this builder, then record`
		);
		process.exit(2);
	}
	// --only rewrites some captures and keeps the rest, then replaces the
	// shared manifest: that is coherent only when the kept captures already
	// name this manifest (and, for the same builder, this builder). Otherwise
	// the set would mix provenances, so re-record every capture instead.
	if (only !== undefined) {
		const stale = recordings
			.filter((r) => !only.includes(r.name))
			.filter((r) => {
				const kept = JSON.parse(readFileSync(`${ROOT}recorded/${r.name}.json`, 'utf8')).provenance;
				if (kept?.manifest_sha256 !== provenance.manifest_sha256) return true;
				return r.library === libraries[0] && kept.library_sha256 !== provenance.library_sha256;
			})
			.map((r) => r.name);
		if (stale.length > 0) {
			console.error(`--only would leave ${stale.join(', ')} recorded against another manifest or builder; re-record every capture`);
			process.exit(2);
		}
	}
	mkdirSync(`${ROOT}recorded`, { recursive: true });

	// Capture every entry before writing any, so a failure leaves the set untouched.
	const captured = [];
	for (const rec of chosen) {
		const path = resolvePath(rec.path, manifest);
		const url = new URL(path, base);
		for (const [key, value] of Object.entries(rec.query ?? {})) url.searchParams.set(key, value);
		const response = await fetch(url, { method: rec.method });
		const contentType = response.headers.get('content-type');
		const text = rec.method === 'HEAD' ? '' : await response.text();
		const body = recordedBody({
			name: rec.name,
			method: rec.method,
			contentType,
			text
		});
		const out = {
			name: rec.name,
			recorded_at: new Date().toISOString(),
			library: rec.library,
			provenance,
			request: { method: rec.method, path, query: rec.query ?? {} },
			response: { status: response.status, content_type: contentType, body }
		};
		captured.push(out);
		console.log(`${rec.name}: ${rec.method} ${path} -> ${response.status}`);
	}
	try {
		checkProvenance({ manifest, captured });
	} catch (error) {
		console.error(`refusing to write: ${error.message}`);
		process.exit(1);
	}
	try {
		writeRecordedSet({ root: ROOT, captured, manifestPath, listed: recordings.map((r) => r.name) });
	} catch (error) {
		console.error(error.message);
		process.exit(1);
	}
}

if (process.argv[1] === fileURLToPath(import.meta.url)) await main();
