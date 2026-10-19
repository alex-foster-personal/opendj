import { useState, type MouseEvent } from 'react';

import { STAR_FILLED_PATH, STAR_OUTLINE_PATH } from '$lib/ui/icon-glyphs';

import './RatingStars.react.css';

// React twin of RatingStars.svelte: same DOM, classes, ARIA and behavior,
// held to the same contract (RatingStars.contract.ts).
const STARS: number[] = [1, 2, 3, 4, 5];

export type RatingStarsProps = {
	rating: number | null;
	onrate: (next: number) => void;
	/** Non-null renders the stars inert, with this as the reason on hover. */
	disabledReason?: string | null;
};

export default function RatingStars({ rating, onrate, disabledReason = null }: RatingStarsProps) {
	const [hoverN, setHoverN] = useState<number | null>(null);
	const stop = (e: MouseEvent) => e.stopPropagation();
	const lit = (n: number): boolean => (hoverN !== null ? n <= hoverN : rating !== null && rating >= n);
	const unrated = rating === null || rating === 0;

	return (
		<span
			className={unrated ? 'rb-stars unrated' : 'rb-stars'}
			role="radiogroup"
			aria-label="Rating"
			onDoubleClick={stop}
			onPointerLeave={() => setHoverN(null)}
		>
			{STARS.map((n) => {
				const on = lit(n);
				const cls = ['rb-star', on && 'filled', hoverN !== null && n <= hoverN && 'preview']
					.filter(Boolean)
					.join(' ');
				return (
					<button
						key={n}
						className={cls}
						role="radio"
						aria-checked={rating !== null && rating >= n}
						aria-label={`Set rating ${n}`}
						title={disabledReason ?? `Set rating ${n}`}
						disabled={disabledReason !== null}
						onPointerEnter={() => setHoverN(n)}
						onClick={(e) => {
							// Keep row click/dblclick (select / deck load) out of rating edits.
							e.stopPropagation();
							onrate(rating === n ? 0 : n);
						}}
						onDoubleClick={stop}
					>
						<svg viewBox="0 0 24 24" width="1em" height="1em" aria-hidden="true">
							<path
								d={on ? STAR_FILLED_PATH : STAR_OUTLINE_PATH}
								fill={on ? 'currentColor' : 'none'}
								stroke="currentColor"
								strokeWidth="1.2"
							/>
						</svg>
					</button>
				);
			})}
		</span>
	);
}
