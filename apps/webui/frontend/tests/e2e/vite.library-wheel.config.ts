import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';

export default defineConfig({
	plugins: [sveltekit()],
	optimizeDeps: { exclude: ['signalsmith-stretch'] },
	server: {
		host: '127.0.0.1',
		port: 5228,
		strictPort: true,
		proxy: { '/api': 'http://127.0.0.1:9428' }
	}
});
