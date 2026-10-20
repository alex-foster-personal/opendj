import { expect } from 'storybook/test';

/**
 * HotCueProposalLabel UI contract, shared by the Svelte story
 * (HotCueProposalLabel.stories.ts) and its React twin
 * (HotCueProposalLabel.stories.tsx). See src/react/README.md.
 */

type Args = { kind: string };
type Ctx = { canvasElement: HTMLElement; args: Args };

const label = (root: HTMLElement): HTMLElement => {
	const el = root.querySelector<HTMLElement>('span.proposal-label');
	if (el === null) throw new Error('span.proposal-label not rendered');
	return el;
};

export const contracts = {
	/** A known kind is named in the caption, set in italic. */
	Drop: {
		args: { kind: 'drop' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = label(canvasElement);
			await expect(el.textContent).toBe('proposed drop');
			await expect(getComputedStyle(el).fontStyle).toBe('italic');
			await expect(getComputedStyle(el).textOverflow).toBe('ellipsis');
		}
	},
	/** An empty kind reads as a bare proposal. */
	Unnamed: {
		args: { kind: '' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(label(canvasElement).textContent).toBe('proposed');
		}
	},
	/** A kind outside the allowed set is never echoed. */
	UnknownKind: {
		args: { kind: 'chorus' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(label(canvasElement).textContent).toBe('proposed');
		}
	}
};
