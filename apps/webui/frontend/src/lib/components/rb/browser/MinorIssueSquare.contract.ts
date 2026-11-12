import { expect, within } from 'storybook/test';

// Structural copy of MinorIssue (lib/rb/track-minor-issues.ts): importing that
// module, even for a type, drags pane-contract.svelte.ts and its runes into tsc.
type MinorIssue = {
	id:
		| 'not-on-cloud'
		| 'low-quality'
		| 'missing-genre'
		| 'stems-failed'
		| 'remote-stems-not-run'
		| 'no-lyrics';
	label: string;
};

/**
 * MinorIssueSquare UI contract, shared by the Svelte story (MinorIssueSquare.stories.ts)
 * and its React twin (MinorIssueSquare.stories.tsx). See src/react/README.md.
 */

type Args = { issues: MinorIssue[] };
type Ctx = { canvasElement: HTMLElement; args: Args };

export const contracts = {
	/** Several issues: one square, its title and aria-label join every label with '; '. */
	Several: {
		args: {
			issues: [
				{ id: 'not-on-cloud', label: 'Not on CloudSync' },
				{ id: 'missing-genre', label: 'Missing genre' }
			]
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const square = within(canvasElement).getByTestId('minor-issue-square');
			await expect(square).toHaveClass('minor-issue-square');
			await expect(square).toHaveAttribute('title', 'Not on CloudSync; Missing genre');
			await expect(square).toHaveAttribute('aria-label', 'Not on CloudSync; Missing genre');
			const svg = square.querySelector('svg');
			await expect(svg).toHaveAttribute('aria-hidden', 'true');
			await expect(svg).toHaveAttribute('width', '9');
			await expect(square.querySelector('rect.mis-fill')).not.toBeNull();
			await expect(square.querySelector('path.mis-ink')).not.toBeNull();
		}
	},
	/** One issue: its label alone. */
	One: {
		args: { issues: [{ id: 'no-lyrics', label: 'No lyrics' }] } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(within(canvasElement).getByTestId('minor-issue-square')).toHaveAttribute('title', 'No lyrics');
		}
	},
	/** No issues: nothing renders. */
	None: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { issues: [] } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(within(canvasElement).queryByTestId('minor-issue-square')).toBeNull();
			await expect(canvasElement.querySelector('svg')).toBeNull();
		}
	}
};
