import { expect, fn, userEvent, waitFor, within } from 'storybook/test';

/**
 * LibraryEditMenu UI contract, shared by the Svelte story
 * (LibraryEditMenu.stories.ts) and its React twin (LibraryEditMenu.stories.tsx),
 * both titled 'Contracts/Browser/LibraryEditMenu'. See src/react/README.md.
 *
 * The list is ALWAYS mounted (the LESS view hides items through
 * data-less-hidden and must find them), and only shown while open.
 */

type Action = 'find-replace' | 'bulk-edit' | 'mytag';
type Args = { hasSelection: boolean; onpick: (action: Action) => void };
type Ctx = { canvasElement: HTMLElement; args: Args };

const TRIGGER_TITLE = 'Edit tags - Find & Replace and Bulk Edit for the selected tracks, and MyTags';
const NEEDS_SELECTION = 'Select one or more tracks first';

const wrap = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>('.library-edit-menu')!;
const trigger = (root: HTMLElement): HTMLButtonElement =>
	root.querySelector<HTMLButtonElement>('[data-testid="library-edit-menu"]')!;
const list = (root: HTMLElement): HTMLElement => root.querySelector<HTMLElement>('[data-testid="library-edit-menu-list"]')!;
const items = (root: HTMLElement): HTMLButtonElement[] =>
	Array.from(list(root).querySelectorAll<HTMLButtonElement>('button[role="menuitem"]'));

export const contracts = {
	/** Closed: the pencil trigger, collapsed and titled; the list is mounted but hidden. */
	Closed: {
		args: { hasSelection: true, onpick: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const t = trigger(canvasElement);
			await expect(t).toHaveAccessibleName('Edit tags');
			await expect(t).toHaveClass('rb-lit-button', 'edit-trigger');
			await expect(t).toHaveAttribute('aria-haspopup', 'menu');
			await expect(t).toHaveAttribute('aria-expanded', 'false');
			await expect(t).toHaveAttribute('title', TRIGGER_TITLE);
			await expect(wrap(canvasElement)).not.toHaveAttribute('data-custom-tip');
			await expect(list(canvasElement)).not.toBeVisible();
			await expect(list(canvasElement)).not.toHaveClass('open');
			await expect(list(canvasElement)).toHaveAttribute('aria-label', 'Edit tags');
			await expect(list(canvasElement)).toHaveAttribute('data-hover-card', '');
			await expect(items(canvasElement).map((b) => b.textContent?.trim())).toEqual([
				'Find & Replace',
				'Bulk Edit',
				'MyTags'
			]);
			await expect(args.onpick).not.toHaveBeenCalled();
		}
	},
	/** With a selection: every item is live; a pick forwards its action and closes; reopened it sits fixed below the pencil. */
	WithSelection: {
		args: { hasSelection: true, onpick: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const t = trigger(canvasElement);
			await userEvent.click(t);
			await expect(t).toHaveAttribute('aria-expanded', 'true');
			await expect(wrap(canvasElement)).toHaveAttribute('data-custom-tip', '');
			const menu = within(canvasElement).getByRole('menu', { name: 'Edit tags' });
			await expect(menu).toBeVisible();
			await expect(menu).toHaveClass('edit-menu-list', 'open');
			for (const item of items(canvasElement)) {
				await expect(item).toBeEnabled();
				await expect(item).not.toHaveAttribute('title');
			}
			await expect(items(canvasElement)[0]).toHaveAttribute('data-less-hidden', '');
			await expect(items(canvasElement)[1]).toHaveAttribute('data-less-hidden', '');
			await expect(items(canvasElement)[2]).not.toHaveAttribute('data-less-hidden');
			await userEvent.click(within(menu).getByRole('menuitem', { name: 'Bulk Edit' }));
			await expect(args.onpick).toHaveBeenLastCalledWith('bulk-edit');
			await expect(list(canvasElement)).not.toBeVisible();
			await expect(t).toHaveAttribute('aria-expanded', 'false');

			await userEvent.click(t);
			await userEvent.click(within(menu).getByRole('menuitem', { name: 'Find & Replace' }));
			await expect(args.onpick).toHaveBeenLastCalledWith('find-replace');

			await userEvent.click(t);
			await waitFor(() => expect(list(canvasElement).style.position).toBe('fixed'));
			const r = t.getBoundingClientRect();
			await expect(list(canvasElement).style.top).toBe(`${Math.round(r.top + r.height + 4)}px`);
			await expect(list(canvasElement).style.left).toBe(`${Math.round(r.left)}px`);
		}
	},
	/** No selection: the two selection edits are inert with the reason; MyTags still picks. */
	NoSelection: {
		args: { hasSelection: false, onpick: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			await userEvent.click(trigger(canvasElement));
			const [findReplace, bulkEdit, myTags] = items(canvasElement);
			for (const item of [findReplace, bulkEdit]) {
				await expect(item).toBeDisabled();
				await expect(item).toHaveAttribute('title', NEEDS_SELECTION);
			}
			await expect(myTags).toBeEnabled();
			await expect(myTags).not.toHaveAttribute('title');
			await userEvent.click(myTags);
			await expect(args.onpick).toHaveBeenCalledTimes(1);
			await expect(args.onpick).toHaveBeenCalledWith('mytag');
			await expect(list(canvasElement)).not.toBeVisible();
		}
	},
	/** Escape closes and returns focus to the pencil; an outside press closes; the pencil toggles. */
	Dismiss: {
		args: { hasSelection: true, onpick: fn() } satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const t = trigger(canvasElement);
			await userEvent.click(t);
			items(canvasElement)[2].focus();
			await userEvent.keyboard('{Escape}');
			await expect(list(canvasElement)).not.toBeVisible();
			await expect(t).toHaveFocus();

			await userEvent.click(t);
			await expect(list(canvasElement)).toBeVisible();
			await userEvent.click(canvasElement.ownerDocument.body);
			await expect(list(canvasElement)).not.toBeVisible();

			await userEvent.click(t);
			await userEvent.click(t);
			await expect(list(canvasElement)).not.toBeVisible();
			await expect(args.onpick).not.toHaveBeenCalled();
		}
	}
};
