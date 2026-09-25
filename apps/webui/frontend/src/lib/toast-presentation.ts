/**
 * Human-readable toast headlines and expandable detail (issue #3882, UX-TOAST-02).
 *
 * Pure module: no stores, no network. Call sites pass raw messages; the tray
 * shows `headline` collapsed and keeps technical text in `detail`.
 */

export const TOAST_SOLUTION_URL_PLACEHOLDER = 'https://docs.opendj.app/errors/tbd';

export interface ToastPresentation {
	/** Short, human, shown collapsed. */
	headline: string;
	/** Technical / verbose; shown expanded only. */
	detail?: string | undefined;
	/** Error-only hint with placeholder solutions URL. */
	solutionHint?: string | undefined;
	/** Feature area for bare exception mapping. */
	feature?: string | undefined;
	/** Original message for perf rows and logs. */
	rawMessage: string;
}

const BARE_EXCEPTION_NAMES = new Set([
	'Error',
	'RangeError',
	'TypeError',
	'ReferenceError',
	'SyntaxError',
	'DOMException'
]);

function inferFeature(message: string, feature?: string): string | undefined {
	if (feature !== undefined && feature !== '') return feature;
	if (/^Deck \d+/i.test(message)) return 'Deck audio';
	if (/beat sync|beat-sync|BAR fold/i.test(message)) return 'Beat Sync';
	if (/autoplay/i.test(message)) return 'AutoPlay';
	if (/presentation clock|output clock/i.test(message)) return 'Audio output';
	if (/cloudsync|cloud sync/i.test(message)) return 'CloudSync';
	if (/stem/i.test(message)) return 'Stems';
	if (/load failed|unload/i.test(message)) return 'Deck load';
	return undefined;
}

function isBareExceptionText(text: string): boolean {
	const trimmed = text.trim();
	return BARE_EXCEPTION_NAMES.has(trimmed);
}

function humanHeadlineForBareException(feature: string | undefined): string {
	const area = feature ?? 'This feature';
	return `${area} hit a limit`;
}

function splitDeckProcessorMessage(message: string): ToastPresentation | null {
	const match = /^Deck (\d) processor failed - (.+)$/i.exec(message);
	if (match === null) return null;
	return {
		headline: `Deck ${match[1]} audio stopped unexpectedly`,
		detail: message,
		rawMessage: message,
		feature: 'Deck audio'
	};
}

function splitDeckLoadMessage(message: string): ToastPresentation | null {
	const match = /^Deck (\d) (load|stems unavailable|unload cleanup|retired processor cleanup|processor disposal) failed - (.+)$/i.exec(
		message
	);
	if (match === null) return null;
	const action = match[2].toLowerCase();
	const deck = match[1];
	let headline = `Deck ${deck} could not finish ${action}`;
	if (action === 'load') headline = `Deck ${deck} could not load the track`;
	if (action === 'stems unavailable') headline = `Deck ${deck} stems are unavailable`;
	return {
		headline,
		detail: message,
		rawMessage: message,
		feature: 'Deck audio'
	};
}

/**
 * Map a raw toast message into compact on-screen copy.
 */
export function formatToastPresentation(input: {
	kind: 'info' | 'warn' | 'error';
	message: string;
	cause?: unknown;
	feature?: string | undefined;
}): ToastPresentation {
	const rawMessage = input.message;
	const feature = inferFeature(rawMessage, input.feature);
	const causeName =
		input.cause instanceof Error && input.cause.name !== '' ? input.cause.name : undefined;
	const causeMessage =
		input.cause instanceof Error && input.cause.message !== '' ? input.cause.message : undefined;

	const deckProcessor = splitDeckProcessorMessage(rawMessage);
	if (deckProcessor !== null) {
		return withSolutionHint(input.kind, deckProcessor);
	}

	const deckLoad = splitDeckLoadMessage(rawMessage);
	if (deckLoad !== null) {
		return withSolutionHint(input.kind, deckLoad);
	}

	if (isBareExceptionText(rawMessage) || (causeName !== undefined && isBareExceptionText(causeName))) {
		const detailParts = [rawMessage];
		if (causeMessage !== undefined && causeMessage !== rawMessage) detailParts.push(causeMessage);
		return withSolutionHint(input.kind, {
			headline: humanHeadlineForBareException(feature),
			detail: detailParts.join(': '),
			feature,
			rawMessage
		});
	}

	// Long technical strings: keep a short headline, stash the rest for expand.
	if (rawMessage.length > 96 && rawMessage.includes(' - ')) {
		const splitAt = rawMessage.indexOf(' - ');
		return withSolutionHint(input.kind, {
			headline: rawMessage.slice(0, splitAt),
			detail: rawMessage,
			feature,
			rawMessage
		});
	}

	return withSolutionHint(input.kind, {
		headline: rawMessage,
		feature,
		rawMessage
	});
}

function withSolutionHint(
	kind: 'info' | 'warn' | 'error',
	presentation: ToastPresentation
): ToastPresentation {
	if (kind !== 'error') return presentation;
	return {
		...presentation,
		solutionHint: `Try reloading the page or read more at ${TOAST_SOLUTION_URL_PLACEHOLDER}`
	};
}
