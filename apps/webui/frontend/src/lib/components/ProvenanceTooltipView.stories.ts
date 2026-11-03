import type { Meta, StoryObj } from '@storybook/sveltekit';
import { createRawSnippet } from 'svelte';

import ProvenanceTooltipView from './ProvenanceTooltipView.svelte';

const text = (value: string) => createRawSnippet(() => ({ render: () => `<span>${value}</span>` }));

/**
 * The track page's provenance hover: when the container found a provenance
 * record for the field it passes the formatted title and the value is
 * wrapped in a span carrying it; otherwise the value renders bare. Hover the
 * text in the first story for the title.
 */
const meta = {
	title: 'Components/ProvenanceTooltipView',
	component: ProvenanceTooltipView,
	tags: ['autodocs']
} satisfies Meta<typeof ProvenanceTooltipView>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A field with a provenance record: source, confidence and modified time on hover. */
export const WithProvenance: Story = {
	args: {
		title: 'rekordbox · conf 0.92 · 2026-10-12T21:30:00Z',
		children: text('Night Drive (Extended Mix)')
	}
};

/** A record with no confidence reads as '?'. */
export const UnknownConfidence: Story = {
	args: {
		title: 'tags · conf ? · 2026-09-01T10:00:00Z',
		children: text('Lumen Fields')
	}
};

/** No provenance for the field: the value renders with no hover. */
export const NoProvenance: Story = {
	args: { title: null, children: text('Quiet Kiln') }
};
