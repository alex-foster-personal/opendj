import type { Meta, StoryObj } from '@storybook/sveltekit';

import { resolveWaveBandColors } from '$lib/rb/wave-palette';
import { previewSampleWaveform } from '$lib/settings/waveform-preview-sample';
import { paintWaveformPreview } from '$lib/settings/waveform-preview-paint';

import WaveformDesignPreviewView from './WaveformDesignPreviewView.svelte';

/**
 * The settings waveform thumbnail. The container resolves the effective
 * design and palette from prefs and the skin, then lazily loads the sample;
 * here they are args. The sample is the repo's own checked-in preview
 * capture (waveform-preview-sample.ts), not a fetch. The canvas spans its
 * container, so the story root is set to 360px wide.
 */
const sample = previewSampleWaveform();

const meta = {
	title: 'Components/Settings/WaveformDesignPreviewView',
	component: WaveformDesignPreviewView,
	tags: ['autodocs'],
	decorators: [
		(story) => {
			const root = typeof document !== 'undefined' ? document.getElementById('storybook-root') : null;
			if (root) root.style.width = '360px';
			return story();
		}
	],
	args: {
		paint: paintWaveformPreview,
		active: 'tri-band',
		palette: 'rekordbox',
		colors: resolveWaveBandColors('dark', 'rekordbox'),
		sample
	}
} satisfies Meta<typeof WaveformDesignPreviewView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** The default look: three bands in the rekordbox palette. */
export const TriBand: Story = {};

/** One mono band. */
export const Mono: Story = {
	args: { active: 'mono', palette: 'mono', colors: resolveWaveBandColors('dark', 'mono') }
};

/** The outline design. */
export const Line: Story = { args: { active: 'line' } };

/** Blocks in the legacy palette. */
export const Blocks: Story = {
	args: { active: 'blocks', palette: 'legacy', colors: resolveWaveBandColors('dark', 'legacy') }
};

/** Sample chunk not loaded yet: the dark fill only. */
export const SamplePending: Story = { args: { sample: null } };
