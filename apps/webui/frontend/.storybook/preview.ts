import type { Preview } from '@storybook/sveltekit';
import { initialize, mswLoader } from 'msw-storybook-addon';

import '../src/app.css';
import '../src/lib/rb/theme.css';
import './preview.css';
import { failOnUnhandledApi, onUnhandledApiRequest, resetUnhandledApi } from './unhandled-api';

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

// A DAEMON request no story handler answers is an ERROR, not a silent
// passthrough: a contract that quietly hits a real daemon (or nothing) proves
// nothing, and fails the story (./unhandled-api.ts). Everything else (Vite
// module and asset requests, which also cross
// the service worker) passes through; erroring on those breaks every lazily
// imported module.
// STORYBOOK_MSW=off builds a static copy for hosts with no service workers
// (a shared preview page); stories that need recorded responses then fail
// loudly instead of the whole Storybook failing to render.
const mswOn = import.meta.env.STORYBOOK_MSW !== 'off';

if (mswOn) initialize({
	quiet: true,
	onUnhandledRequest: onUnhandledApiRequest
});

const preview: Preview = {
	loaders: mswOn ? [mswLoader] : [],
	beforeEach: resetUnhandledApi,
	afterEach: failOnUnhandledApi,
	parameters: {
		controls: { expanded: true },
		docs: { toc: true }
	}
};

export default preview;
