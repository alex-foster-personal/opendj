/**
 * Human-readable toast headlines and expandable detail (issue #3882, UX-TOAST-02).
 *
 * Pure module: no stores, no network. Call sites pass raw messages; the tray
 * shows `headline` collapsed and keeps technical text in `detail`.
 */

import type { ToastDiagnostic } from './toast-error-classification';
import { deckLoadPlainHeadline, libraryFailureCodeInText, trackTitleBeforeCode } from './rb/deck-load-failure-copy';

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
 * A library load failure whose text still carries `title: CODE: detail`.
 * The headline is the plain sentence. The raw text stays in `detail` for
 * click-to-copy. Titles are taken whole, dashes included.
 */
function codedLibraryLoadPresentation(message: string): ToastPresentation | null {
	const code = libraryFailureCodeInText(message);
	if (code === undefined) return null;
	const deckMatch = /^Deck (\d)\b/.exec(message);
	const deck = deckMatch === null ? null : Number(deckMatch[1]);
	const title = trackTitleBeforeCode(message, code);
	const headline = deckLoadPlainHeadline(deck, title, code, message);
	if (headline === null) return null;
	return {
		headline,
		detail: message,
		rawMessage: message,
		feature: 'Deck load'
	};
}

/**
 * Map a raw toast message into compact on-screen copy.
 */
function applyDiagnostic(
	presentation: ToastPresentation,
	diagnostic?: ToastDiagnostic | undefined
): ToastPresentation {
	if (diagnostic === undefined) return presentation;
	const feature = diagnostic.feature !== '' ? diagnostic.feature : presentation.feature;
	const detailParts: string[] = [];
	if (presentation.detail !== undefined && presentation.detail !== '') {
		detailParts.push(presentation.detail);
	}
	if (diagnostic.settingsSummary !== undefined && diagnostic.settingsSummary !== '') {
		detailParts.push(`Settings: ${diagnostic.settingsSummary}`);
	}
	if (diagnostic.hint !== undefined && diagnostic.hint !== '') {
		detailParts.push(diagnostic.hint);
	}
	const detail = detailParts.length > 0 ? detailParts.join('\n') : presentation.detail;
	let headline = presentation.headline;
	// A deck-load sentence already names the track. Prefixing the feature
	// ("Open DJ: 7A") is what cut a Camelot title down to its first fragment
	// once an earlier split had left only that fragment as the headline.
	const alreadyNamesTheTrack = /couldn't load "|could not load the track|stopped unexpectedly/i.test(
		headline
	);
	if (
		!alreadyNamesTheTrack &&
		feature !== undefined &&
		feature !== '' &&
		!headline.toLowerCase().includes(feature.toLowerCase()) &&
		headline.length < 72
	) {
		headline = `${feature}: ${headline}`;
	}
	return { ...presentation, feature, detail, headline };
}

export function formatToastPresentation(input: {
	kind: 'info' | 'warn' | 'error';
	message: string;
	cause?: unknown;
	feature?: string | undefined;
	diagnostic?: ToastDiagnostic | undefined;
}): ToastPresentation {
	const rawMessage = input.message;
	const feature = inferFeature(rawMessage, input.feature ?? input.diagnostic?.feature);
	const causeName =
		input.cause instanceof Error && input.cause.name !== '' ? input.cause.name : undefined;
	const causeMessage =
		input.cause instanceof Error && input.cause.message !== '' ? input.cause.message : undefined;

	const deckProcessor = splitDeckProcessorMessage(rawMessage);
	if (deckProcessor !== null) {
		return applyDiagnostic(withSolutionHint(input.kind, deckProcessor), input.diagnostic);
	}

	// Before the generic "Deck N load failed" split. That split drops the
	// title, and a title may itself contain " - ".
	const coded = codedLibraryLoadPresentation(rawMessage);
	if (coded !== null) {
		return applyDiagnostic(withSolutionHint(input.kind, coded), input.diagnostic);
	}

	const deckLoad = splitDeckLoadMessage(rawMessage);
	if (deckLoad !== null) {
		return applyDiagnostic(withSolutionHint(input.kind, deckLoad), input.diagnostic);
	}

	if (isBareExceptionText(rawMessage) || (causeName !== undefined && isBareExceptionText(causeName))) {
		const detailParts = [rawMessage];
		if (causeMessage !== undefined && causeMessage !== rawMessage) detailParts.push(causeMessage);
		return applyDiagnostic(
			withSolutionHint(input.kind, {
				headline: humanHeadlineForBareException(feature),
				detail: detailParts.join(': '),
				feature,
				rawMessage
			}),
			input.diagnostic
		);
	}

	// Long technical strings: keep a short headline, stash the rest for expand.
	// Never split or cut on " - ". A track title is allowed to contain it
	// ("7A - 6 - Glasswing"), and the first fragment is not the error.
	if (rawMessage.length > 96) {
		const newline = rawMessage.indexOf('\n');
		const splitAt = newline > 0 && newline < 96 ? newline : 93;
		const headline = newline > 0 && newline < 96 ? rawMessage.slice(0, newline) : `${rawMessage.slice(0, splitAt)}...`;
		return applyDiagnostic(
			withSolutionHint(input.kind, {
				headline,
				detail: rawMessage,
				feature,
				rawMessage
			}),
			input.diagnostic
		);
	}

	return applyDiagnostic(
		withSolutionHint(input.kind, {
			headline: rawMessage,
			feature,
			rawMessage
		}),
		input.diagnostic
	);
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
