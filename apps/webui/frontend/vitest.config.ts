import { sveltekit } from '@sveltejs/kit/vite';
import react from '@vitejs/plugin-react';
import { storybookTest } from '@storybook/addon-vitest/vitest-plugin';
import { playwright } from '@vitest/browser-playwright';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

/**
 * UI contracts: every story under src/ runs as a real-browser test (render
 * smoke plus its `play` function) via Storybook's Vitest addon. This is the
 * agent inner loop -- `pnpm test:contracts src/path/X.stories.ts` mounts the
 * component in Chromium with its fixtures, no daemon, engine or IPC.
 *
 * PW_CHROMIUM_PATH points at an already-installed Chromium when the one
 * matching this Playwright version is not downloaded (cloud boxes ship
 * /opt/pw-browsers/chromium-*); unset means Playwright's own browser.
 */
const chromiumPath = process.env.PW_CHROMIUM_PATH;

// A fresh object per project: vitest writes the project name into each instance.
const browser = () => ({
	enabled: true,
	headless: true,
	provider: playwright(chromiumPath ? { launchOptions: { executablePath: chromiumPath } } : {}),
	instances: [{ browser: 'chromium' as const }]
});

export default defineConfig({
	plugins: [sveltekit()],
	test: {
		projects: [
			{
				extends: true,
				plugins: [
					storybookTest({
						configDir: fileURLToPath(new URL('./.storybook', import.meta.url))
					})
				],
				test: {
					name: 'storybook',
					browser: browser()
				}
			},
			{
				// React twins: same contracts (X.contract.ts), React renderer,
				// no SvelteKit plugin (see vite.react.config.ts).
				plugins: [
					react(),
					storybookTest({
						configDir: fileURLToPath(new URL('./.storybook-react', import.meta.url))
					})
				],
				resolve: {
					alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) }
				},
				test: { name: 'storybook-react', browser: browser() }
			}
		]
	}
});
