import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createElement, useEffect, type FunctionComponent } from 'react';
import { expect, waitFor } from 'storybook/test';

import ReactIsland from './ReactIsland.svelte';

// REACT-01: ReactIsland mounts a React twin inside the Svelte tree, which is how
// Deck.svelte renders its leaf controls when `ui.react.deck` is on. These run in
// real Chromium: the island must load react-dom, mount the twin's real DOM in
// its `display: contents` host, and hand the props through.
const meta = {
	title: 'Contracts/React/ReactIsland',
	component: ReactIsland
} satisfies Meta<typeof ReactIsland>;
export default meta;
type Story = StoryObj<typeof meta>;

type Ctx = { canvasElement: HTMLElement };
type IslandArgs = { load: () => Promise<{ default: FunctionComponent<object> }>; props: object };

/** One meta types every story's args with the island's default generic; the
 *  twin and its props are checked against each other here instead. */
function islandArgs<P extends object>(load: () => Promise<{ default: FunctionComponent<P> }>, props: P): IslandArgs {
	return { load: load as unknown as IslandArgs['load'], props };
}

const host = (root: HTMLElement): HTMLElement | null => root.querySelector('[data-react-island]');

/** The twin with no props mounts its own DOM inside the island host. */
export const MountsTwin: Story = {
	args: islandArgs(() => import('$lib/components/rb/deck/PadStrip.tsx'), {}),
	play: async ({ canvasElement }: Ctx) => {
		const island = host(canvasElement);
		await expect(island).not.toBeNull();
		await expect(getComputedStyle(island!).display).toBe('contents');
		await waitFor(() => expect(island!.querySelector('.pad-strip')).not.toBeNull());
		await expect(island!.querySelectorAll('.pad-letter')).toHaveLength(8);
	}
};

/** Props reach the twin: a loading badge shows its phase in title and data attribute. */
export const PassesProps: Story = {
	args: islandArgs(() => import('$lib/components/rb/deck/SecondaryLoadBadge.tsx'), {
		status: 'loading', error: null, phase: 'decode'
	}),
	play: async ({ canvasElement }: Ctx) => {
		const island = host(canvasElement);
		await expect(island).not.toBeNull();
		await waitFor(() => expect(island!.querySelector('[data-testid="deck-secondary-load"]')).not.toBeNull());
		const badge = island!.querySelector('[data-testid="deck-secondary-load"]') as HTMLElement;
		await expect(badge).toHaveTextContent(/^STEMS$/);
		await expect(badge).toHaveAttribute('data-stem-phase', 'decode');
	}
};

// Set by the settled badge's wrapper once React has committed it, so the
// empty-host assertion below runs after a proven mount, not after a guess.
let settledCommitted = false;

/** Negative control: a settled badge renders nothing, so the host stays empty. */
export const SettledRendersNothing: Story = {
	args: islandArgs(async () => {
		settledCommitted = false;
		const { default: Badge } = await import('$lib/components/rb/deck/SecondaryLoadBadge.tsx');
		type BadgeProps = Parameters<typeof Badge>[0];
		// A wrapper with no DOM of its own: its effect runs only after React
		// commits the real badge, which leaves the host empty when settled.
		function Committed(props: BadgeProps) {
			useEffect(() => {
				settledCommitted = true;
			}, []);
			return createElement(Badge, props);
		}
		return { default: Committed };
	}, { status: 'ready', error: null, phase: null }),
	play: async ({ canvasElement }: Ctx) => {
		const island = host(canvasElement);
		await expect(island).not.toBeNull();
		await waitFor(() => expect(settledCommitted).toBe(true));
		await expect(island!.childElementCount).toBe(0);
	}
};

/** A twin that cannot load shows the failure in place, never a silently empty control. */
export const FailsLoud: Story = {
	args: islandArgs<object>(() => Promise.reject(new Error('twin chunk missing')), {}),
	play: async ({ canvasElement }: Ctx) => {
		await waitFor(() => expect(canvasElement.querySelector('[data-react-island-failed]')).not.toBeNull());
		const failed = canvasElement.querySelector('[data-react-island-failed]') as HTMLElement;
		await expect(failed).toHaveAttribute('role', 'alert');
		await expect(failed).toHaveAttribute('title', 'React twin failed to load: twin chunk missing');
	}
};
