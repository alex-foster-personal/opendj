import type { Meta, StoryObj } from '@storybook/sveltekit';

import { contracts } from './TelemetryConsentOverlay.contract';
import TelemetryConsentOverlay from './TelemetryConsentOverlay.svelte';

// Literal title (Storybook's indexer reads it statically); the twin uses the same one.
const meta = { title: 'Contracts/Telemetry/TelemetryConsentOverlay', component: TelemetryConsentOverlay } satisfies Meta<typeof TelemetryConsentOverlay>;
export default meta;
type Story = StoryObj<typeof meta>;

export const Accept: Story = contracts.Accept;
export const DeclineNoReplay: Story = contracts.DeclineNoReplay;
export const SaveFails: Story = contracts.SaveFails;
export const Closed: Story = contracts.Closed;
