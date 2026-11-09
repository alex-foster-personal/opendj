import type { Meta, StoryObj } from '@storybook/sveltekit';
import { http, HttpResponse } from 'msw';
import { expect, waitFor, within } from 'storybook/test';

import type { FolderScan } from '$lib/setup/setup-api';

import ConfiguredMusicFolders from './ConfiguredMusicFolders.svelte';

/**
 * Settings > configured music folders. The component reads
 * GET /api/v1/setup/folders, then scans each folder with
 * GET /api/v1/setup/detect/folder. No recording of these endpoints exists in
 * src/sandbox/recorded/, so the bodies are synthesized here and typed against
 * the API schema (FolderScanOut).
 */
const FIVE = ['/music/Incoming', '/Volumes/Crates/Promos', '/music/House', '/music/Techno', '/music/Edits'];

function scanOf(path: string): FolderScan {
	return {
		path,
		exists: true,
		readable: true,
		denied: false,
		detail: '',
		how_to_grant: '',
		audio_files: 10 + path.length,
		icloud_placeholders: 0,
		sample: [`${path}/track-01.mp3`]
	};
}

function foldersHandlers(folders: string[]) {
	return [
		http.get('/api/v1/setup/folders', () => HttpResponse.json({ folders })),
		http.get('/api/v1/setup/detect/folder', ({ request }) =>
			HttpResponse.json(scanOf(new URL(request.url).searchParams.get('path') ?? ''))
		)
	];
}

const meta = {
	title: 'Contracts/Settings/ConfiguredMusicFolders',
	component: ConfiguredMusicFolders,
	tags: ['autodocs']
} satisfies Meta<typeof ConfiguredMusicFolders>;

export default meta;
type Story = StoryObj<typeof meta>;

function folderInputs(root: HTMLElement): HTMLInputElement[] {
	return [...root.querySelectorAll<HTMLInputElement>('input[aria-label="Folder to import"]')];
}

/** Nothing configured: one blank row, which cannot be removed. */
export const Empty: Story = {
	parameters: { msw: { handlers: foldersHandlers([]) } },
	play: async ({ canvasElement }) => {
		const canvas = within(canvasElement);
		await waitFor(() => expect(canvas.getByRole('button', { name: 'Remove folder' })).toBeDisabled());
		const inputs = folderInputs(canvasElement);
		await expect(inputs).toHaveLength(1);
		await expect(inputs[0].value).toBe('');
		await expect(canvasElement.querySelectorAll('p.counts')).toHaveLength(0);
		await expect(canvas.queryByText(/Could not load/)).toBeNull();
		await expect(canvas.getByRole('button', { name: 'Save music folders' })).toBeVisible();
	}
};

/** One configured folder, scanned. */
export const OneFolder: Story = {
	parameters: { msw: { handlers: foldersHandlers([FIVE[0]]) } },
	play: async ({ canvasElement }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('p.counts')).toHaveLength(1));
		await expect(folderInputs(canvasElement).map((input) => input.value)).toEqual([FIVE[0]]);
		await expect(within(canvasElement).getByRole('button', { name: 'Save music folders' })).toBeVisible();
	}
};

/** Five configured folders, in order, each scanned. */
export const FiveFolders: Story = {
	parameters: { msw: { handlers: foldersHandlers(FIVE) } },
	play: async ({ canvasElement }) => {
		await waitFor(() => expect(canvasElement.querySelectorAll('p.counts')).toHaveLength(5));
		await expect(folderInputs(canvasElement).map((input) => input.value)).toEqual(FIVE);
		await expect(within(canvasElement).getByRole('button', { name: 'Save music folders' })).toBeVisible();
	}
};
