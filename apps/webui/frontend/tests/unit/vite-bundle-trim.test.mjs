// The two build-time trims in vite.config.ts: the root layout's static closure
// emitted as two boot chunks (vite-layout-shell-chunk.ts) and codec-parser's unused
// parsers stubbed out (vite-codec-parser-trim.ts).
//
// Regression lines:
// - if a module reached only through a dynamic import is named into a boot
//   chunk then lazy code became boot weight
// - if a boot-closure module is named into the layout shell, or the two
//   chunks are merged, then layout modules evaluate before SvelteKit starts
// - if an entry module or route node 0 is named then SvelteKit's entry files
//   change shape
// - if anything is named for a graph with no client layout node then the
//   server build is being re-chunked
// - if the FLAC parser resolves to a stub then stem decode is broken
// - if non-fLaC bytes can reach the FLAC decoder then the Ogg stub is live
// - if a stubbed parser can be constructed without throwing then a wrong
//   parser fails silently
// - if @wasm-audio-decoders/flac starts selecting a codec other than flac or
//   ogg then the "these parsers cannot run" argument no longer holds
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import {
	BOOT_RUNTIME_CHUNK,
	LAYOUT_SHELL_CHUNK,
	bootChunkByModuleId,
	bootManualChunks
} from '../../vite-layout-shell-chunk.ts';
import {
	unusedCodecParserStubId,
	unusedCodecParserStubSource
} from '../../vite-codec-parser-trim.ts';
import { isFlacContainer } from '../../src/lib/player/decode/flac-header.ts';

const FRONTEND = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

const ENTRY = '/kit/src/runtime/client/entry.js';
const APP = '/app/.svelte-kit/generated/client-optimized/app.js';
const NODE_0 = '/app/.svelte-kit/generated/client-optimized/nodes/0.js';
const NODE_5 = '/app/.svelte-kit/generated/client-optimized/nodes/5.js';

// static edges only, as Rollup's `importedIds` reports them
const GRAPH = {
	[ENTRY]: ['/svelte/runtime.js'],
	[APP]: ['/app/src/hooks.client.ts'],
	'/app/src/hooks.client.ts': ['/svelte/runtime.js', '/app/src/lib/error-report.ts'],
	'/app/src/lib/error-report.ts': [],
	'/svelte/runtime.js': [],
	[NODE_0]: ['/app/src/routes/+layout.svelte', '/svelte/runtime.js'],
	'/app/src/routes/+layout.svelte': [
		'/app/src/lib/engine.ts',
		'/app/src/lib/shared.ts',
		'/app/src/lib/error-report.ts'
	],
	'/app/src/lib/engine.ts': ['/app/src/lib/deep.ts'],
	'/app/src/lib/deep.ts': ['/app/src/lib/engine.ts'],
	'/app/src/lib/shared.ts': [],
	[NODE_5]: ['/app/src/routes/admin/+page.svelte'],
	'/app/src/routes/admin/+page.svelte': ['/app/src/lib/shared.ts', '/app/src/lib/admin-only.ts'],
	'/app/src/lib/admin-only.ts': [],
	// reached from the layout only through import(), so absent from importedIds
	'/app/src/lib/midi-engine.ts': ['/app/src/lib/shared.ts']
};
const importedIdsOf = (id) => GRAPH[id] ?? [];
const SHELL = [
	'/app/src/lib/deep.ts',
	'/app/src/lib/engine.ts',
	'/app/src/lib/shared.ts',
	'/app/src/routes/+layout.svelte'
];
const BOOT = ['/app/src/hooks.client.ts', '/app/src/lib/error-report.ts', '/svelte/runtime.js'];

function membersOf(chunkByModuleId, chunkName) {
	return [...chunkByModuleId].filter(([, name]) => name === chunkName).map(([id]) => id).sort();
}

