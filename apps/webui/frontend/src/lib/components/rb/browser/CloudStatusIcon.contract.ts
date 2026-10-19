import { expect } from 'storybook/test';

import type { TrackCloudView } from './track-cloud-state';

/**
 * CloudStatusIcon UI contract, shared by the Svelte story
 * (CloudStatusIcon.stories.ts) and its React twin (CloudStatusIcon.stories.tsx).
 * See src/react/README.md.
 */

type Args = { view: TrackCloudView };
type Ctx = { canvasElement: HTMLElement; args: Args };

const view = (over: Partial<TrackCloudView>): TrackCloudView => ({
	kind: 'on-cloud-and-local',
	title: 'On cloud and local',
	showIcon: true,
	transfer: null,
	provider: null,
	overlay: 'none',
	...over
});

const icon = (root: HTMLElement): HTMLElement => root.querySelector('.cloud-status-icon') as HTMLElement;

export const contracts = {
	/** Not on cloud: hollow cloud with a slash, class not-on-cloud, title doubles as the label. */
	NotOnCloud: {
		args: { view: view({ kind: 'not-on-cloud', title: 'Not on cloud' }) } satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = icon(canvasElement);
			await expect(el).toHaveClass('not-on-cloud');
			await expect(el).not.toHaveClass('streaming');
			await expect(el).toHaveAttribute('title', 'Not on cloud');
			await expect(el).toHaveAttribute('aria-label', 'Not on cloud');
			const paths = el.querySelectorAll('svg.cloud-icon path');
			await expect(paths).toHaveLength(2);
			await expect(paths[0]).toHaveAttribute('fill', 'none');
			await expect(el.querySelector('.tick-overlay')).toBeNull();
		}
	},
	/** On cloud, not local, with a green tick: solid cloud and the green overlay. */
	OnCloudNotLocalGreenTick: {
		args: {
			view: view({ kind: 'on-cloud-not-local', title: 'On cloud, not on this machine', overlay: 'green-tick' })
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = icon(canvasElement);
			await expect(el).toHaveClass('on-cloud-not-local');
			const paths = el.querySelectorAll('svg.cloud-icon path');
			await expect(paths).toHaveLength(1);
			await expect(paths[0]).toHaveAttribute('fill', 'currentColor');
			await expect(el.querySelector('svg.tick-overlay.tick-green path')).toHaveAttribute('stroke', '#35c04f');
			await expect(el.querySelector('.tick-blue')).toBeNull();
		}
	},
	/** Streaming from Spotify: the provider mark replaces the cloud, plus a blue tick. */
	StreamingSpotify: {
		args: {
			view: view({ kind: 'streaming', title: 'Streaming from Spotify', provider: 'spotify', overlay: 'blue-tick' })
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = icon(canvasElement);
			await expect(el).toHaveClass('streaming');
			await expect(el.querySelector('.cloud-icon')).toBeNull();
			await expect(el.querySelector('svg.provider-icon circle')).toHaveAttribute('fill', '#35c04f');
			await expect(el.querySelectorAll('path.spotify-arc')).toHaveLength(3);
			await expect(el.querySelector('svg.tick-overlay.tick-blue path')).toHaveAttribute('stroke', '#4cc9f0');
		}
	},
	/** Streaming from SoundCloud: its mark takes the orange fill, no overlay. */
	StreamingSoundcloud: {
		args: {
			view: view({ kind: 'streaming', title: 'Streaming from SoundCloud', provider: 'soundcloud' })
		} satisfies Args,
		play: async ({ canvasElement }: Ctx) => {
			const el = icon(canvasElement);
			await expect(el.querySelector('svg.provider-icon path.soundcloud')).toHaveAttribute('fill', '#ff5500');
			await expect(el.querySelector('.tick-overlay')).toBeNull();
			await expect(el).toHaveAttribute('aria-label', 'Streaming from SoundCloud');
		}
	}
};
