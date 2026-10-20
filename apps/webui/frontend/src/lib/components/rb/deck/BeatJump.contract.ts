import { expect, fn, userEvent, within } from 'storybook/test';

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
type Args = { deck: DeckState; pending: boolean; onJump: (beats: number) => Promise<void> };
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
const DELTAS = [-8, -4, 4, 8];

export const contracts = {
	/** Mid-track on a real grid: all four jumps live, labeled, titled, and each click forwards its beats. */
	Ready: {
		args: { deck: deck({}), pending: false, onJump: fn(async () => {}) } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const group = canvas.getByRole('group', { name: 'beat jump deck 2' });
			await expect(group).toHaveClass('beat-jump');
			await expect(group).toHaveAttribute('title', 'Beat jump: move the transport by whole beatgrid beats');
			await expect(canvasElement.querySelector('.column-label')).toHaveTextContent('JUMP');
			const labels = Array.from(group.querySelectorAll('button')).map((b) => b.textContent?.trim());
			await expect(labels).toEqual(['-8', '-4', '+4', '+8']);
			for (const delta of DELTAS) {
				const b = jumpButton(canvasElement, delta);
				const lbl = delta > 0 ? `+${delta}` : String(delta);
				await expect(b).toBeEnabled();
				await expect(b).toHaveAttribute('data-testid', `beat-jump-${delta}-deck-2`);
				await expect(b).toHaveAttribute('data-performance-control', 'beat-jump');
				await expect(b).toHaveAttribute('aria-label', `jump ${lbl} beats deck 2`);
				await expect(b).toHaveAttribute('title', `jump ${lbl} beats along the real PQTZ beatgrid`);
			}
			await userEvent.click(canvas.getByRole('button', { name: 'jump +4 beats deck 2' }));
			await expect(args.onJump).toHaveBeenLastCalledWith(4);
			await userEvent.click(canvas.getByRole('button', { name: 'jump -8 beats deck 2' }));
			await expect(args.onJump).toHaveBeenLastCalledWith(-8);
			await expect(args.onJump).toHaveBeenCalledTimes(2);
		}
	},
	/** On the first beat: backward jumps are inert with a reason, forward ones stay live. */
	AtFirstBeat: {
		args: { deck: deck({ position_ms: 500 }), pending: false, onJump: fn(async () => {}) } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(jumpButton(canvasElement, -8)).toBeDisabled();
			await expect(jumpButton(canvasElement, -8)).toHaveAttribute('title', 'already within 8 beats of the first beatgrid beat');
			await expect(jumpButton(canvasElement, -4)).toBeDisabled();
			await expect(jumpButton(canvasElement, -4)).toHaveAttribute('title', 'already within 4 beats of the first beatgrid beat');
			await expect(jumpButton(canvasElement, 4)).toBeEnabled();
			await expect(jumpButton(canvasElement, 8)).toBeEnabled();
			await userEvent.click(jumpButton(canvasElement, 8));
			await expect(args.onJump).toHaveBeenCalledWith(8);
		}
	},
	/** A pending deck command blocks every jump, titled with that reason. */
	Pending: {
		args: { deck: deck({}), pending: true, onJump: fn(async () => {}) } satisfies Args,
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
		args: { deck: deck({ anlz: null }), pending: false, onJump: fn(async () => {}) } satisfies Args,
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
