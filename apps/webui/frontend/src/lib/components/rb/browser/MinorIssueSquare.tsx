// Structural copy of MinorIssue (lib/rb/track-minor-issues.ts): importing that
// module, even for a type, drags pane-contract.svelte.ts and its runes into tsc.
type MinorIssue = {
	id: 'not-on-cloud' | 'low-quality' | 'missing-genre' | 'stems-failed' | 'no-lyrics';
	label: string;
};

import './MinorIssueSquare.react.css';

// React twin of MinorIssueSquare.svelte: same DOM, held to MinorIssueSquare.contract.ts.
export type MinorIssueSquareProps = { issues: MinorIssue[] };

export default function MinorIssueSquare({ issues }: MinorIssueSquareProps) {
	if (issues.length === 0) return null;
	const title = issues.map((i) => i.label).join('; ');
	return (
		<span className="minor-issue-square" title={title} aria-label={title} data-testid="minor-issue-square">
			<svg viewBox="0 0 10 10" width="9" height="9" aria-hidden="true">
				<rect className="mis-fill" x="1" y="1" width="8" height="8" rx="1" />
				<path className="mis-ink" d="M5 2.5v3.2M5 7.2h.01" strokeWidth="1.1" strokeLinecap="round" />
			</svg>
		</span>
	);
}
