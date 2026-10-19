import type { KeyboardEvent } from 'react';

import './MissingTracksFolder.react.css';

// React twin of MissingTracksFolder.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (MissingTracksFolder.contract.ts).
// Presentational Missing Tracks tree row. Count is GET /reconcile/summary
// total_broken (passed in); click selects the reserved 'missing' node.

export type MissingTracksFolderProps = {
	brokenCount: number | null;
	error: string | null;
	selected: boolean;
	onselect: () => void;
};

export default function MissingTracksFolder({ brokenCount, error, selected, onselect }: MissingTracksFolderProps) {
	const countText = error !== null ? '!' : brokenCount === null ? '...' : String(brokenCount);
	const countTitle =
		error !== null
			? `broken count unavailable: ${error}`
			: brokenCount === null
				? 'loading broken track count'
				: `${brokenCount} broken tracks`;

	return (
		<div
			data-menu-target="tree-readonly"
			className={selected ? 'row selected' : 'row'}
			data-testid="playlist-missing-tracks"
			role="button"
			tabIndex={0}
			onClick={() => onselect()}
			onKeyDown={(event: KeyboardEvent) => {
				if (event.key === 'Enter') onselect();
			}}
		>
			<svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
				<path d="M1 3h5l1.5 2H15v8H1z" fill="currentColor" />
			</svg>
			<span className="name">Missing Tracks</span>
			<span className="count" title={countTitle}>
				{countText}
			</span>
		</div>
	);
}
