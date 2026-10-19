import { expect, fn, userEvent, within } from 'storybook/test';

/**
 * BrowserConfirmDialog UI contract, shared by the Svelte story
 * (BrowserConfirmDialog.stories.ts) and its React twin
 * (BrowserConfirmDialog.stories.tsx). See src/react/README.md.
 */

type Opts = { remember: boolean; setDefault: boolean };
type Args = {
	open?: boolean;
	title: string;
	message: string;
	primaryLabel?: string;
	secondaryLabel?: string;
	showRemember?: boolean;
	rememberLabel?: string;
	showDefault?: boolean;
	defaultLabel?: string;
	onPrimary: (opts: Opts) => void;
	onSecondary?: (opts: Opts) => void;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

export const contracts = {
	/** Open and untouched by any button: both boxes toggle in place and the dialog stays up. */
	Open: {
		args: {
			open: true,
			title: 'Move to trash',
			message: 'Move 2 tracks to the trash?',
			showDefault: true,
			onPrimary: fn(),
			onSecondary: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const boxes = canvas.getAllByRole('checkbox');
			await expect(boxes.map((b) => (b as HTMLInputElement).checked)).toEqual([false, false]);
			await userEvent.click(boxes[0]);
			await expect(boxes[0]).toBeChecked();
			await userEvent.click(canvas.getByText("Don't show this again"));
			await expect(boxes[1]).toBeChecked();
			await userEvent.click(boxes[0]);
			await expect(boxes[0]).not.toBeChecked();
			await expect(canvas.getByRole('dialog', { name: 'Move to trash' })).toBeVisible();
			await expect(args.onPrimary).not.toHaveBeenCalled();
			await expect(args.onSecondary).not.toHaveBeenCalled();
		}
	},
	/** Default labels and the remember box; primary reports the box and closes the dialog. */
	Defaults: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: {
			open: true,
			title: 'Remove from playlist',
			message: 'Remove 3 tracks from "Warmup"?\nThe files stay in the collection.',
			onPrimary: fn(),
			onSecondary: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const dialog = canvas.getByRole('dialog', { name: 'Remove from playlist' });
			await expect(dialog).toHaveAttribute('aria-modal', 'true');
			await expect(dialog.querySelector('.browser-confirm-title')).toHaveTextContent('Remove from playlist');
			await expect(dialog.querySelector('.browser-confirm-msg')?.textContent).toBe(
				'Remove 3 tracks from "Warmup"?\nThe files stay in the collection.'
			);
			await expect(canvas.getAllByRole('checkbox')).toHaveLength(1);
			await userEvent.click(canvas.getByRole('checkbox', { name: "Don't show this again" }));
			await expect(canvas.getByRole('button', { name: 'Cancel' })).toHaveClass('browser-confirm-secondary');
			await userEvent.click(canvas.getByRole('button', { name: 'OK' }));
			await expect(args.onPrimary).toHaveBeenCalledWith({ remember: true, setDefault: false });
			await expect(args.onSecondary).not.toHaveBeenCalled();
			await expect(canvas.queryByRole('dialog')).toBeNull();
		}
	},
	/** Custom labels with the default box only; secondary reports it and closes. */
	DefaultOnlySecondary: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: {
			open: true,
			title: 'Load to deck',
			message: 'Deck 1 is playing.',
			primaryLabel: 'Load anyway',
			secondaryLabel: 'Keep playing',
			showRemember: false,
			showDefault: true,
			defaultLabel: 'Always do this',
			onPrimary: fn(),
			onSecondary: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			await expect(canvas.getAllByRole('checkbox')).toHaveLength(1);
			await expect(canvas.queryByRole('checkbox', { name: "Don't show this again" })).toBeNull();
			await userEvent.click(canvas.getByRole('checkbox', { name: 'Always do this' }));
			await userEvent.click(canvas.getByRole('button', { name: 'Keep playing' }));
			await expect(args.onSecondary).toHaveBeenCalledWith({ remember: false, setDefault: true });
			await expect(args.onPrimary).not.toHaveBeenCalled();
			await expect(canvas.queryByRole('dialog')).toBeNull();
		}
	},
	/** Both boxes, both checked: primary reports both. */
	BothBoxes: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: {
			open: true,
			title: 'Delete',
			message: 'Delete the playlist?',
			showDefault: true,
			onPrimary: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const boxes = canvas.getAllByRole('checkbox');
			await expect(boxes).toHaveLength(2);
			await expect(boxes[0]).toHaveAccessibleName('Default to this');
			await expect(boxes[1]).toHaveAccessibleName("Don't show this again");
			await userEvent.click(boxes[0]);
			await userEvent.click(boxes[1]);
			await userEvent.click(canvas.getByRole('button', { name: 'OK' }));
			await expect(args.onPrimary).toHaveBeenCalledWith({ remember: true, setDefault: true });
		}
	},
	/** Closed renders nothing at all. */
	Closed: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { open: false, title: 'Hidden', message: 'Nothing to see', onPrimary: fn() } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('.browser-confirm')).toBeNull();
			await expect(within(canvasElement).queryByText('Nothing to see')).toBeNull();
		}
	}
};
