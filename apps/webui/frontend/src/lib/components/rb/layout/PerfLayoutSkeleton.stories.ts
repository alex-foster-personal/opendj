/**
 * PerfLayoutSkeleton stories (#10264): one story per PERFORMANCE_LAYOUTS preset
 * at 1280x800 and 1920x1080, built by iterating the catalog so a new preset
 * gets its stories when the named CSF exports are refreshed from the map.
 */
import type { Meta, StoryObj } from '@storybook/sveltekit';

import { PERFORMANCE_LAYOUTS, type PerformanceLayout } from '$lib/rb/skin-layout';

import PerfLayoutSkeleton from './PerfLayoutSkeleton.svelte';

export const PERF_LAYOUT_SKELETON_VIEWPORTS = [
	{ w: 1280, h: 800 },
	{ w: 1920, h: 1080 }
] as const;

const meta = {
	title: 'Layout/PerfLayoutSkeleton',
	component: PerfLayoutSkeleton,
	parameters: { layout: 'fullscreen' },
	// Helper exports are for unit tests, not stories (they have no args).
	excludeStories: ['PERF_LAYOUT_SKELETON_VIEWPORTS', 'perfLayoutSkeletonStories'],
	tags: ['autodocs']
} satisfies Meta<typeof PerfLayoutSkeleton>;

export default meta;
type Story = StoryObj<typeof meta>;

function viewportParams(w: number, h: number, storyName: string) {
	return {
		layout: 'fullscreen' as const,
		viewport: {
			defaultViewport: storyName,
			viewports: {
				[storyName]: {
					name: storyName,
					styles: { width: `${w}px`, height: `${h}px` }
				}
			}
		}
	};
}

function makeStory(preset: PerformanceLayout, w: number, h: number): Story {
	const storyName = `${preset}-${w}x${h}`;
	return {
		name: storyName,
		args: { preset, widthPx: w, heightPx: h },
		parameters: viewportParams(w, h, storyName)
	};
}

/** Catalog x viewports. Unit tests assert this map; CSF named exports below mirror it. */
export const perfLayoutSkeletonStories: Readonly<Record<string, Story>> = Object.fromEntries(
	PERFORMANCE_LAYOUTS.flatMap((preset) =>
		PERF_LAYOUT_SKELETON_VIEWPORTS.map(({ w, h }) => {
			const story = makeStory(preset, w, h);
			return [story.name as string, story] as const;
		})
	)
);

// CSF named exports for Storybook's static indexer (mirrors perfLayoutSkeletonStories keys).
export const DeckGrid1280x800: Story = perfLayoutSkeletonStories['deck-grid-1280x800'];
export const DeckGrid1920x1080: Story = perfLayoutSkeletonStories['deck-grid-1920x1080'];
export const ReplicaDesk1280x800: Story = perfLayoutSkeletonStories['replica-desk-1280x800'];
export const ReplicaDesk1920x1080: Story = perfLayoutSkeletonStories['replica-desk-1920x1080'];
