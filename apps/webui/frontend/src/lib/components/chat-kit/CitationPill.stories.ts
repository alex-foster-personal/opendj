import type { Meta, StoryObj } from '@storybook/sveltekit';

import CitationPill from './CitationPill.svelte';

const meta = {
	title: 'Components/ChatKit/CitationPill',
	component: CitationPill,
	tags: ['autodocs'],
	args: {
		citation: { label: 'docs.opendj.dev', state: 'verified', href: 'https://docs.opendj.dev' }
	}
} satisfies Meta<typeof CitationPill>;

export default meta;
type Story = StoryObj<typeof meta>;

export const Verified: Story = {
	args: {
		citation: { label: 'docs.opendj.dev', state: 'verified', href: 'https://docs.opendj.dev' }
	}
};

export const Warn: Story = {
	args: {
		citation: { label: 'forum.example', state: 'warn', href: 'https://forum.example/thread' }
	}
};

export const Fail: Story = {
	args: { citation: { label: 'dead link', state: 'fail' } }
};
