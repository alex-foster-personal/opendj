import { expect, fn, userEvent, within } from 'storybook/test';

import type { BeatJumpSize } from '$lib/rb/beat-jump-size.svelte';
import type { DeckState } from '$lib/rb/deck-state-types';

/**
 * BeatJump UI contract, shared by the Svelte story (BeatJump.stories.ts) and
 * its React twin (BeatJump.stories.tsx), both titled 'Contracts/Deck/BeatJump'.
 * See src/react/README.md.
 *
 * The deck fixture carries only the DeckState fields BeatJump reads (deck_id,
 * stable_id, anlz.beatgrid.beats, duration_ms, position_ms, loop); the cast
 * keeps the rest of the big deck shape out of a story. The grid is a steady
 * 120 BPM PQTZ grid: 64 beats from 0.5 s, every 0.5 s, in a 33 s track.
 */

type Beat = { n: number; bpm: number; t: number };
type DeckFixture = {
	deck_id: 1 | 2 | 3 | 4;
	stable_id: string | null;
	anlz: { beatgrid: { beats: Beat[] } } | null;
	duration_ms: number | null;
	position_ms: number;
	loop: { in_ms: number; out_ms: number; engaged: boolean; beat_length: number | null } | null;
};
type Args = {
	deck: DeckState;
	pending: boolean;
	onJump: (beats: number) => Promise<void>;
	onSize: (beats: BeatJumpSize) => Promise<void>;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const BEATS: Beat[] = Array.from({ length: 64 }, (_, i) => ({ n: (i % 4) + 1, bpm: 120, t: 0.5 + i * 0.5 }));

// The component's prop is the full DeckState; the fixture is the subset BeatJump reads.
const deck = (over: Partial<DeckFixture>): DeckState =>
	({
		deck_id: 2,
		stable_id: 'trk-1',
		anlz: { beatgrid: { beats: BEATS } },
		duration_ms: 33000,
		position_ms: 16000,
		loop: null,
		...over
	}) as DeckState;

const jumpButton = (root: HTMLElement, delta: number): HTMLButtonElement => {
	const el = root.querySelector<HTMLButtonElement>(`button[data-beats="${delta}"]`);
	if (el === null) throw new Error(`no beat-jump button for ${delta}`);
	return el;
};
// The deck's jump size is the default 4 (DECKUX-10470): one back, one forward.
const DELTAS = [-4, 4];

export const contracts = {
	/** Mid-track on a real grid: < 4 > plus two live jumps, labeled, titled, and each click forwards its beats. */
	Ready: {
		args: { deck: deck({}), pending: false, onJump: fn(async () => {}), onSize: fn(async () => {}) } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const group = canvas.getByRole('group', { name: 'beat jump deck 2' });
			await expect(group).toHaveClass('beat-jump');
			await expect(group).toHaveAttribute('title', 'Beat jump: move the transport by the chosen number of beatgrid beats');
			await expect(canvasElement.querySelector('.column-label')).toHaveTextContent('JUMP');
			await expect(canvas.getByTestId('beat-jump-size-deck-2')).toHaveTextContent('4');
			for (const button of group.querySelectorAll('button')) {
				await expect(button.querySelector('svg')).not.toBeNull();
			}
			for (const delta of DELTAS) {
				const b = jumpButton(canvasElement, delta);
				const way = delta < 0 ? 'back' : 'forward';
				await expect(b).toBeEnabled();
				await expect(b).toHaveAttribute('data-testid', `beat-jump-${way}-deck-2`);
				await expect(b).toHaveAttribute('data-performance-control', 'beat-jump');
				await expect(b).toHaveAttribute('aria-label', `jump ${way} 4 beats deck 2`);
				await expect(b).toHaveAttribute('title', `jump ${delta > 0 ? '+4' : '-4'} beats along the real PQTZ beatgrid`);
			}
			await userEvent.click(canvas.getByRole('button', { name: 'jump forward 4 beats deck 2' }));
			await expect(args.onJump).toHaveBeenLastCalledWith(4);
			await userEvent.click(canvas.getByRole('button', { name: 'jump back 4 beats deck 2' }));
			await expect(args.onJump).toHaveBeenLastCalledWith(-4);
			await expect(args.onJump).toHaveBeenCalledTimes(2);
			await userEvent.click(canvas.getByRole('button', { name: 'larger beat jump size deck 2' }));
			await expect(args.onSize).toHaveBeenLastCalledWith(8);
			await userEvent.click(canvas.getByRole('button', { name: 'smaller beat jump size deck 2' }));
			await expect(args.onSize).toHaveBeenLastCalledWith(2);
		}
	},
	/** On the first beat: the back jump is inert with a reason, forward stays live. */
	AtFirstBeat: {
		args: { deck: deck({ position_ms: 500 }), pending: false, onJump: fn(async () => {}), onSize: fn(async () => {}) } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(jumpButton(canvasElement, -4)).toBeDisabled();
			await expect(jumpButton(canvasElement, -4)).toHaveAttribute('title', 'already within 4 beats of the first beatgrid beat');
			await expect(jumpButton(canvasElement, 4)).toBeEnabled();
			await userEvent.click(jumpButton(canvasElement, 4));
			await expect(args.onJump).toHaveBeenCalledWith(4);
		}
	},
	/** A pending deck command blocks both jumps and the size stepper, titled with that reason. */
	Pending: {
		args: { deck: deck({}), pending: true, onJump: fn(async () => {}), onSize: fn(async () => {}) } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			for (const delta of DELTAS) {
				await expect(jumpButton(canvasElement, delta)).toBeDisabled();
				await expect(jumpButton(canvasElement, delta)).toHaveAttribute('title', 'deck command pending');
			}
			await expect(args.onJump).not.toHaveBeenCalled();
		}
	},
	/** A loaded track with no beatgrid: every jump inert with the no-grid reason. */
	NoBeatgrid: {
		args: { deck: deck({ anlz: null }), pending: false, onJump: fn(async () => {}), onSize: fn(async () => {}) } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			for (const delta of DELTAS) {
				await expect(jumpButton(canvasElement, delta)).toBeDisabled();
				await expect(jumpButton(canvasElement, delta)).toHaveAttribute(
					'title',
					'track has no usable beatgrid - beat jump unavailable'
				);
			}
		}
	}
};
