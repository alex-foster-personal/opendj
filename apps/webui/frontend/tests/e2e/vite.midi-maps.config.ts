import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

const frontendPort = Number(process.env.MIDI_MAPS_E2E_FRONTEND_PORT);
const apiOrigin = process.env.MIDI_MAPS_E2E_API_BASE;

if (!Number.isSafeInteger(frontendPort) || apiOrigin === undefined) {
	throw new Error(
		'the midi_maps e2e vite config needs MIDI_MAPS_E2E_FRONTEND_PORT and MIDI_MAPS_E2E_API_BASE'
	);
}

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: {
		exclude: ['signalsmith-stretch']
	},
	server: {
		host: '127.0.0.1',
		port: frontendPort,
		strictPort: true,
		proxy: {
			// ws: true is load-bearing: the suite proves installed-map reload
			// after a real browser WebSocket disconnect/reconnect on /api/v1/events.
			'/api': { target: apiOrigin, changeOrigin: false, ws: true }
		}
	}
});
