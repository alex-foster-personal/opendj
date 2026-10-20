import { expect } from 'storybook/test';

/**
 * FaderRejoinGuidance UI contract, shared by the Svelte story
 * (FaderRejoinGuidance.stories.ts) and its React twin
 * (FaderRejoinGuidance.stories.tsx). See src/react/README.md.
 */

type Args = { fromY: number; toY: number; trackWidth: number };
type Ctx = { canvasElement: HTMLElement };

const svg = (root: HTMLElement): SVGSVGElement => root.querySelector('svg.rejoin-guidance') as SVGSVGElement;
const line = (root: HTMLElement): SVGLineElement => root.querySelector('svg.rejoin-guidance line') as SVGLineElement;

export const contracts = {
	/** A short gap (24px or less) draws a solid arrow at 35% of the track width. */
	Short: {
		args: { fromY: 50, toY: 70, trackWidth: 40 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(svg(canvasElement)).toHaveAttribute('aria-hidden', 'true');
			await expect(svg(canvasElement)).toHaveAttribute('width', '40');
			await expect(svg(canvasElement)).toHaveAttribute('height', '100%');
			const l = line(canvasElement);
			await expect(l).toHaveAttribute('x1', '14');
			await expect(l).toHaveAttribute('x2', '14');
			await expect(l).toHaveAttribute('y1', '50');
			await expect(l).toHaveAttribute('y2', '70');
			await expect(l).toHaveAttribute('stroke', '#e6c200');
			await expect(l).toHaveAttribute('stroke-width', '1.5');
			await expect(l).not.toHaveAttribute('stroke-dasharray');
			await expect(l).toHaveAttribute('marker-end', 'url(#rejoin-arrowhead)');
			await expect(canvasElement.querySelector('marker#rejoin-arrowhead')).not.toBeNull();
		}
	},
	/** A long gap (more than 24px, either direction) is dotted. */
	Long: {
		args: { fromY: 120, toY: 20, trackWidth: 60 } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const l = line(canvasElement);
			await expect(l).toHaveAttribute('x1', '21');
			await expect(l).toHaveAttribute('y1', '120');
			await expect(l).toHaveAttribute('y2', '20');
			await expect(l).toHaveAttribute('stroke-dasharray', '3 2');
		}
	}
};
