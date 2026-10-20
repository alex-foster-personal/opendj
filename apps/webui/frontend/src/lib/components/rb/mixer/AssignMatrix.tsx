import type { CrossfaderAssign, MixerChannelState } from '$lib/rb/mixer-types';

type DeckId = MixerChannelState['deck_id'];

import './AssignMatrix.react.css';

/** React twin of AssignMatrix.svelte (crossfader assign matrix,
 * SCREENSHOT-SPEC 4), held to the same contract (AssignMatrix.contract.ts). */
export type AssignMatrixProps = {
	/** Which crossfader bus this matrix assigns to (A = left, B = right). */
	bus: 'A' | 'B';
	/** Current assignment per channel (shared across both matrices). */
	assigns: Record<DeckId, CrossfaderAssign>;
	/** Called with the channel's new assignment on click. */
	onassign: (deck: DeckId, assign: CrossfaderAssign) => void;
};

// Cell order matches the screenshot grids: 1 2 on top, 3 4 below.
const CELLS: DeckId[] = [1, 2, 3, 4];

export default function AssignMatrix({ bus, assigns, onassign }: AssignMatrixProps) {
	const side = bus === 'A' ? 'left (A)' : 'right (B)';
	return (
		<div className="matrix" aria-label={`crossfader ${bus} assign matrix`}>
			{CELLS.map((deck) => {
				const lit = assigns[deck] === bus;
				return (
					<button
						key={deck}
						className={lit ? 'cell lit' : 'cell'}
						title={
							lit
								? `Channel ${deck} is on ${side} now. Click to release to THRU.`
								: assigns[deck] === 'THRU'
									? `Channel ${deck} is THRU (not on the crossfader) now. Click to assign to ${side}.`
									: `Channel ${deck} is on ${assigns[deck]} now. Click to move it to ${side}.`
						}
						onClick={() => onassign(deck, assigns[deck] === bus ? 'THRU' : bus)}
					>
						{deck}
					</button>
				);
			})}
		</div>
	);
}
