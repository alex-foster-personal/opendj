import { expect, within } from 'storybook/test';

import { RELEASE_STAGE, RELEASE_STAGE_TITLE } from '$lib/release-stage';

/**
 * AlphaBadge UI contract, shared by the Svelte story (AlphaBadge.stories.ts)
 * and its React twin (AlphaBadge.stories.tsx). The badge takes no props, so
 * one story pins it. See src/react/README.md.
 */

type Ctx = { canvasElement: HTMLElement };

export const contracts = {
	/** The release stage as text, with the explanation on hover. */
	Default: {
		args: {},
		play: async ({ canvasElement }: Ctx) => {
			const badge = within(canvasElement).getByTestId('release-stage-badge');
			await expect(badge.tagName).toBe('SPAN');
			await expect(badge).toHaveClass('release-stage');
			await expect(badge.textContent).toBe(RELEASE_STAGE);
			await expect(badge).toHaveAttribute('title', RELEASE_STAGE_TITLE);
			await expect(getComputedStyle(badge).fontStyle).toBe('italic');
		}
	}
};
