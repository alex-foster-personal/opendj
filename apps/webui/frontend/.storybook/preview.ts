import type { Preview } from '@storybook/sveltekit';

import '../src/app.css';
import '../src/lib/rb/theme.css';
import './preview.css';

/**
 * theme.css scopes every --rb-* var under `.perf-root` ON PURPOSE: the
 * app-wide :root accent is orange and must not change (RECON-FRONTEND 6).
 * So anything under lib/components/rb renders unstyled without that class,
 * and wave/render.ts `readPalette` throws outright when a var resolves
 * empty. The real app carries the class on the /performance route wrapper;
 * the preview iframe carries it on <body> so every story inherits it.
 */
if (typeof document !== 'undefined') {
	document.body.classList.add('perf-root');
}

const preview: Preview = {
	parameters: {
		controls: { expanded: true },
		docs: { toc: true }
	}
};

export default preview;
