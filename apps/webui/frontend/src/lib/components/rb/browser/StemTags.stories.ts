import type { Meta, StoryObj } from '@storybook/sveltekit';

import type { ComponentProps } from 'svelte';

import StemTags from './StemTags.svelte';

/**
 * The browser Stems column cell: [V][I][D] tags (vocals / instruments /
 * drums) plus a short model/format/size readout, with the full detail in the
 * hover title and a ControlExplainer popover. It renders only what the
 * listing hydrate measured, so the args are the whole contract. Sizes are
 * invented.
 */
// The prop's own type, so the story adds no importer to api-rb.ts (the
// quality gate's frontend.max_fan_in module).
type StemSummary = NonNullable<ComponentProps<typeof StemTags>['stems']>;
type Ready = Extract<StemSummary, { status: 'ready' }>;

const ready = (over: Partial<Ready> = {}): StemSummary => ({
	status: 'ready',
	model: 'htdemucs',
	preset: 'balanced',
	overlap: 0.25,
	shifts: 1,
	format: 'flac',
	total_bytes: 96 * 1024 * 1024,
	groups: {
		V: { bytes: 24 * 1024 * 1024, parts: ['vocals'] },
		I: { bytes: 48 * 1024 * 1024, parts: ['bass', 'other'] },
		D: { bytes: 24 * 1024 * 1024, parts: ['drums'] }
	},
	...over
});

const meta = {
	title: 'Components/Browser/StemTags',
	component: StemTags,
	tags: ['autodocs']
} satisfies Meta<typeof StemTags>;

export default meta;
type Story = StoryObj<typeof meta>;

/** All three groups present: V, I and D all lit. */
export const AllReady: Story = { args: { stems: ready() } };

/** Drums group measured at zero bytes: its tag stays dim. */
export const MissingDrums: Story = {
	args: {
		stems: ready({
			groups: {
				V: { bytes: 24 * 1024 * 1024, parts: ['vocals'] },
				I: { bytes: 48 * 1024 * 1024, parts: ['bass', 'other'] },
				D: { bytes: 0, parts: [] }
			}
		})
	}
};

/** Unknown model name is shortened to 8 characters; overlap and shifts appear as ov/sh. */
export const UnknownModel: Story = {
	args: { stems: ready({ model: 'experimental_roformer_large', shifts: 2, overlap: 0.5 }) }
};

/** Row listing has not hydrated stems yet (null): a dash, hover says not loaded. */
export const NotLoaded: Story = { args: { stems: null } };

/** Hydrated and no local stem bundle exists. */
export const None: Story = { args: { stems: { status: 'none' } } };

/** Bundle exists but failed validation; the reason is in the hover title. */
export const Invalid: Story = {
	args: { stems: { status: 'invalid', error: 'drums part missing from bundle manifest' } }
};
