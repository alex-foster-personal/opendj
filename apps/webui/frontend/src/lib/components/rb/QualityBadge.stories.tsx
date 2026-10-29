import type { Meta, StoryObj } from '@storybook/react-vite';

import { contracts } from './QualityBadge.contract';
import QualityBadge from './QualityBadge';

/**
 * Reference story for the numeric-readout rule - see the "Numeric readouts
 * carry hover titles" decision doc. Every badge below puts its measured
 * numbers (kbps, rung N of M) in the `title`, never on screen alone.
 *
 * Shapes mirror apps/shared/audio_quality.py, the backend that actually
 * measures these. Nothing here is a mock of a network call: QualityBadge is
 * pure props, so the args ARE the contract. The args and the `play`
 * assertions live in QualityBadge.contract.ts, shared with the Svelte original
 * (QualityBadge.stories.ts), which keeps this same literal title.
 */
const meta = {
	title: 'Components/QualityBadge',
	component: QualityBadge,
	tags: ['autodocs'],
	argTypes: {
		showContainer: { control: 'boolean' }
	}
} satisfies Meta<typeof QualityBadge>;

export default meta;
type Story = StoryObj<typeof meta>;

/** Top rung: lossless, so the rung comes from format not bitrate. */
export const Stadium: Story = contracts.Stadium;

export const Club: Story = contracts.Club;

/** Bottom rung: the badge is meant to look wrong at a glance. */
export const NaughtyStep: Story = contracts.NaughtyStep;

/**
 * venue null: the backend could not measure it. The badge says Unknown and
 * the hover carries the backend's REASON. It never guesses a rung, because
 * a guessed rung would be mocked data.
 */
export const Unknown: Story = contracts.Unknown;
export const RankMissing: Story = contracts.RankMissing;

/** quality null: the row exists but its audio quality has not loaded yet. */
export const NotLoaded: Story = contracts.NotLoaded;

/** Tight rows drop the container suffix; the hover still carries it. */
export const WithoutContainer: Story = contracts.WithoutContainer;

/** The track table's tight column: the rung's initial, the rest on hover. */
export const Compact: Story = contracts.Compact;

/** QLTSCAN-05: a scanned letter; the spectrum made this FLAC a C. */
export const GradeChecked: Story = contracts.GradeChecked;

/** Not scanned yet: the bitrate letter, hollow. */
export const GradeUnchecked: Story = contracts.GradeUnchecked;

/** An issue adds a dot; it does not move the letter. */
export const GradeWithIssues: Story = contracts.GradeWithIssues;
