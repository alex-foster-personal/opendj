import type { HandleClientError } from '@sveltejs/kit';

import { installClientErrorReporting, reportClientError } from '$lib/client-error-reporting';

installClientErrorReporting();

export const handleError: HandleClientError = ({ error, event, message, status }) => {
	reportClientError(
		error,
		{
			source: 'sveltekit',
			fallback_message: message,
			route: event.url.pathname,
			status
		},
		'sveltekit'
	);
	return { message };
};
