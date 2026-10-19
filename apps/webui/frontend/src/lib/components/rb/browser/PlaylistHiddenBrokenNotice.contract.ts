import { expect, within } from 'storybook/test';

/**
 * PlaylistHiddenBrokenNotice UI contract, shared by the Svelte story
 * (PlaylistHiddenBrokenNotice.stories.ts) and its React twin
 * (PlaylistHiddenBrokenNotice.stories.tsx). See src/react/README.md.
 */

type Args = { hiddenBrokenPlaylistCount: number };
type Ctx = { canvasElement: HTMLElement; args: Args };

export const contracts = {
	/** One hidden playlist: singular copy on an inert child row. */
	One: {
		args: { hiddenBrokenPlaylistCount: 1 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const row = within(canvasElement).getByTestId('playlists-hidden-broken');
			await expect(row.tagName).toBe('DIV');
			await expect(row).toHaveClass('row', 'child', 'rb-inert');
			await expect(row.textContent).toBe('1 playlist hidden by Broken filter');
			await expect(getComputedStyle(row).opacity).toBe('0.5');
		}
	},
	/** Several: plural copy with the count. */
	Several: {
		args: { hiddenBrokenPlaylistCount: 7 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const row = within(canvasElement).getByTestId('playlists-hidden-broken');
			await expect(row.textContent).toBe('7 playlists hidden by Broken filter');
		}
	},
	/** None hidden: nothing renders at all. */
	None: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { hiddenBrokenPlaylistCount: 0 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(within(canvasElement).queryByTestId('playlists-hidden-broken')).toBeNull();
			await expect(canvasElement.querySelector('.row')).toBeNull();
		}
	}
};
