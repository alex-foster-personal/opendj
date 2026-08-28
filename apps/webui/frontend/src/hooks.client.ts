import type { HandleClientError } from '@sveltejs/kit';

import { installClientErrorReporting, reportClientError } from '$lib/client-error-reporting';
import { installVisitorTelemetry } from '$lib/client-telemetry';

installClientErrorReporting();
installVisitorTelemetry();

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
