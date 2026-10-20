import { Fragment } from 'react';

import { readoutWords, type PointerWord, type WordIndex } from '$lib/lyrics/pointer-word';

import './ScrubLyricStrip.react.css';

// React twin of ScrubLyricStrip.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (ScrubLyricStrip.contract.ts).
// Floating word readout for scrub-hover surfaces. Pure presentation: the
// parent resolves the pointer and passes the result in. pointer-events: none
// throughout, so the readout never steals the click underneath it.

export type ScrubLyricStripProps = {
	/** The indexed words of the hovered track. */
	index: WordIndex;
	/** The resolved pointer state for the current pointer x. */
	state: PointerWord;
	/** Pointer x in CSS px, relative to the strip's left edge. */
	xPx: number;
	/** Strip width in CSS px (clamps the readout inside the row). */
	widthPx: number;
	/** Display-order neighbors shown either side of the focus word. */
	radius?: number;
};

function titleFor(kind: PointerWord['kind']): string {
	if (kind === 'inside') return 'word under the pointer (aligned onset/offset seconds bound it)';
	if (kind === 'before') return 'pointer is just before this word';
	if (kind === 'after') return 'pointer is just after this word';
	return 'pointer is in an instrumental gap between the dimmed words';
}

export default function ScrubLyricStrip({ index, state, xPx, widthPx, radius = 2 }: ScrubLyricStripProps) {
	const words = readoutWords(index, state, radius);
	if (words.length === 0) return null;
	const clampedX = Math.min(widthPx - 8, Math.max(8, xPx));

	return (
		<div className="scrub-readout" data-kind={state.kind} style={{ left: `${clampedX}px` }} title={titleFor(state.kind)} aria-hidden="true">
			{words.map((rw) => (
				<Fragment key={rw.idx}>
					<span className="w" data-role={rw.role}>
						{rw.text}
					</span>
					{rw.gapAfter && <span className="gap-dot" title="the pointer is in this gap"></span>}{' '}
				</Fragment>
			))}
		</div>
	);
}
