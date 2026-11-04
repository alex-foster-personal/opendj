import type { TrackCloudView } from './track-cloud-state';

import './CloudStatusIcon.react.css';

// React twin of CloudStatusIcon.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (CloudStatusIcon.contract.ts).

export type CloudStatusIconProps = { view: TrackCloudView };

export default function CloudStatusIcon({ view }: CloudStatusIconProps) {
	// Tidal's mark is black and white, so its glyph takes a theme-aware fill
	// from the `.tidal` rule instead of the dim column text.
	const providerColor =
		view.provider === 'spotify' ? '#35c04f' : view.provider === 'soundcloud' ? '#ff5500' : 'currentColor';
	const cls = ['cloud-status-icon', view.kind].join(' ');
	const showProvider = view.kind === 'streaming' && view.provider !== null && view.provider !== 'unknown';

	return (
		<span className={cls} title={view.title} aria-label={view.title}>
			{showProvider ? (
				<svg className="provider-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					{view.provider === 'spotify' ? (
						<>
							{/* Hand-drawn Spotify mark: green disk with three dark sound-wave
							    arcs, widest on top, so it reads as the provider and not as a
							    status dot. */}
							<circle cx="8" cy="8" r="7" fill={providerColor} />
							<path className="spotify-arc" d="M3.9 6.2Q8 4.6 12.1 6.6" fill="none" stroke="#191414" strokeWidth="1.4" strokeLinecap="round" />
							<path className="spotify-arc" d="M4.5 8.5Q8 7.3 11.3 8.9" fill="none" stroke="#191414" strokeWidth="1.15" strokeLinecap="round" />
							<path className="spotify-arc" d="M5.1 10.7Q8 9.8 10.5 11" fill="none" stroke="#191414" strokeWidth="0.95" strokeLinecap="round" />
						</>
					) : view.provider === 'soundcloud' ? (
						// SoundCloud's mark: four bars rising left to right into a cloud
						// with a flat base, a large bump and a smaller one on the right.
						<path
							className="soundcloud"
							d="M.6 9.4h1V12h-1zm1.6-1h1V12h-1zm1.6-1h1V12h-1zm1.6-1h1V12h-1zM7 12V5.2A4 4 0 0 1 13 7.5a2.25 2.25 0 0 1 0 4.5z"
							fill={providerColor}
						/>
					) : view.provider === 'tidal' ? (
						// Tidal's four-diamond mark: three touching across the top, one
						// under the middle. Filled by the `.tidal` rule.
						<path className="tidal" d="M.5 5.5l2.5-2.5 2.5 2.5-2.5 2.5zm5 0l2.5-2.5 2.5 2.5-2.5 2.5zm5 0l2.5-2.5 2.5 2.5-2.5 2.5zm-5 5l2.5-2.5 2.5 2.5-2.5 2.5z" />
					) : null}
				</svg>
			) : view.kind === 'temporary' ? (
				// Spotify's "downloaded" mark: a filled disk with a down arrow.
				<svg className="offload-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<circle cx="8" cy="8" r="7" fill="currentColor" />
					<path d="M8 4.5v6M5.4 8.2 8 10.8l2.6-2.6" fill="none" stroke="var(--rb-bg, #111)" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" />
				</svg>
			) : view.kind === 'offloaded' ? (
				// Apple Music's "download" mark: a hollow cloud with a down arrow.
				<svg className="offload-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z" fill="none" stroke="currentColor" strokeWidth="1.25" />
					<path d="M8 6.5v4M6.5 9.2 8 10.7l1.5-1.5" fill="none" stroke="currentColor" strokeWidth="1.25" strokeLinecap="round" strokeLinejoin="round" />
				</svg>
			) : (
				<svg className="cloud-icon" viewBox="0 0 16 16" width="12" height="12" aria-hidden="true">
					<path
						d="M4.5 12a3 3 0 0 1-.4-5.97A4 4 0 0 1 12 6.5 2.75 2.75 0 0 1 11.5 12z"
						fill={view.kind === 'not-on-cloud' ? 'none' : 'currentColor'}
						stroke="currentColor"
						strokeWidth="1.25"
					/>
					{view.kind === 'not-on-cloud' && (
						<path d="M3 13 13 3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
					)}
				</svg>
			)}
			{view.overlay === 'green-tick' ? (
				<svg className="tick-overlay tick-green" viewBox="0 0 8 8" width="6" height="6" aria-hidden="true">
					<path d="M1.2 4.2 L3.2 6.2 L6.8 1.8" fill="none" stroke="#35c04f" strokeWidth="1.4" strokeLinecap="round" />
				</svg>
			) : view.overlay === 'blue-tick' ? (
				<svg className="tick-overlay tick-blue" viewBox="0 0 8 8" width="6" height="6" aria-hidden="true">
					<path d="M1.2 4.2 L3.2 6.2 L6.8 1.8" fill="none" stroke="#4cc9f0" strokeWidth="1.4" strokeLinecap="round" />
				</svg>
			) : null}
		</span>
	);
}