test('boot-runtime is the entries static closure; layout-shell is node 0 closure minus it', () => {
	const chunks = bootChunkByModuleId(Object.keys(GRAPH), importedIdsOf);
	assert.deepEqual(membersOf(chunks, BOOT_RUNTIME_CHUNK), BOOT);
	assert.deepEqual(membersOf(chunks, LAYOUT_SHELL_CHUNK), SHELL);
});

test('lazy-only modules, the entry modules and route nodes are never named', () => {
	const chunks = bootChunkByModuleId(Object.keys(GRAPH), importedIdsOf);
	for (const outside of [
		'/app/src/lib/midi-engine.ts',
		'/app/src/lib/admin-only.ts',
		'/app/src/routes/admin/+page.svelte',
		NODE_0,
		NODE_5,
		ENTRY,
		APP
	]) {
		assert.equal(chunks.get(outside), undefined, `${outside} must be left to Rollup`);
	}
});

test('boot-runtime imports nothing outside itself and layout-shell only boot-runtime', () => {
	const chunks = bootChunkByModuleId(Object.keys(GRAPH), importedIdsOf);
	for (const [member, chunkName] of chunks) {
		for (const dependency of importedIdsOf(member)) {
			const dependencyChunk = chunks.get(dependency);
			if (chunkName === BOOT_RUNTIME_CHUNK) {
				assert.equal(dependencyChunk, BOOT_RUNTIME_CHUNK, `${member} -> ${dependency} leaves boot-runtime`);
			} else {
				assert.notEqual(dependencyChunk, undefined, `${member} -> ${dependency} leaves both boot chunks`);
			}
		}
	}
});

test('a graph with no client layout node (the server build) names nothing', () => {
	const serverIds = ['/app/.svelte-kit/generated/server/internal.js', '/app/src/routes/+layout.svelte'];
	assert.equal(bootChunkByModuleId(serverIds, importedIdsOf).size, 0);
});

test('a client graph missing a boot entry fails loudly instead of guessing', () => {
	const withoutApp = Object.keys(GRAPH).filter((id) => id !== APP);
	assert.throws(() => bootChunkByModuleId(withoutApp, importedIdsOf), /found 1 boot entries/);
});

test('manualChunks names boot members and leaves everything else to Rollup', () => {
	const manualChunks = bootManualChunks();
	let closureReads = 0;
	const meta = {
		getModuleIds: () => {
			closureReads += 1;
			return Object.keys(GRAPH)[Symbol.iterator]();
		},
		getModuleInfo: (id) => (id in GRAPH ? { importedIds: GRAPH[id] } : null)
	};
	assert.equal(manualChunks('/app/src/lib/engine.ts', meta), LAYOUT_SHELL_CHUNK);
	assert.equal(manualChunks('/app/src/lib/shared.ts', meta), LAYOUT_SHELL_CHUNK);
	assert.equal(manualChunks('/svelte/runtime.js', meta), BOOT_RUNTIME_CHUNK);
	assert.equal(manualChunks('/app/src/lib/midi-engine.ts', meta), undefined);
	assert.equal(manualChunks(NODE_0, meta), undefined);
	assert.equal(manualChunks(APP, meta), undefined);
	assert.equal(closureReads, 1, 'the closures are computed once per module graph');
});

// ------------------------------------------------------- codec-parser ---

const flacEntry = fileURLToPath(import.meta.resolve('@wasm-audio-decoders/flac'));
const codecParserRoot = dirname(createRequire(flacEntry).resolve('codec-parser'));

function parserImports(relativePath) {
	const importer = join(codecParserRoot, relativePath);
	const source = readFileSync(importer, 'utf8');
	const specifiers = [...source.matchAll(/from\s+"([^"]+Parser\.js)"/g)].map((m) => m[1]);
	return { importer, specifiers };
}

