import './HotCueProposalLabel.react.css';

// React twin of HotCueProposalLabel.svelte: same DOM, held to HotCueProposalLabel.contract.ts.
//
// proposalCaption is copied from $lib/rb/auto-cue-proposals rather than
// imported: that module type-imports auto-cues-api, whose chain
// (api-rb -> analysis-issues) reaches job-progress.svelte.ts, a runes module
// tsc cannot read. The contract pins the known, empty and unknown captions, so a change to the
// original that this copy misses fails the twin's story.
const ALLOWED_KINDS = new Set(['intro', 'drop', 'break', 'outro', '']);

function proposalCaption(kind: string): string {
	if (kind === '' || !ALLOWED_KINDS.has(kind)) return 'proposed';
	return `proposed ${kind}`;
}

export type HotCueProposalLabelProps = { kind: string };

export default function HotCueProposalLabel({ kind }: HotCueProposalLabelProps) {
	return <span className="proposal-label">{proposalCaption(kind)}</span>;
}
