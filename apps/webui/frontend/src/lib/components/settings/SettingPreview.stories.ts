import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect, waitFor } from 'storybook/test';

import type { SettingPreview as SettingPreviewKind } from '$lib/settings/catalog-types';

import SettingPreview from './SettingPreview.svelte';

/**
 * The one place a settings row's live preview is chosen. One story per
 * preview kind: the STORY_PER_KIND map below fails type-check when a kind is
 * added to the union without a story. The container reads prefs, so this is a
 * contract story. The canvas spans its container, so the root is 360px wide.
 */
const meta = {
	title: 'Contracts/Settings/SettingPreview',
	component: SettingPreview,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root = typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) root.style.width = '360px';
			return story();
		}
	]
} satisfies Meta<typeof SettingPreview>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Count of pixels the painter drew (alpha above zero): zero means a blank preview. */
function paintedPixelCount(canvas: HTMLCanvasElement): number {
	const ctx = canvas.getContext('2d');
	if (ctx === null || canvas.width === 0 || canvas.height === 0) return 0;
	const data = ctx.getImageData(0, 0, canvas.width, canvas.height).data;
	let painted = 0;
	for (let i = 3; i < data.length; i += 4) if (data[i] > 0) painted++;
	return painted;
}

/** The waveform design preview paints the sample track, never a blank canvas. */
export const Waveform: Story = {
	args: { kind: 'waveform', label: 'Waveform design preview' },
	play: async ({ canvasElement, args }) => {
		const canvas = canvasElement.querySelector<HTMLCanvasElement>(`canvas[aria-label="${args.label}"]`);
		await expect(canvas).not.toBeNull();
		await expect(canvas!.getAttribute('data-design')).toBeTruthy();
		await waitFor(() => expect(paintedPixelCount(canvas!)).toBeGreaterThan(0), { timeout: 5000 });
	}
};

/** The segment display preview paints the sample sections, never a blank canvas. */
export const Sections: Story = {
	args: { kind: 'sections', label: 'Segment display preview' },
	play: async ({ canvasElement, args }) => {
		const root = canvasElement.querySelector<HTMLElement>(`[aria-label="${args.label}"]`);
		await expect(root).not.toBeNull();
		const canvas = root!.querySelector<HTMLCanvasElement>('canvas');
		await expect(canvas).not.toBeNull();
		await waitFor(() => expect(paintedPixelCount(canvas!)).toBeGreaterThan(0), { timeout: 5000 });
	}
};

const STORY_PER_KIND = { waveform: Waveform, sections: Sections } satisfies Record<SettingPreviewKind, Story>;
void STORY_PER_KIND;
