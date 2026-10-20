import { Fragment } from 'react';

import './PadStrip.react.css';

// React twin of PadStrip.svelte: same DOM and classes, held to the same
// contract (PadStrip.contract.ts). Decorative only; theme.css hides it via
// --rb-pad-strip-display on .perf-root.
const PAD_LETTERS: string[] = ['A', 'B', 'C', 'D', 'E', 'F', 'G', 'H'];

export default function PadStrip() {
	return (
		<div className="pad-strip" aria-hidden="true">
			{PAD_LETTERS.map((letter, i) => (
				<Fragment key={letter}>
					{i === 4 && <span className="pad-sep">|</span>}
					<span className="pad-letter">{letter}</span>
				</Fragment>
			))}
		</div>
	);
}
