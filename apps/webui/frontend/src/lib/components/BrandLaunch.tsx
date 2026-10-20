import { useEffect, useState, type AnimationEvent, type CSSProperties } from 'react';

import {
	BRAND_LAUNCH_DURATION_MS,
	BRAND_LAUNCH_FADE_MS,
	BRAND_LAUNCH_HOLD_MS,
	BRAND_LAUNCH_SLIDE_MS,
	completeBrandLaunch,
	shouldPlayBrandLaunch
} from '$lib/brand-launch';

import './BrandLaunch.react.css';

// React twin of BrandLaunch.svelte: same DOM, classes and timing variables,
// held to the same contract (BrandLaunch.contract.ts). Plays once per browser;
// completion is persisted only when the overlay's own fade ends, never on the
// halves' bubbling slide animationend.

export default function BrandLaunch() {
	const [visible, setVisible] = useState(false);

	useEffect(() => {
		setVisible(shouldPlayBrandLaunch(window.localStorage));
	}, []);

	function finish(event: AnimationEvent<HTMLDivElement>): void {
		if (event.target !== event.currentTarget) return;
		completeBrandLaunch(window.localStorage);
		setVisible(false);
	}

	if (!visible) return null;
	const timing = {
		'--brand-slide-ms': `${BRAND_LAUNCH_SLIDE_MS}ms`,
		'--brand-hold-ms': `${BRAND_LAUNCH_HOLD_MS}ms`,
		'--brand-fade-ms': `${BRAND_LAUNCH_FADE_MS}ms`,
		'--brand-total-ms': `${BRAND_LAUNCH_DURATION_MS}ms`
	} as CSSProperties;
	return (
		<div className="brand-launch" aria-label="Open DJ launch animation" style={timing} onAnimationEnd={finish}>
			<div className="brand-mark" aria-hidden="true">
				<svg className="brand-mark-svg" viewBox="0 0 512 512" xmlns="http://www.w3.org/2000/svg">
					<path
						className="brand-half-dark"
						data-testid="brand-launch-half-dark"
						d="M 367.72,144.28 A 188.00,188.00 0 0,0 101.85,410.15 Z"
						fill="#9c4b34"
					/>
					<path
						className="brand-half-light"
						data-testid="brand-launch-half-light"
						d="M 410.15,101.85 A 188.00,188.00 0 0,1 144.28,367.72 Z"
						fill="#D97757"
					/>
				</svg>
			</div>
		</div>
	);
}
