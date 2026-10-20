import './FaderRejoinGuidance.react.css';

// React twin of FaderRejoinGuidance.svelte: same SVG, attributes and
// animation, held to the same contract (FaderRejoinGuidance.contract.ts).
export type FaderRejoinGuidanceProps = {
	fromY: number;
	toY: number;
	trackWidth: number;
};

export default function FaderRejoinGuidance({ fromY, toY, trackWidth }: FaderRejoinGuidanceProps) {
	const dotted = Math.abs(toY - fromY) > 24;
	const midX = trackWidth * 0.35;
	return (
		<svg className="rejoin-guidance" aria-hidden="true" width={trackWidth} height="100%">
			<defs>
				<marker id="rejoin-arrowhead" markerWidth="6" markerHeight="6" refX="5" refY="3" orient="auto">
					<path d="M0,0 L6,3 L0,6 Z" fill="#e6c200" />
				</marker>
			</defs>
			<line
				x1={midX}
				y1={fromY}
				x2={midX}
				y2={toY}
				stroke="#e6c200"
				strokeWidth="1.5"
				strokeDasharray={dotted ? '3 2' : undefined}
				markerEnd="url(#rejoin-arrowhead)"
			/>
		</svg>
	);
}
