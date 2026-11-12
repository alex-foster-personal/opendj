import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import { SECTION_OVERLAY_PREF_DEFAULTS } from '$lib/rb/section-overlay-prefs';
import { paintSectionsPreview } from '$lib/settings/sections-preview-paint';

import SectionDisplayPreviewView from './SectionDisplayPreviewView.svelte';

/** The segment display preview canvas, painted from explicit prefs (props only). */
const meta = {
	title: 'Settings/SectionDisplayPreviewView',
	component: SectionDisplayPreviewView,
	tags: ['autodocs'],
	args: {
		paint: paintSectionsPreview,
		prefs: SECTION_OVERLAY_PREF_DEFAULTS,
		label: 'Segment display preview'
	}
} satisfies Meta<typeof SectionDisplayPreviewView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Default prefs render the labelled preview canvas. */
export const Default: Story = {
	play: async ({ canvasElement }) => {
		const root = canvasElement.querySelector('[aria-label="Segment display preview"]');
		await expect(root).not.toBeNull();
		await expect(root!.querySelector('canvas')).not.toBeNull();
	}
};
