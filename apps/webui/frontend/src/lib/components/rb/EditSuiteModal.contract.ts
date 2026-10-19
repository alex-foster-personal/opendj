import { expect, fireEvent, fn, userEvent, within } from 'storybook/test';

/**
 * EditSuiteModal UI contract, shared by the Svelte story
 * (EditSuiteModal.stories.ts) and its React twin (EditSuiteModal.stories.tsx).
 * The body content is framework-specific (a Svelte snippet, a React node), so
 * each stories file passes the same `<p class="es-demo">` child in its meta
 * args; everything asserted here is shared. See src/react/README.md.
 */

type Args = { title: string; onclose: () => void };
type Ctx = { canvasElement: HTMLElement; args: Args };

export const DEMO_BODY_TEXT = 'Replace "feat." with "ft." in 12 titles';

export const contracts = {
	/** The panel is a titled modal dialog around the child; the x button and the backdrop close it, the panel does not. */
	ClickToClose: {
		args: { title: 'Find and replace', onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const page = within(canvasElement.ownerDocument.body);
			const dialog = page.getByRole('dialog', { name: 'Find and replace' });
			await expect(dialog).toHaveAttribute('aria-modal', 'true');
			await expect(dialog).toHaveAttribute('tabindex', '-1');
			await expect(dialog).toHaveClass('es-panel');
			await expect(within(dialog).getByRole('heading', { level: 2 })).toHaveTextContent('Find and replace');
			await expect(dialog.querySelector('.es-body p.es-demo')).toHaveTextContent(DEMO_BODY_TEXT);
			await userEvent.click(within(dialog).getByText(DEMO_BODY_TEXT));
			await expect(args.onclose).not.toHaveBeenCalled();
			const close = within(dialog).getByRole('button', { name: 'close' });
			await expect(close).toHaveTextContent('x');
			await userEvent.click(close);
			await expect(args.onclose).toHaveBeenCalledTimes(1);
			const overlay = canvasElement.querySelector('.es-overlay') as HTMLElement;
			await expect(overlay).toHaveAttribute('role', 'presentation');
			fireEvent.click(overlay);
			await expect(args.onclose).toHaveBeenCalledTimes(2);
		}
	},
	/** Escape anywhere in the window closes; any other key does not. */
	EscapeToClose: {
		args: { title: 'Bulk edit', onclose: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await expect(within(canvasElement).getByRole('dialog', { name: 'Bulk edit' })).toBeVisible();
			await userEvent.keyboard('a');
			await expect(args.onclose).not.toHaveBeenCalled();
			await userEvent.keyboard('{Escape}');
			await expect(args.onclose).toHaveBeenCalledTimes(1);
		}
	}
};
