import { expect, within } from 'storybook/test';

/**
 * BannerWarning UI contract, shared by the Svelte story (BannerWarning.stories.ts)
 * and its React twin (BannerWarning.stories.tsx). See src/react/README.md.
 */

type Args = { message: string };
type Ctx = { canvasElement: HTMLElement; args: Args };

export const contracts = {
	/** The message renders as the whole text of one .banner-warning alert. */
	ReadOnly: {
		args: { message: 'Library is read-only: rekordbox is running' } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const alert = within(canvasElement).getByRole('alert');
			await expect(alert).toHaveClass('banner-warning');
			await expect(alert.tagName).toBe('DIV');
			await expect(alert.textContent).toBe(args.message);
		}
	},
	/** A different message replaces the text verbatim, with no extra whitespace. */
	DaemonDown: {
		args: { message: 'Daemon unreachable - retrying' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const alerts = within(canvasElement).getAllByRole('alert');
			await expect(alerts).toHaveLength(1);
			await expect(alerts[0].textContent).toBe('Daemon unreachable - retrying');
		}
	}
};
