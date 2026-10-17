import react from '@vitejs/plugin-react';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';

/**
 * Vite config for the React twins only (.storybook-react and the
 * storybook-react vitest project). It deliberately does NOT load the
 * SvelteKit plugin: twins are plain React plus framework-neutral TS, and
 * `$lib` is the one SvelteKit alias they may use.
 */
export default defineConfig({
	plugins: [react()],
	resolve: {
		alias: { $lib: fileURLToPath(new URL('./src/lib', import.meta.url)) }
	}
});
