import { useEffect, useState } from 'react';

import { onPinsVisibleChanged, readPinsVisible, writePinsVisible } from '$lib/rb/feedback-pin-visibility';

import './FeedbackPinTopbarControls.react.css';

/** React twin of FeedbackPinTopbarControls.svelte (FB-17 pins-visible
 * checkbox), held to the same contract (FeedbackPinTopbarControls.contract.ts). */
export default function FeedbackPinTopbarControls() {
	const [pinsVisible, setPinsVisible] = useState(false);

	useEffect(() => {
		const sync = () => setPinsVisible(readPinsVisible(window.localStorage));
		sync();
		const off = onPinsVisibleChanged(sync);
		return () => {
			off();
		};
	}, []);

	function togglePinsVisible(): void {
		const next = !pinsVisible;
		setPinsVisible(next);
		writePinsVisible(window.localStorage, next);
	}

	return (
		<label className="fb-shell-pins-toggle" title="Show or hide feedback comment pins on every page">
			<input type="checkbox" checked={pinsVisible} onChange={togglePinsVisible} /> Show feedback comment pins
		</label>
	);
}
