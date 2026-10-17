import type { Meta, StoryObj } from '@storybook/sveltekit';
import { http, HttpResponse } from 'msw';
import { expect, waitFor, within } from 'storybook/test';

import { silentTrackAudio } from '../../sandbox/media';
import { recordedTrackId, replay } from '../../sandbox/recorded';
import LyricsPanel from './LyricsPanel.svelte';

/**
 * LyricsPanel UI contracts. The panel fetches through the shared lyrics cache
 * and probes the track's audio, so these stories answer those requests with
 * daemon responses RECORDED from the lyrics-words fixture library
 * (src/sandbox/recorded/, see src/sandbox/recordings.json). Any other /api/
 * request fails the story.
 */
const VOCAL = recordedTrackId('lyrics-words-vocal-track');
const WORDLESS = recordedTrackId('lyrics-words-wordless-track');
/** Not a real track: the panel's lyrics cache is per stable_id and module
 * wide, so the failure story needs an id no other story has loaded. */
const BROKEN = 'sandbox-daemon-500';

const meta = {
	title: 'Contracts/Lyrics/LyricsPanel',
	component: LyricsPanel
} satisfies Meta<typeof LyricsPanel>;

export default meta;
type Story = StoryObj<typeof meta>;

/** A track with aligned words shows its verdict, evidence and every word,
 * each word titled with what the ASR witness said about it. */
export const VocalTrack: Story = {
	args: { stableId: VOCAL },
	parameters: {
		msw: {
			handlers: [
				replay('lyrics-words-vocal-track'),
				replay('audio-head-vocal-track'),
				silentTrackAudio(VOCAL)
			]
		}
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await waitFor(() => expect(canvasElement.querySelector('.qc b')).toHaveTextContent('Vocal'));
		await expect(canvas.getByText('92.5% vocal coverage')).toBeVisible();
		await expect(canvas.getByText(/40 words\s*\/ 7 lines/)).toBeVisible();
		const words = canvasElement.querySelectorAll('p.words button');
		await expect(words).toHaveLength(40);
		await expect(words[0]).toHaveTextContent('we');
		await expect(words[0]).toHaveAttribute(
			'title',
			'2.00s - the independent ASR witness heard this word at this time'
		);
		// The audio probe answered 200, so a player is offered. Its
		// preload="metadata" GET is awaited too, so it cannot land in the next
		// story's window instead.
		await waitFor(() =>
			expect(canvasElement.querySelector('audio')?.readyState).toBeGreaterThanOrEqual(1)
		);
	}
};

/** A track with a cached line transcript but no aligned words still shows
 * its lyrics: the words route answers the recorded empty payload, so the
 * panel falls back to the line cache and draws each recorded line. */
export const LineCacheOnly: Story = {
	args: { stableId: WORDLESS },
	parameters: {
		msw: {
			handlers: [
				replay('lyrics-words-wordless-track'),
				replay('lyrics-lines-wordless-track'),
				replay('audio-head-wordless-track'),
				silentTrackAudio(WORDLESS)
			]
		}
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(await canvas.findByText(/^Zorvik, Plimba, Tavrex/)).toBeVisible();
		await expect(await canvas.findByText('neon signs above the river bend')).toBeVisible();
		await expect(canvas.queryByText(/No karaoke lyric data/)).toBeNull();
		// The audio probe answered 200, so a player is offered. Its
		// preload="metadata" GET is answered here and awaited, so it cannot land
		// in the next story's window instead.
		await waitFor(() =>
			expect(canvasElement.querySelector('audio')?.readyState).toBeGreaterThanOrEqual(1)
		);
	}
};

/** A daemon failure is stated, never shown as "no lyrics". The 500 body is
 * synthesized in the backend's error envelope, not recorded. */
export const DaemonFailureIsStated: Story = {
	args: { stableId: BROKEN },
	parameters: {
		msw: {
			handlers: [
				http.get(`/api/v1/tracks/${BROKEN}/lyrics/words`, () =>
					HttpResponse.json(
						{ detail: { code: 'internal', message: 'state.db is locked' } },
						{ status: 500 }
					)
				),
				http.head(`/api/v1/tracks/${BROKEN}/audio`, () => new HttpResponse(null, { status: 404 }))
			]
		}
	},
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await expect(
			await canvas.findByText(/Lyrics could not be read for this track/)
		).toBeVisible();
		await expect(canvas.queryByText(/No karaoke lyric data/)).toBeNull();
	}
};
