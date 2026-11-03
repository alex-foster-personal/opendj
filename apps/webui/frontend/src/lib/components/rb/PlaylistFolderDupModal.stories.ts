import type { Meta, StoryObj } from '@storybook/sveltekit';
import { fn } from 'storybook/test';

import type { UploadFileResult } from '$lib/rb/api-ingest';

import PlaylistFolderDupModal from './PlaylistFolderDupModal.svelte';

/**
 * Pure props. The rows are invented upload results; Accept as new / Skip only
 * record a per-file decision inside the modal, and Continue hands the map to
 * `ondone` (recorded in the Actions panel). No upload endpoint is called.
 * Files left undecided default to Skip on Continue.
 */
const row = (filename: string, over: Partial<UploadFileResult> = {}): UploadFileResult => ({
	filename,
	staged_path: `/music/staging/${filename}`,
	skipped_duplicate: false,
	verdict: 'possible_duplicate',
	duplicate_of: {
		stable_id: 'e1f2a3b4c5d6e7f8a9b0c1d2e3f4a5b6c7d8e9f0',
		title: 'Night Drive (Extended Mix)',
		artist: 'Lumen Fields',
		method: 'chromaprint',
		score: 0.97
	},
	duration_s: 412,
	fingerprint_method: 'chromaprint',
	unchecked_library_tracks: [],
	...over
});

const meta = {
	title: 'Components/PlaylistFolderDupModal',
	component: PlaylistFolderDupModal,
	tags: ['autodocs'],
	argTypes: { busy: { control: 'boolean' } },
	args: { busy: false, ondone: fn(), oncancel: fn() }
} satisfies Meta<typeof PlaylistFolderDupModal>;

export default meta;
type Story = StoryObj<typeof meta>;

/** One file matched a library track by audio fingerprint. */
export const OneDuplicate: Story = {
	args: { rows: [row('Lumen Fields - Night Drive (Extended Mix).flac')] }
};

/** Mixed methods: a fingerprint score, a duration-only match with no score, and an untitled match shown by id prefix. */
export const MixedMatches: Story = {
	args: {
		rows: [
			row('Lumen Fields - Night Drive (Extended Mix).flac'),
			row('Glass Harbor - Low Tide.mp3', {
				duplicate_of: {
					stable_id: '0a1b2c3d4e5f60718293a4b5c6d7e8f901234567',
					title: 'Low Tide',
					artist: 'Glass Harbor',
					method: 'duration',
					score: null
				},
				fingerprint_method: 'duration'
			}),
			row('untitled-take-7.wav', {
				duplicate_of: {
					stable_id: '9f8e7d6c5b4a39281706f5e4d3c2b1a099887766',
					title: null,
					artist: null,
					method: 'chromaprint',
					score: 0.88
				}
			})
		]
	}
};

/** Busy: every action button is disabled while the parent finishes a request. */
export const Busy: Story = {
	args: { busy: true, rows: [row('Lumen Fields - Night Drive (Extended Mix).flac')] }
};

/** Long filenames: the list wraps inside the 520px modal and scrolls past 70vh. */
export const ManyLongNames: Story = {
	args: {
		rows: Array.from({ length: 12 }, (_, i) =>
			row(`Lumen Fields - Night Drive (Extended Mix) - Remastered Session Take ${i + 1} - Studio Master 24bit 96kHz.flac`)
		)
	}
};
