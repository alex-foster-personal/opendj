import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import VirtualList from './VirtualList.svelte';

/**
 * The parent owns the items and slices them itself: the snippet receives
 * [startIndex, endIndex) and renders those rows. Row height must match the
 * rendered CSS height exactly, so every row below is a fixed 20px.
 */
const rows = (rowHeight: number) =>
	createRawSnippet((start: () => number, end: () => number) => ({
		// The scroll container is flex:1 inside a sized parent in the app; here no
		// parent sizes it, so give it a fixed height (a plain-props story cannot
		// wrap the component in a frame).
		render: () =>
			`<div><style>.vlist-scroll{height:240px;border:1px solid var(--rb-border, #444)}</style><div></div></div>`,
		// render() runs once, before the list has measured its viewport, and a
		// raw snippet does not re-render when its arguments change. So the rows
		// are filled here: once the list has measured (next frame), and on every
		// scroll, reading the window the component has just computed.
		setup: (node) => {
			const host = node.lastElementChild as HTMLElement;
			const scroller = node.closest('.vlist-scroll');
			const fill = () => {
				const out: string[] = [];
				for (let i = start(); i < end(); i++) {
					out.push(
						`<div style="height:${rowHeight}px;line-height:${rowHeight}px;padding:0 8px;box-sizing:border-box;color:var(--rb-text);border-bottom:1px solid var(--rb-border, transparent)">Night Drive (Extended Mix) - Lumen Fields #${i + 1}</div>`
					);
				}
				host.innerHTML = out.join('');
			};
			const frame = requestAnimationFrame(fill);
			scroller?.addEventListener('scroll', fill);
			return () => {
				cancelAnimationFrame(frame);
				scroller?.removeEventListener('scroll', fill);
			};
		}
	}));

const meta = {
	title: 'Components/Browser/VirtualList',
	component: VirtualList,
	tags: ['autodocs', 'capture-canvas-width'],
	parameters: { layout: 'padded' }
} satisfies Meta<typeof VirtualList>;

export default meta;
type Story = StoryObj<typeof meta>;

/** 10,000 rows: only the visible window (plus overscan) is in the DOM. */
export const LargeList: Story = {
	args: { itemCount: 10000, rowHeight: 20, children: rows(20) }
};

export const SmallList: Story = {
	args: { itemCount: 5, rowHeight: 20, children: rows(20) }
};

/** Zero items: just an empty scroll container. */
export const Empty: Story = {
	args: { itemCount: 0, rowHeight: 20, children: rows(20) }
};

/** Taller rows with minimal overscan. */
export const TallRowsLowOverscan: Story = {
	args: { itemCount: 2000, rowHeight: 32, overscan: 2, children: rows(32) }
};
