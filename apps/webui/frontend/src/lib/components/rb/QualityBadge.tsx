import { useRef, useState } from 'react';

import type { TrackQuality } from '$lib/rb/library-types';
import { gradeView } from '$lib/rb/quality-grade';

import './QualityBadge.react.css';

// React twin of QualityBadge.svelte: same DOM, classes, data-venue and title,
// held to the same contract (QualityBadge.contract.ts). Renders what the
// backend measured and NOTHING else; venue null says 'Unknown' with the
// backend's reason in the title.

export type QualityBadgeProps = {
	quality: TrackQuality | null;
	/** Hide the container suffix where the row is very tight. */
	showContainer?: boolean;
	/** Initial only (C, W, S...) instead of the rung name. */
	compact?: boolean;
	/** Loads the full hover text on the first mouseenter (QLTSCAN-05). */
	loadHover?: (() => Promise<string>) | undefined;
};

/** '.mp3' -> 'mp3'; '' stays '' so nothing renders a bare dot. */
function _ext(container: string): string {
	return container.startsWith('.') ? container.slice(1) : container;
}

/** The mandatory hover title: blurb + the measured numbers behind it. */
function _title(q: TrackQuality | null): string {
	if (q === null) return 'audio quality not loaded yet for this row';
	const ext = _ext(q.container);
	if (q.venue === null) {
		// q.blurb is the backend's REASON here, not a rung description.
		return `Quality unknown -- ${q.blurb}${ext ? `, ${ext}` : ''}.`;
	}
	const measured = q.kbps === null ? 'bitrate not measurable' : `Effective ${q.kbps} kbps`;
	const lossless = q.lossless ? ' Lossless container, so the rung is set by format, not bitrate.' : '';
	return `${q.label} -- ${q.blurb}. ${measured}${ext ? `, ${ext}` : ''}.` + ` Rung ${q.rank === null ? 'unknown' : q.rank + 1} of ${q.of}.${lossless}`;
}

export default function QualityBadge({ quality, showContainer = true, compact = false, loadHover }: QualityBadgeProps) {
	const [hoverText, setHoverText] = useState<string | null>(null);
	const [hoverKey, setHoverKey] = useState<string | null>(null);
	const gradeKey = JSON.stringify(quality?.grade ?? null);
	const currentKey = useRef(gradeKey);
	currentKey.current = gradeKey;
	const full = quality === null ? '...' : quality.label;
	const label = compact ? full.slice(0, 1).toUpperCase() : full;
	const ext = quality === null ? '' : _ext(quality.container);
	const rung = quality === null || quality.venue === null ? 'unknown' : quality.venue;
	if (quality?.grade) {
		const graded = gradeView(quality.grade, compact);
		const requestHover = () => {
			if (loadHover === undefined) return;
			const key = gradeKey;
			void loadHover().then((text) => {
				if (key === currentKey.current) {
					setHoverText(text);
					setHoverKey(key);
				}
			});
		};
		return (
			<span
				className={graded.classes}
				title={hoverKey === gradeKey && hoverText ? hoverText : graded.title}
				data-venue={rung}
				data-grade={graded.dataGrade}
				onMouseEnter={requestHover}
			>
				<span className="q-label">{graded.label}</span>
				{graded.dot ? <span className="q-dot" aria-hidden="true" /> : null}
				{showContainer && ext ? <span className="q-ext">{ext}</span> : null}
			</span>
		);
	}
	return (
		<span className={`q-badge q-${rung}${compact ? ' compact' : ''}`} title={_title(quality)} data-venue={rung}>
			<span className="q-label">{label}</span>
			{showContainer && ext ? <span className="q-ext">{ext}</span> : null}
		</span>
	);
}
