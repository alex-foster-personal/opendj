import { shiftLiveBeatLoopRangeMs } from '$lib/player/transport/loops';
import type { AnlzBeat } from '$lib/rb/anlz-types';
import {
	beatJumpSizeLabel,
	beatJumpSizes,
	steppedBeatJumpSize,
	type BeatJumpSize
} from '$lib/rb/beat-jump-size.svelte';
import { formatBeatJumpDelta } from '$lib/rb/format-beat-jump';
import { beatJumpMovesTransportWithinDuration } from '$lib/rb/beat-sync-math';
import type { DeckState } from '$lib/rb/deck-state-types';

import './BeatJump.react.css';

// React twin of BeatJump.svelte: same DOM, classes, ARIA, titles and
// behavior, held to the same contract (BeatJump.contract.ts). One per-deck
// jump size (DECKUX-10470) stepped with < >, and two buttons that count exact
// PQTZ grid beats through `onJump`, never a BPM-derived offset; with no track,
// no grid, or no grid left in that direction a jump renders inert with a title
// saying which.

export type BeatJumpProps = {
	deck: DeckState;
	pending: boolean;
	onJump: (beats: number) => Promise<void>;
	onSize: (beats: BeatJumpSize) => Promise<void>;
};

const NO_BEATS: readonly AnlzBeat[] = [];

export default function BeatJump({ deck, pending, onJump, onSize }: BeatJumpProps) {
	const size: BeatJumpSize = beatJumpSizes[deck.deck_id];
	const smaller = steppedBeatJumpSize(size, -1);
	const larger = steppedBeatJumpSize(size, 1);
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
			if (deck.loop !== null && deck.loop.engaged && !Number.isInteger(delta)) {
				return 'a live loop shifts by whole beats only - pick 1 beat or more';
			}
			if (deck.loop !== null && deck.loop.engaged) {
				return `the live loop cannot shift ${formatBeatJumpDelta(delta)} beats without leaving the real PQTZ grid`;
			}
			return delta < 0
				? `already within ${beatJumpSizeLabel(-delta)} beats of the first beatgrid beat`
				: `fewer than ${beatJumpSizeLabel(delta)} beatgrid beats remain ahead`;
		}
		return `jump ${formatBeatJumpDelta(delta)} beats along the real PQTZ beatgrid`;
	}

	async function jump(delta: number): Promise<void> {
		if (!canJump(delta)) return;
		await onJump(delta);
	}

	async function step(next: BeatJumpSize): Promise<void> {
		if (pending || next === size) return;
		await onSize(next);
	}

	return (
		<div
			className="beat-jump"
			role="group"
			aria-label={`beat jump deck ${deck.deck_id}`}
			title="Beat jump: move the transport by the chosen number of beatgrid beats"
		>
			<span className="column-label">JUMP</span>
			<button
				className="size-step"
				disabled={pending || smaller === size}
				data-performance-control="beat-jump-size-down"
				data-testid={`beat-jump-size-down-deck-${deck.deck_id}`}
				aria-label={`smaller beat jump size deck ${deck.deck_id}`}
				title={
					smaller === size ? 'smallest jump size' : `jump size ${beatJumpSizeLabel(smaller)} beats`
				}
				onClick={() => void step(smaller)}
			>
				<svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false">
					<path
						d="M5.2 1.4 2.6 4 5.2 6.6"
						fill="none"
						stroke="currentColor"
						strokeWidth="1.4"
						strokeLinecap="round"
						strokeLinejoin="round"
					/>
				</svg>
			</button>
			<span className="size-readout" data-testid={`beat-jump-size-deck-${deck.deck_id}`}>
				{beatJumpSizeLabel(size)}
			</span>
			<button
				className="size-step"
				disabled={pending || larger === size}
				data-performance-control="beat-jump-size-up"
				data-testid={`beat-jump-size-up-deck-${deck.deck_id}`}
				aria-label={`larger beat jump size deck ${deck.deck_id}`}
				title={
					larger === size ? 'largest jump size' : `jump size ${beatJumpSizeLabel(larger)} beats`
				}
				onClick={() => void step(larger)}
			>
				<svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false">
					<path
						d="M2.8 1.4 5.4 4 2.8 6.6"
						fill="none"
						stroke="currentColor"
						strokeWidth="1.4"
						strokeLinecap="round"
						strokeLinejoin="round"
					/>
				</svg>
			</button>
			{[-size, size].map((delta) => (
				<button
					key={delta}
					className="jump"
					disabled={!canJump(delta)}
					data-performance-control="beat-jump"
					data-testid={`beat-jump-${delta < 0 ? 'back' : 'forward'}-deck-${deck.deck_id}`}
					aria-label={`jump ${delta < 0 ? 'back' : 'forward'} ${beatJumpSizeLabel(size)} beats deck ${deck.deck_id}`}
					data-beats={delta}
					title={title(delta)}
					onClick={() => void jump(delta)}
				>
					{delta < 0 ? (
						<svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false">
							<path d="M6 1.2 2.4 4 6 6.8Z" fill="currentColor" />
						</svg>
					) : (
						<svg viewBox="0 0 8 8" width="7" height="7" aria-hidden="true" focusable="false">
							<path d="M2 1.2 5.6 4 2 6.8Z" fill="currentColor" />
						</svg>
					)}
				</button>
			))}
		</div>
	);
}
