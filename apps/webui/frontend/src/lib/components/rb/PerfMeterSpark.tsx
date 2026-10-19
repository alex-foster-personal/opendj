import { sparkPoints } from '$lib/rb/perf-meter-model';

import './PerfMeterSpark.react.css';

/** React twin of PerfMeterSpark.svelte (PERFMODE-05 sparkline), held to the
 * same contract (PerfMeterSpark.contract.ts). No stores, no timers, no fetch. */
export type PerfMeterSparkProps = {
	values: Array<number | null>;
	/** Omitted when the chart sits beside its own named row (PERF-UI-12). */
	label?: string;
	title?: string;
};

export default function PerfMeterSpark({ values, label, title }: PerfMeterSparkProps) {
	const geometry = sparkPoints(values);
	return (
		<div className="perf-spark" title={title || undefined}>
			{label && <span className="perf-spark-label">{label}</span>}
			<svg className="perf-spark-svg" viewBox="0 0 120 32" aria-hidden="true">
				{geometry.segments.map((segment) => (
					<polyline
						key={segment.points}
						points={segment.points}
						fill="none"
						stroke="currentColor"
						strokeWidth="1.5"
						strokeDasharray={segment.dashed ? '3 2' : undefined}
					/>
				))}
			</svg>
		</div>
	);
}
