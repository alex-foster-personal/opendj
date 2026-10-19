import { expect } from 'storybook/test';

/**
 * PerfMeterSpark UI contract, shared by the Svelte story (PerfMeterSpark.stories.ts)
 * and its React twin (PerfMeterSpark.stories.tsx). See src/react/README.md.
 */

type Args = { values: Array<number | null>; label?: string; title?: string };
type Ctx = { canvasElement: HTMLElement };

const lines = (root: HTMLElement): SVGPolylineElement[] =>
	Array.from(root.querySelectorAll<SVGPolylineElement>('svg.perf-spark-svg polyline'));

export const contracts = {
	/** Label and hover title shown; a null gap becomes one dashed segment, never a zero. */
	Labeled: {
		args: { values: [1, 2, null, 4, 3], label: 'CPU', title: 'CPU percent, last 5 samples' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const root = canvasElement.querySelector('div.perf-spark');
			await expect(root).toHaveAttribute('title', 'CPU percent, last 5 samples');
			await expect(canvasElement.querySelector('span.perf-spark-label')).toHaveTextContent('CPU');
			await expect(canvasElement.querySelector('svg.perf-spark-svg')).toHaveAttribute('aria-hidden', 'true');
			const segs = lines(canvasElement);
			await expect(segs).toHaveLength(3);
			await expect(segs.map((s) => s.getAttribute('stroke-dasharray'))).toEqual([null, '3 2', null]);
			for (const s of segs) {
				await expect(s).toHaveAttribute('fill', 'none');
				await expect(s).toHaveAttribute('stroke', 'currentColor');
				await expect(s).toHaveAttribute('stroke-width', '1.5');
			}
		}
	},
	/** No label and no title: no label span, no title attribute; a single sample draws nothing. */
	Bare: {
		// Story parity: the end state draws nothing visible (EMPTY, not BLANK).
		tags: ['flat-frame'],
		args: { values: [5] } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('div.perf-spark')).not.toHaveAttribute('title');
			await expect(canvasElement.querySelector('span.perf-spark-label')).toBeNull();
			await expect(lines(canvasElement)).toHaveLength(0);
		}
	},
	/** An empty title string is treated as no title. */
	EmptyTitle: {
		args: { values: [3, 1, 2], title: '' } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			await expect(canvasElement.querySelector('div.perf-spark')).not.toHaveAttribute('title');
			await expect(lines(canvasElement)).toHaveLength(2);
		}
	}
};
