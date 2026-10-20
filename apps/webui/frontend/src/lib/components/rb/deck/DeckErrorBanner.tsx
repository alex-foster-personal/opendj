import { useState } from 'react';

import { classifyToastError } from '$lib/toast-error-classification';
import { formatToastPresentation } from '$lib/toast-presentation';
import { buildToastReport, writeToastReport } from '$lib/toast-report';

import './DeckErrorBanner.react.css';

// React twin of DeckErrorBanner.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (DeckErrorBanner.contract.ts). Presentational: the
// parent owns the deck identity, the error id and the dismiss action. A null
// `errorId` renders no id at all, never a placeholder that looks like one.

export type DeckErrorBannerProps = {
	deckId: number;
	error: string;
	errorId: string | null;
	onDismiss: () => void;
};

export default function DeckErrorBanner({ deckId, error, errorId, onDismiss }: DeckErrorBannerProps) {
	const diagnostic = classifyToastError({
		kind: 'error',
		message: error,
		context: { deck_id: deckId, source: 'deck-error-banner' }
	});
	const presentation = formatToastPresentation({ kind: 'error', message: error, diagnostic });
	// The load failure string often has no deck number of its own.
	const shownHeadline = presentation.headline.startsWith("couldn't load ")
		? `Deck ${deckId}: ${presentation.headline}`
		: presentation.headline;

	const [copyNote, setCopyNote] = useState<string | null>(null);

	async function copyReport(): Promise<void> {
		if (errorId === null) return;
		const href = typeof window === 'undefined' ? '' : (window.location?.href ?? '');
		const page = href === '' ? 'unknown' : href.split('?')[0].split('#')[0];
		const text = buildToastReport({
			id: errorId,
			kind: 'error',
			headline: shownHeadline,
			message: error,
			detail: presentation.detail,
			classification: diagnostic.classification,
			settingsSummary: diagnostic.settingsSummary,
			hint: diagnostic.hint,
			createdAt: new Date().toISOString(),
			env: {
				machine: 'unknown',
				user: 'signed out',
				client: { name: 'unknown', version: 'unknown' },
				url: page
			}
		});
		try {
			await writeToastReport(
				text,
				typeof navigator === 'undefined' ? undefined : navigator.clipboard,
				typeof window !== 'undefined' && window.isSecureContext === true
			);
			setCopyNote('copied');
			setTimeout(() => {
				setCopyNote(null);
			}, 1500);
		} catch (exc) {
			setCopyNote(exc instanceof Error ? exc.message : String(exc));
		}
	}

	return (
		<div
			className="deck-error"
			role="alert"
			data-performance-error={deckId}
			data-performance-error-id={errorId ?? undefined}
			title={errorId === null ? shownHeadline : `${shownHeadline}\n\nError id ${errorId} - click to copy full report.`}
		>
			<button type="button" className="deck-error-body" onClick={() => void copyReport()}>
				<span className="deck-error-text">{shownHeadline}</span>
				<span className="deck-error-copy-hint">Click to copy</span>
				{copyNote !== null && <span className="deck-error-copy-note">{copyNote}</span>}
			</button>
			{errorId !== null && (
				<span
					className="deck-error-id"
					title={`Error id ${errorId}. This exact string is in the console line and the perf-event ring row for this failure.`}
				>
					{errorId}
				</span>
			)}
			<button
				type="button"
				className="deck-error-dismiss"
				data-performance-error-dismiss={deckId}
				title="Dismiss this deck error"
				aria-label={`Dismiss deck ${deckId} error`}
				onClick={onDismiss}
			>
				x
			</button>
		</div>
	);
}
