import type { Plugin } from 'vite';

/**
 * Leaves the parsers codec-parser never runs here out of the production
 * bundle.
 *
 * `@wasm-audio-decoders/flac` frames its input with codec-parser, and
 * codec-parser's entry imports every parser it knows statically: MPEG, AAC
 * and the Ogg container (which brings Opus and Vorbis) ride along with FLAC,
 * about 56 KB of source before minification. None of them can run in this
 * app:
 *   - the FLAC decoder constructs the parser with "audio/flac" or
 *     "audio/ogg" only, so the MPEG and AAC branches are unreachable;
 *   - it picks "audio/ogg" only for bytes that start with `OggS`, and
 *     `flac-stem-decode.ts` hands this decoder nothing but native `fLaC`
 *     containers (`isFlacContainer`); every other part is refused before a
 *     decoder is taken and goes to `decodeAudioData`;
 *   - mp3 stems go to mpg123-decoder, which does not use codec-parser.
 *
 * Each of the three imports resolves to a stub whose constructor throws by
 * name, so a future caller that does reach one fails loudly instead of
 * mis-parsing (the decode path turns that into `decode-failed` and falls
 * back to `decodeAudioData`). The FLAC parser is untouched.
 *
 * Measured Thu 1 Oct 2026 on af--preview-mixtour-io 7e14328ea0, gzip as
 * scripts/bundle-budget.mjs weighs it: other-lazy 288,815 -> 283,192. The
 * decoder chunk is fetched only when a stemmed deck decodes, so the other two
 * surfaces do not move.
 */

const UNUSED_PARSER = /\/(?:codecs\/mpeg\/MPEG|codecs\/aac\/AAC|containers\/ogg\/Ogg)Parser\.js$/;
const CODEC_PARSER_ENTRY = /\/node_modules\/codec-parser\/src\/CodecParser\.js$/;
const STUB_PREFIX = '\0codec-parser-unused:';

/** The stub's virtual id for an import codec-parser's entry makes, or null to leave it alone. */
export function unusedCodecParserStubId(source: string, importer: string | undefined): string | null {
	if (importer === undefined || !CODEC_PARSER_ENTRY.test(importer.replace(/\\/g, '/'))) return null;
	if (!UNUSED_PARSER.test(source)) return null;
	return `${STUB_PREFIX}${source.slice(source.lastIndexOf('/') + 1, -'.js'.length)}`;
}

/** Source of the stub module behind a virtual id from `unusedCodecParserStubId`. */
export function unusedCodecParserStubSource(stubId: string): string | null {
	if (!stubId.startsWith(STUB_PREFIX)) return null;
	const parserName = stubId.slice(STUB_PREFIX.length);
	return (
		`export default class ${parserName} {\n` +
		`\tconstructor() {\n` +
		`\t\tthrow new Error(${JSON.stringify(
			`codec-parser ${parserName} is not bundled: this app only frames FLAC (see vite-codec-parser-trim.ts)`
		)});\n` +
		`\t}\n` +
		`}\n`
	);
}

export function codecParserTrimPlugin(): Plugin {
	return {
		name: 'opendj-codec-parser-trim',
		apply: 'build',
		enforce: 'pre',
		resolveId: (source, importer) => unusedCodecParserStubId(source, importer),
		load: (id) => unusedCodecParserStubSource(id)
	};
}