test('the installed codec-parser imports exactly the parsers this trim reasons about', () => {
	const entry = parserImports('src/CodecParser.js');
	const ogg = parserImports('src/containers/ogg/OggParser.js');
	assert.deepEqual(entry.specifiers.sort(), [
		'./codecs/aac/AACParser.js',
		'./codecs/flac/FLACParser.js',
		'./codecs/mpeg/MPEGParser.js',
		'./containers/ogg/OggParser.js'
	]);
	assert.deepEqual(ogg.specifiers.sort(), [
		'../../codecs/Parser.js',
		'../../codecs/flac/FLACParser.js',
		'../../codecs/opus/OpusParser.js',
		'../../codecs/vorbis/VorbisParser.js'
	]);
});

test('MPEG, AAC and Ogg parsers resolve to stubs; the FLAC parser does not', () => {
	const entry = parserImports('src/CodecParser.js');
	const resolved = Object.fromEntries(
		entry.specifiers.map((specifier) => [specifier, unusedCodecParserStubId(specifier, entry.importer)])
	);
	assert.deepEqual(resolved, {
		'./codecs/aac/AACParser.js': '\0codec-parser-unused:AACParser',
		'./codecs/mpeg/MPEGParser.js': '\0codec-parser-unused:MPEGParser',
		'./containers/ogg/OggParser.js': '\0codec-parser-unused:OggParser',
		'./codecs/flac/FLACParser.js': null
	});
});

test('only imports made by the codec-parser entry are stubbed', () => {
	const ogg = parserImports('src/containers/ogg/OggParser.js');
	for (const specifier of ogg.specifiers) {
		assert.equal(unusedCodecParserStubId(specifier, ogg.importer), null);
	}
	assert.equal(unusedCodecParserStubId('./codecs/mpeg/MPEGParser.js', '/app/src/lib/CodecParser.js'), null);
	assert.equal(unusedCodecParserStubId('./codecs/mpeg/MPEGParser.js', undefined), null);
});

test('the app gate that keeps Ogg away from the FLAC decoder: only fLaC magic passes', () => {
	const bytesOf = (text) => new TextEncoder().encode(text).buffer;
	assert.equal(isFlacContainer(bytesOf('fLaC\0\0\0\x22')), true);
	assert.equal(isFlacContainer(bytesOf('OggS\0\x02\0\0')), false);
	assert.equal(isFlacContainer(bytesOf('ID3\x04\0\0\0\0')), false);
	const decode = readFileSync(join(FRONTEND, 'src/lib/player/decode/flac-stem-decode.ts'), 'utf8');
	assert.match(decode, /if \(isFlacContainer\(bytes\)\) return 'flac';/);
	assert.match(decode, /if \(barred !== null\) return \{ buffer: await fallback\(bytes\), refusal: barred \};/);
});

test('a stubbed parser throws by name when constructed', async () => {
	const source = unusedCodecParserStubSource('\0codec-parser-unused:OggParser');
	const stub = await import(`data:text/javascript,${encodeURIComponent(source)}`);
	assert.equal(stub.default.name, 'OggParser');
	assert.throws(() => new stub.default(), /codec-parser OggParser is not bundled/);
	assert.equal(unusedCodecParserStubSource('/app/src/lib/x.ts'), null);
});

test('the FLAC decoder still selects only flac or ogg and rejects every non-FLAC codec', () => {
	const decoder = readFileSync(join(dirname(flacEntry), 'src/FLACDecoder.js'), 'utf8');
	const selected = [...decoder.matchAll(/codec \+= "([a-z]+)"/g)].map((m) => m[1]).sort();
	assert.deepEqual(selected, ['flac', 'ogg']);
	assert.match(decoder, /new CodecParser\(codec,/);
	assert.match(decoder, /if \(codec !== "flac"\)\s*throw new Error/);
});

test('vite.config.ts wires both trims and the terser option', () => {
	const config = readFileSync(join(FRONTEND, 'vite.config.ts'), 'utf8');
	assert.match(config, /plugins: \[[^\]]*codecParserTrimPlugin\(\)[^\]]*\]/);
	assert.match(config, /output: \{ manualChunks: bootManualChunks\(\) \}/);
	assert.match(config, /terserOptions: \{ safari10: false \}/);
});
