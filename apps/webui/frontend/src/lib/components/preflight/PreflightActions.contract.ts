import { expect, fn, userEvent, within } from 'storybook/test';

import {
	preflightExplainers,
	type PreflightActionId,
	type PreflightExplainer
} from '$lib/preflight/preflight-actions';

/**
 * PreflightActions UI contract, shared by the Svelte story
 * (PreflightActions.stories.ts) and its React twin (PreflightActions.stories.tsx).
 * See src/react/README.md.
 */

type Args = {
	actions: PreflightActionId[];
	disabled?: PreflightActionId[];
	pending?: PreflightActionId | null;
	status?: string | null;
	footer?: string | null;
	explainers: PreflightExplainer[];
	onAction?: (id: PreflightActionId) => unknown;
};
type Ctx = { canvasElement: HTMLElement; args: Args };

const ALL: PreflightActionId[] = ['import', 'recheck', 'permissions'];
const button = (root: HTMLElement, id: PreflightActionId): HTMLButtonElement =>
	within(root).getByTestId(`preflight-action-${id}`) as HTMLButtonElement;

export const contracts = {
	/** Labels in order, one disabled by id, a click reports its id, status and footer render, explainers list each button. */
	Idle: {
		args: {
			actions: ALL,
			disabled: ['permissions'],
			pending: null,
			status: 'Re-check finished: 6 of 7 checks pass.',
			footer: 'The engine retries on its own every 5 seconds.',
			explainers: preflightExplainers({ actions: ALL, runSetup: true }, null),
			onAction: fn()
		} satisfies Args,
		play: async ({ canvasElement, args }: Ctx) => {
			const canvas = within(canvasElement);
			const buttons = canvasElement.querySelectorAll('.preflight-actions button.preflight-action');
			await expect(Array.from(buttons, (b) => b.textContent?.trim())).toEqual([
				'Import your music',
				'Re-check',
				'Re-request permissions'
			]);
			await expect(button(canvasElement, 'import')).toHaveClass('preflight-import');
			await expect(button(canvasElement, 'recheck')).not.toHaveClass('preflight-import');
			await expect(button(canvasElement, 'import')).toHaveAttribute('aria-busy', 'false');
			await expect(button(canvasElement, 'import')).toHaveAttribute('type', 'button');
			await expect(button(canvasElement, 'import')).toBeEnabled();
			await expect(button(canvasElement, 'permissions')).toBeDisabled();
			for (const b of buttons) await expect(b).not.toHaveAttribute('title');
			await userEvent.click(button(canvasElement, 'recheck'));
			await expect(args.onAction).toHaveBeenCalledWith('recheck');
			await userEvent.click(button(canvasElement, 'permissions'));
			await expect(args.onAction).toHaveBeenCalledTimes(1);
			const status = canvas.getByTestId('preflight-action-status');
			await expect(status).toHaveAttribute('role', 'status');
			await expect(status).toHaveAttribute('aria-live', 'polite');
			await expect(status).toHaveTextContent('Re-check finished: 6 of 7 checks pass.');
			await expect(canvasElement.querySelector('p.note')).toHaveTextContent(
				'The engine retries on its own every 5 seconds.'
			);
			const items = canvas.getByTestId('preflight-explainers').querySelectorAll('li');
			await expect(items).toHaveLength(4);
			await expect(items[0].querySelector('strong')).toHaveTextContent('Run setup');
			await expect(items[1].textContent).toBe('Import your music: opens setup to import rekordbox or a music folder.');
		}
	},
	/** Import pending: its label switches, it is busy and dimmed, and every button is disabled; no footer. */
	ImportPending: {
		args: {
			actions: ALL,
			pending: 'import',
			explainers: preflightExplainers({ actions: ALL, runSetup: false }, null),
			onAction: fn()
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const imp = button(canvasElement, 'import');
			await expect(imp).toHaveTextContent('Opening setup...');
			await expect(imp).toHaveClass('pending');
			await expect(imp).toHaveAttribute('aria-busy', 'true');
			for (const id of ALL) await expect(button(canvasElement, id)).toBeDisabled();
			await expect(button(canvasElement, 'recheck')).not.toHaveClass('pending');
			await expect(canvasElement.querySelector('p.note')).toBeNull();
			await expect(within(canvasElement).getByTestId('preflight-action-status').textContent?.trim()).toBe('');
		}
	},
	/** A non-import action pending keeps its own label; a blocked import explains why. */
	RecheckPendingBlocked: {
		args: {
			actions: ['import', 'recheck'],
			pending: 'recheck',
			disabled: ['import'],
			explainers: preflightExplainers({ actions: ['import', 'recheck'], runSetup: false }, 'engine offline'),
			onAction: fn()
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(button(canvasElement, 'recheck')).toHaveTextContent('Re-check');
			await expect(button(canvasElement, 'recheck')).toHaveAttribute('aria-busy', 'true');
			await expect(button(canvasElement, 'import')).toHaveTextContent('Import your music');
			const items = within(canvasElement).getByTestId('preflight-explainers').querySelectorAll('li');
			await expect(items[0].textContent).toBe('Import your music: unavailable: engine offline');
		}
	}
};
