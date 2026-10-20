import { expect } from 'storybook/test';

/**
 * SecondaryLoadBadge UI contract, shared by the Svelte story
 * (SecondaryLoadBadge.stories.ts) and its React twin
 * (SecondaryLoadBadge.stories.tsx), both titled
 * 'Contracts/Deck/SecondaryLoadBadge'. See src/react/README.md.
 *
 * The in-flight pulse is part of the contract (it carries the "still loading"
 * meaning), so the loading stories assert it runs and then pause it at its
 * first frame, which keeps the parity screenshot deterministic.
 */

type Args = { status: string; error?: string | null; phase?: string | null };
type Ctx = { canvasElement: HTMLElement; args: Args };

const badge = (root: HTMLElement): HTMLElement | null => root.querySelector('[data-testid="deck-secondary-load"]');

/** The badge's running CSS animations whose keyframes are the pulse (Svelte prefixes a scope hash). */
const pulses = (el: HTMLElement): Animation[] =>
	el
		.getAnimations()
		.filter((a) => a instanceof CSSAnimation && a.animationName.endsWith('secondary-pulse'));

async function expectPulseThenFreeze(el: HTMLElement): Promise<void> {
	const running = pulses(el);
	await expect(running).toHaveLength(1);
	running[0].pause();
	running[0].currentTime = 0;
}

export const contracts = {
	/** In flight with a known phase: STEMS, the phase in the hover title and data attribute, pulsing. */
	Loading: {
		args: { status: 'loading', error: null, phase: 'decode' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).not.toBeNull();
			await expect(el!).toHaveTextContent(/^STEMS$/);
			await expect(el!).toHaveClass('secondary-load');
			await expect(el!).not.toHaveClass('failed');
			await expect(el!).toHaveAttribute('data-stem-load', 'loading');
			await expect(el!).toHaveAttribute('data-stem-phase', 'decode');
			await expect(el!).toHaveAttribute(
				'title',
				'Loading stems in the background (decode). The deck is already playable - this only gates the VOCAL / INST / DRUMS controls.'
			);
			await expectPulseThenFreeze(el!);
		}
	},
	/** In flight with no phase yet: no phase suffix in the title, no data-stem-phase attribute at all. */
	LoadingNoPhase: {
		args: { status: 'loading', error: null, phase: null } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).not.toBeNull();
			await expect(el!).not.toHaveAttribute('data-stem-phase');
			await expect(el!).toHaveAttribute(
				'title',
				'Loading stems in the background. The deck is already playable - this only gates the VOCAL / INST / DRUMS controls.'
			);
			await expectPulseThenFreeze(el!);
		}
	},
	/** A failed load: STEMS!, the .failed class, the error in the title, and no pulse. */
	Failed: {
		args: { status: 'error', error: 'bundle 404', phase: 'fetch' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = badge(canvasElement);
			await expect(el).not.toBeNull();
			await expect(el!).toHaveTextContent(/^STEMS!$/);
			await expect(el!).toHaveClass('secondary-load', 'failed');
			await expect(el!).toHaveAttribute('data-stem-load', 'error');
			await expect(el!).toHaveAttribute(
				'title',
				'Stems unavailable - bundle 404. The deck plays from its mix; stem controls stay off.'
			);
			await expect(pulses(el!)).toHaveLength(0);
		}
	},
	/** `unavailable` is a settled answer: nothing renders, like `ready`. */
	Settled: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { status: 'unavailable', error: null, phase: null } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(badge(canvasElement)).toBeNull();
			await expect(canvasElement.querySelector('.secondary-load')).toBeNull();
		}
	}
};
