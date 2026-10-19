import { useEffect, useRef, useState } from 'react';

import { loadRowsPerSecond, type LoadRateBaseline } from '$lib/rb/load-rate';

import './LibraryLoadIndicator.react.css';

// React twin of LibraryLoadIndicator.svelte: same DOM, classes, ARIA and
// behavior, held to the same contract (LibraryLoadIndicator.contract.ts).
// The strip is always in the layout at one fixed height; only its content is
// conditional. Bar, count and rows/s move only in whole-page jumps.

export type LibraryLoadIndicatorProps = {
	loading: boolean;
	progress: { loaded: number; total: number | null } | null;
	searching?: boolean;
};

export default function LibraryLoadIndicator({ loading, progress, searching = false }: LibraryLoadIndicatorProps) {
	// LIBUX-37: rows/s measured from a baseline reset when a load starts
	// (progress null -> non-null) or is superseded (loaded goes backwards).
	const baseline = useRef<LoadRateBaseline | null>(null);
	const [rowsPerSecond, setRowsPerSecond] = useState<number | null>(null);

	useEffect(() => {
		const p = progress;
		if (p === null) {
			baseline.current = null;
			setRowsPerSecond(null);
			return;
		}
		const now = performance.now();
		if (baseline.current === null || p.loaded < baseline.current.loaded) {
			baseline.current = { atMs: now, loaded: p.loaded };
		}
		setRowsPerSecond(loadRowsPerSecond(baseline.current, now, p.loaded));
	}, [progress]);

	const pct: number | null =
		progress === null || progress.total === null || progress.total <= 0
			? null
			: Math.min(100, Math.round((progress.loaded / progress.total) * 100));

	let label: string;
	if (progress === null) label = 'loading...';
	else {
		const verb = loading ? 'loading...' : 'loading more...';
		label =
			progress.total === null
				? `${verb} ${progress.loaded.toLocaleString()} rows received`
				: `${verb} ${progress.loaded.toLocaleString()} of ${progress.total.toLocaleString()} rows`;
	}

	const showBar = loading || progress !== null;

	return (
		<div className="lli-root" role="status" aria-live="polite">
			{(showBar || searching) && (
				<>
					<span className="lli-mark" aria-hidden="true"></span>
					{searching && <span className="lli-search">searching whole collection...</span>}
					{showBar && (
						<>
							<div
								className={pct === null ? 'lli-track lli-indeterminate' : 'lli-track'}
								role="progressbar"
								aria-label={label}
								aria-valuemin={pct === null ? undefined : 0}
								aria-valuemax={pct === null ? undefined : 100}
								aria-valuenow={pct ?? undefined}
								title={pct === null ? 'Loading progress is not yet knowable' : `${pct}%`}
							>
								<div className="lli-bar" style={pct === null ? undefined : { width: `${pct}%` }}></div>
							</div>
							<span className="lli-label">
								{label}
								{rowsPerSecond !== null && (
									<span
										className="lli-rate"
										title="Rows received per second, measured since this load's first page over a window of at least one second"
									>
										{` · ${Math.round(rowsPerSecond)} rows/s`}
									</span>
								)}
							</span>
						</>
					)}
				</>
			)}
		</div>
	);
}
