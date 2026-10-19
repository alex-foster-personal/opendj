import './PositionBar.react.css';

// React twin of PositionBar.svelte: same DOM, classes and title, held to the
// same contract (PositionBar.contract.ts).
export type PositionBarProps = {
	positionMs?: number;
	durationMs?: number | null;
	label?: string;
};

export default function PositionBar({ positionMs = 0, durationMs = null, label = 'Track position' }: PositionBarProps) {
	const pct =
		durationMs !== null && durationMs > 0 ? Math.min(100, Math.max(0, (positionMs / durationMs) * 100)) : 0;
	return (
		<div className="position-bar" data-testid="trackify-position-bar" aria-label={label}>
			<div
				className="position-fill"
				style={{ width: `${pct}%` }}
				title={`${Math.round(positionMs)} ms of ${durationMs ?? 'unknown'} ms`}
			></div>
		</div>
	);
}
