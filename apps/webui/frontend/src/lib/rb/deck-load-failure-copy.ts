/**
 * Plain deck-load headlines. A track title may contain " - " (Camelot keys,
 * "7A - 6 - Glasswing"), so this module never splits or cuts on that separator.
 * The raw code, row id and path stay in the toast message and copy detail.
 */

/** Plain words for a library load refusal, keyed on the backend's detail.code. */
export const LIBRARY_LOAD_FAILURE_WORDS: ReadonlyMap<string, string> = new Map([
	['TRACK_NOT_FOUND', 'this track is no longer in the library'],
	['AUDIO_FILE_MISSING', "this track's audio file is missing on this machine"],
	['CLOUD_ASSET_UNAVAILABLE', "this track's audio is not on this machine and could not be fetched"],
	['CLOUD_POLICY_UNCONFIGURED', 'cloud audio is not set up on this machine, so this track cannot be fetched'],
	['AUDIO_ACCESS_BLOCKED', "this track's file did not open in time - its drive or folder is not answering"]
]);

function sentenceFor(code: string, words: string): string {
	return `${words.charAt(0).toUpperCase()}${words.slice(1)}.`;
}

/** A path the backend already named with `at '...'` or `at "..."`. */
export function lookedAtPath(text: string): string | null {
	const match = / at '([^']+)'/.exec(text) ?? / at "([^"]+)"/.exec(text);
	return match?.[1] ?? null;
}

/**
 * The first known library code in `text`. Matches `CODE: ` at the start or
 * after a colon-space, so a title like "7A - 6 - Glasswing" is not read as a code.
 */
export function libraryFailureCodeInText(text: string): string | undefined {
	for (const code of LIBRARY_LOAD_FAILURE_WORDS.keys()) {
		const pattern = new RegExp(`(?:^|: )${code}: `);
		if (pattern.test(text)) return code;
	}
	return undefined;
}

/** Title sitting before `: CODE: `, with a leading `Deck N load failed - ` removed. */
export function trackTitleBeforeCode(text: string, code: string): string | null {
	const marker = `: ${code}: `;
	const at = text.indexOf(marker);
	if (at === -1) return null;
	const title = text
		.slice(0, at)
		.replace(/^Deck \d load failed - /i, '')
		.trim();
	return title.length > 0 ? title : null;
}

/**
 * One plain sentence for `code`. Cloud audio that names a path says where
 * this Mac looked, which is the sentence a DJ can act on.
 */
export function plainLibraryLoadSentence(code: string, detail: string): string | null {
	const words = LIBRARY_LOAD_FAILURE_WORDS.get(code);
	if (words === undefined) return null;
	if (code === 'CLOUD_ASSET_UNAVAILABLE') {
		const path = lookedAtPath(detail);
		if (path !== null) return `Its audio isn't on this Mac (looked at ${path}).`;
	}
	return sentenceFor(code, words);
}

/** Headline for one coded library failure. `deck` null leaves the deck unnamed. */
export function deckLoadPlainHeadline(
	deck: number | null,
	title: string | null,
	code: string,
	detail: string
): string | null {
	const sentence = plainLibraryLoadSentence(code, detail);
	if (sentence === null) return null;
	const who = deck === null ? '' : `Deck ${deck}: `;
	if (title !== null && title !== '') return `${who}couldn't load "${title}". ${sentence}`;
	return `${who}${sentence}`;
}
