import type { LyricVerdictMark as Mark } from './lyric-column';

import './LyricVerdictMark.react.css';

// React twin of LyricVerdictMark.svelte: same DOM, held to LyricVerdictMark.contract.ts.
export type LyricVerdictMarkProps = { mark: Mark };

export default function LyricVerdictMark({ mark }: LyricVerdictMarkProps) {
	if (mark.kind === 'icon') {
		return (
			<svg className="lyr-mark-icon" viewBox="0 0 24 24" width="11" height="11" aria-hidden="true">
				<path d={mark.path} fill="none" stroke="currentColor" strokeWidth="2" />
			</svg>
		);
	}
	if (mark.kind === 'pending') {
		return (
			<svg className="lyr-mark-icon lyr-mark-pending" viewBox="0 0 30 24" width="14" height="11" aria-hidden="true">
				<path d={mark.path} fill="none" stroke="currentColor" strokeWidth="2.4" />
				<circle className="lyr-pending-dot" cx="26" cy="5" r="3.2" fill="currentColor" />
			</svg>
		);
	}
	if (mark.kind === 'text') return <>{mark.text}</>;
	// Exhaustive: a new kind in lyric-column.ts fails to compile here, and an
	// untyped one at runtime throws rather than drawing an empty cell.
	const unhandled: never = mark;
	throw new Error(`unhandled lyric verdict mark ${JSON.stringify(unhandled)}`);
}
