import { expect, fn, userEvent, within } from 'storybook/test';

/**
 * DeckErrorBanner UI contract, shared by the Svelte story
 * (DeckErrorBanner.stories.ts) and its React twin (DeckErrorBanner.stories.tsx).
 * See src/react/README.md.
 *
 * The copy-report click writes to the real clipboard, whose answer depends
 * on the browser's permission state, and a success note clears itself on a
 * timer; neither is deterministic, so the contract clicks the body only when
 * there is no id (a pinned no-op).
 */

type Args = { deckId: number; error: string; errorId: string | null; onDismiss: () => void };
type Ctx = { canvasElement: HTMLElement; args: Args };

const HANDOVER = 'the master handover could not start deck 2';
// The banner shows the formatted headline, not the raw string.
const HANDOVER_HEADLINE = `Open DJ: ${HANDOVER}`;
const LOAD_FAILED = 'Test Tone Seven: AUDIO_FILE_MISSING: /Music/test-tone-seven.mp3';
const LOAD_HEADLINE = `Deck 4: couldn't load "Test Tone Seven". This track's audio file is missing on this machine.`;

const banner = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>('.deck-error')!;

export const contracts = {
	/** With an id: the headline, the id beside it, the id in the hover title, and a dismiss button. */
	WithId: {
		args: { deckId: 2, error: HANDOVER, errorId: 't3kq9-17', onDismiss: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const el = canvas.getByRole('alert');
			await expect(el).toHaveClass('deck-error');
			await expect(el).toHaveAttribute('data-performance-error', '2');
			await expect(el).toHaveAttribute('data-performance-error-id', 't3kq9-17');
			await expect(canvasElement.querySelector('.deck-error-text')).toHaveTextContent(HANDOVER_HEADLINE);
			await expect(el).toHaveAttribute(
				'title',
				`${HANDOVER_HEADLINE}\n\nError id t3kq9-17 - click to copy full report.`
			);
			await expect(canvas.getByText('Click to copy')).toHaveClass('deck-error-copy-hint');
			const id = canvas.getByText('t3kq9-17');
			await expect(id).toHaveClass('deck-error-id');
			await expect(id).toHaveAttribute(
				'title',
				'Error id t3kq9-17. This exact string is in the console line and the perf-event ring row for this failure.'
			);
			const dismiss = canvas.getByRole('button', { name: 'Dismiss deck 2 error' });
			await expect(dismiss).toHaveAttribute('title', 'Dismiss this deck error');
			await expect(dismiss).toHaveAttribute('data-performance-error-dismiss', '2');
			await expect(dismiss).toHaveTextContent(/^x$/);
			await userEvent.click(dismiss);
			await expect(args.onDismiss).toHaveBeenCalledTimes(1);
		}
	},
	/** Before the id lands: no id element, the title is the bare headline, and clicking copies nothing. */
	NoIdYet: {
		args: { deckId: 2, error: HANDOVER, errorId: null, onDismiss: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const el = banner(canvasElement);
			await expect(el).not.toHaveAttribute('data-performance-error-id');
			await expect(el).toHaveAttribute('title', HANDOVER_HEADLINE);
			await expect(canvasElement.querySelector('.deck-error-id')).toBeNull();
			await userEvent.click(canvasElement.querySelector<HTMLElement>('.deck-error-body')!);
			await expect(canvasElement.querySelector('.deck-error-copy-note')).toBeNull();
			await expect(args.onDismiss).not.toHaveBeenCalled();
		}
	},
	/** A coded load failure with no deck number of its own: the plain "couldn't load" headline gains Deck 4. */
	LoadFailure: {
		args: { deckId: 4, error: LOAD_FAILED, errorId: 't3kq9-18', onDismiss: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('.deck-error-text')).toHaveTextContent(LOAD_HEADLINE);
			await expect(banner(canvasElement)).toHaveAttribute(
				'title',
				`${LOAD_HEADLINE}\n\nError id t3kq9-18 - click to copy full report.`
			);
			await expect(within(canvasElement).getByRole('button', { name: 'Dismiss deck 4 error' })).toBeVisible();
		}
	}
};
