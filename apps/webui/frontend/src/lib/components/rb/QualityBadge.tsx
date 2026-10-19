import type { TrackQuality } from '$lib/rb/library-types';

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
	return `${q.label} -- ${q.blurb}. ${measured}${ext ? `, ${ext}` : ''}.` + ` Rung ${(q.rank ?? 0) + 1} of ${q.of}.${lossless}`;
}

export default function QualityBadge({ quality, showContainer = true, compact = false }: QualityBadgeProps) {
	const full = quality === null ? '...' : quality.label;
	const label = compact ? full.slice(0, 1).toUpperCase() : full;
	const ext = quality === null ? '' : _ext(quality.container);
	const rung = quality === null || quality.venue === null ? 'unknown' : quality.venue;
	return (
		<span className={`q-badge q-${rung}${compact ? ' compact' : ''}`} title={_title(quality)} data-venue={rung}>
			<span className="q-label">{label}</span>
			{showContainer && ext ? <span className="q-ext">{ext}</span> : null}
		</span>
	);
}
