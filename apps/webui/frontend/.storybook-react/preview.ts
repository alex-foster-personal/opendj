import type { Preview } from '@storybook/react-vite';
import { initialize, mswLoader } from 'msw-storybook-addon';

import '../src/app.css';
import '../src/lib/rb/theme.css';
import '../.storybook/preview.css';
import { failOnUnhandledApi, onUnhandledApiRequest, resetUnhandledApi } from '../.storybook/unhandled-api';

// Same frame as the Svelte preview (.storybook/preview.ts), so a twin and
// its original render under identical CSS and identical request rules.
if (typeof document !== 'undefined') {
	document.body.classList.add('perf-root');
}

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
	parameters: { controls: { expanded: true } }
};

export default preview;
