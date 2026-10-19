import { expect } from 'storybook/test';

import { LYRIC_UNCHECKED_MARK, LYRIC_VERDICT_MARKS, type LyricVerdictMark } from './lyric-column';

/**
 * LyricVerdictMark UI contract, shared by the Svelte story (LyricVerdictMark.stories.ts)
 * and its React twin (LyricVerdictMark.stories.tsx). See src/react/README.md.
 */

type Args = { mark: LyricVerdictMark };
type Ctx = { canvasElement: HTMLElement; args: Args };

const pathOf = (m: LyricVerdictMark): string => (m.kind === 'text' ? '' : m.path);

export const contracts = {
	/** The vocal verdict: one 11px icon drawn from the mark's path, no text. */
	Vocal: {
		args: { mark: LYRIC_VERDICT_MARKS.vocal } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const svgs = canvasElement.querySelectorAll('svg.lyr-mark-icon');
			await expect(svgs).toHaveLength(1);
			const svg = svgs[0];
			await expect(svg).not.toHaveClass('lyr-mark-pending');
			await expect(svg).toHaveAttribute('viewBox', '0 0 24 24');
			await expect(svg).toHaveAttribute('width', '11');
			await expect(svg).toHaveAttribute('aria-hidden', 'true');
			await expect(svg.querySelector('path')).toHaveAttribute('d', pathOf(LYRIC_VERDICT_MARKS.vocal));
			await expect(svg.querySelector('circle')).toBeNull();
		}
	},
	/** Lyrics found but unchecked: the wider sheet icon with a pending dot. */
	Pending: {
		args: { mark: LYRIC_UNCHECKED_MARK } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const svg = canvasElement.querySelector('svg.lyr-mark-icon.lyr-mark-pending');
			await expect(svg).not.toBeNull();
			await expect(svg).toHaveAttribute('viewBox', '0 0 30 24');
			await expect(svg).toHaveAttribute('width', '14');
			await expect(svg?.querySelector('path')).toHaveAttribute('d', pathOf(LYRIC_UNCHECKED_MARK));
			await expect(svg?.querySelector('circle.lyr-pending-dot')).toHaveAttribute('fill', 'currentColor');
		}
	},
	/** A text verdict renders its compact mark as bare text, no icon. */
	Sparse: {
		args: { mark: LYRIC_VERDICT_MARKS.sparse } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('svg')).toBeNull();
			await expect(canvasElement.textContent?.trim()).toBe('~');
		}
	}
};
