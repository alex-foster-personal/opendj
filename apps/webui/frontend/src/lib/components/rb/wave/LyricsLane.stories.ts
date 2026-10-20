import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './LyricsLane.contract';
import LyricsLane from './LyricsLane.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Wave/LyricsLane', component: LyricsLane } satisfies Meta<typeof LyricsLane>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Playing: Story = contracts.Playing;
export const DoublePitch: Story = contracts.DoublePitch;
export const LoadError: Story = contracts.LoadError;
export const NoLyrics: Story = contracts.NoLyrics;
