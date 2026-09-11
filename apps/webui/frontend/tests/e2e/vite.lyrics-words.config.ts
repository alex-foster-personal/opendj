/**
 * Vite dev server for the lyrics-words e2e suite (#2079). Proxies /api to
 * the real backend booted by playwright.lyrics-words.config.ts against a
 * throwaway fixture library with one words-seeded track and one wordless
 * track. No tone-fixture plugin needed: this suite never measures audio.
 *
 * Ports are fixed and explicit, never one of the reserved lane ports listed
 * in playwright.webkit-deckload.config.ts or the other gate configs.
 * ``strictPort`` refuses to drift onto a neighbour's port instead of silently
 * succeeding on the wrong one.
 */
import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export const LYRICS_WORDS_FRONTEND_PORT = 5328;
export const LYRICS_WORDS_API_PORT = 8706;

export default defineConfig({
	plugins: [sveltekit()],
	server: {
		host: '127.0.0.1',
		port: LYRICS_WORDS_FRONTEND_PORT,
		strictPort: true,
		proxy: {
			'/api': {
				target: `http://127.0.0.1:${LYRICS_WORDS_API_PORT}`,
				changeOrigin: false
			}
		}
	}
});
