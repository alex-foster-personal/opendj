import { shiftLiveBeatLoopRangeMs } from '$lib/player/transport/loops';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import { beatJumpMovesTransportWithinDuration } from '$lib/rb/beat-sync-math';
import type { DeckState } from '$lib/rb/deck-state-types';

import './BeatJump.react.css';

// React twin of BeatJump.svelte: same DOM, classes, ARIA, titles and
// behavior, held to the same contract (BeatJump.contract.ts). Each button
// counts exact PQTZ grid beats through `onJump`, never a BPM-derived offset;
// with no track, no grid, or no grid left in that direction it renders inert
// with a title saying which.

export type BeatJumpProps = {
	deck: DeckState;
	pending: boolean;
	onJump: (beats: number) => Promise<void>;
};

const JUMPS: readonly number[] = [-8, -4, 4, 8];
const NO_BEATS: readonly AnlzBeat[] = [];

function _label(delta: number): string {
	return delta > 0 ? `+${delta}` : String(delta);
}

export default function BeatJump({ deck, pending, onJump }: BeatJumpProps) {
	const beats: readonly AnlzBeat[] = deck.anlz?.beatgrid.beats ?? NO_BEATS;
	const blockedReason: string | null = pending
		? 'deck command pending'
		: deck.stable_id === null
			? 'no track loaded'
			: beats.length < 2
				? 'track has no usable beatgrid - beat jump unavailable'
				: null;

	function canJump(delta: number): boolean {
		if (blockedReason !== null) return false;
		// Without a known duration the engine's own duration clamp cannot be
		// mirrored here, so jumping is unavailable rather than a silent no-op.
		if (deck.duration_ms === null) return false;
		if (!beatJumpMovesTransportWithinDuration(beats, deck.position_ms, delta, deck.duration_ms)) {
			return false;
		}
		if (deck.loop !== null && deck.loop.engaged) {
			try {
				shiftLiveBeatLoopRangeMs(beats, deck.loop, delta, deck.duration_ms);
			} catch {
				return false;
			}
		}
		return true;
	}

	function title(delta: number): string {
		if (blockedReason !== null) return blockedReason;
		if (!canJump(delta)) {
			if (deck.loop !== null && deck.loop.engaged) {
				return `the live loop cannot shift ${_label(delta)} beats without leaving the real PQTZ grid`;
			}
			return delta < 0
				? `already within ${-delta} beats of the first beatgrid beat`
				: `fewer than ${delta} beatgrid beats remain ahead`;
		}
		return `jump ${_label(delta)} beats along the real PQTZ beatgrid`;
	}

	async function jump(delta: number): Promise<void> {
		if (!canJump(delta)) return;
		await onJump(delta);
	}

	return (
		<div
			className="beat-jump"
			role="group"
			aria-label={`beat jump deck ${deck.deck_id}`}
			title="Beat jump: move the transport by whole beatgrid beats"
		>
			<span className="column-label">JUMP</span>
			{JUMPS.map((delta) => (
				<button
					key={delta}
					disabled={!canJump(delta)}
					data-performance-control="beat-jump"
					data-testid={`beat-jump-${delta}-deck-${deck.deck_id}`}
					aria-label={`jump ${_label(delta)} beats deck ${deck.deck_id}`}
					data-beats={delta}
					title={title(delta)}
					onClick={() => void jump(delta)}
				>
					{_label(delta)}
				</button>
			))}
		</div>
	);
}
