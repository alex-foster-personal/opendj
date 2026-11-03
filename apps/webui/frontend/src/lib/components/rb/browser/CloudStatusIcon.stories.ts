import type { Meta, StoryObj } from '@storybook/sveltekit';
import { expect } from 'storybook/test';

import CloudStatusIcon from './CloudStatusIcon.svelte';
import { contracts } from './CloudStatusIcon.contract';
import { trackCloudView, type TrackCloudInput } from './track-cloud-state';

/**
 * Views come from the real pure `trackCloudView` function, fed invented
 * inputs, so each story shows exactly what the library row would derive.
 */
const view = (over: Partial<TrackCloudInput>) =>
	trackCloudView({
		fileExists: true,
		isStreaming: false,
		hasRemoteCopy: false,
		spotifyPending: false,
		transfer: null,
		filePath: '/music/Lumen Fields/Night Drive (Extended Mix).flac',
		...over
	});

const meta = {
	title: 'Contracts/Browser/CloudStatusIcon',
	component: CloudStatusIcon,
	tags: ['autodocs'],
	args: { view: view({}) }
} satisfies Meta<typeof CloudStatusIcon>;
export default meta;
type Story = StoryObj<typeof meta>;

export const NotOnCloud: Story = contracts.NotOnCloud;
export const OnCloudNotLocalGreenTick: Story = contracts.OnCloudNotLocalGreenTick;
export const StreamingSpotify: Story = contracts.StreamingSpotify;
export const StreamingSoundcloud: Story = contracts.StreamingSoundcloud;

/** Local file only, not synced: blue tick overlay. */
export const LocalOnly: Story = {
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
	}
};

/** On the cloud and on this machine: green tick overlay. */
export const CloudAndLocal: Story = { args: { view: view({ hasRemoteCopy: true }) },
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
	}
};

/** On the cloud but not downloaded. */
export const CloudNotLocal: Story = {
	args: { view: view({ hasRemoteCopy: true, fileExists: false, fileAvailability: 'absent' }) },
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
	}
};

/** A download in flight with a known byte total. */
export const Downloading: Story = {
	args: {
		view: view({
			hasRemoteCopy: true,
			fileExists: false,
			transfer: { direction: 'download', bytesTransferred: 12_000_000, bytesTotal: 48_000_000 }
		})
	},
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
		await expect(args.view.title).toContain('25%');
	}
};

/** An upload with an unknown total: no percentage is invented. */
export const UploadUnknownTotal: Story = {
	args: {
		view: view({ transfer: { direction: 'upload', bytesTransferred: 2_500_000, bytesTotal: null } })
	},
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
		await expect(args.view.title).not.toContain('%');
	}
};

/** Streaming-service rows take the provider brand color (view helper). */
export const StreamingSpotifyView: Story = {
	args: { view: view({ isStreaming: true, fileExists: false, provider: 'spotify' }) },
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
		await expect(canvasElement.querySelectorAll('.spotify-arc').length).toBeGreaterThan(0);
	}
};

export const StreamingSoundCloudView: Story = {
	args: { view: view({ isStreaming: true, fileExists: false, provider: 'soundcloud' }) },
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
		await expect(canvasElement.querySelector('.soundcloud')).not.toBeNull();
	}
};

export const StreamingTidalView: Story = {
	args: { view: view({ isStreaming: true, fileExists: false, provider: 'tidal' }) },
	play: async ({ canvasElement, args }) => {
		await expect(canvasElement.querySelector('.cloud-status-icon')).toHaveAttribute('aria-label', args.view.title);
		await expect(canvasElement.querySelector('.tidal')).not.toBeNull();
	}
};
