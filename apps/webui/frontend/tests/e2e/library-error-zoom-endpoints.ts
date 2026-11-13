/**
 * Fixed loopback endpoints for library search error zoom e2e (issue #12949).
 */
export const LIBRARY_ERROR_ZOOM_BACKEND_PORT = 8708;
export const LIBRARY_ERROR_ZOOM_FRONTEND_PORT = 5280;

export interface LibraryErrorZoomEndpoints {
	backendOrigin: string;
	frontendOrigin: string;
	backendPort: number;
	frontendPort: number;
}

export function resolveLibraryErrorZoomEndpoints(): LibraryErrorZoomEndpoints {
	const backendPort = LIBRARY_ERROR_ZOOM_BACKEND_PORT;
	const frontendPort = LIBRARY_ERROR_ZOOM_FRONTEND_PORT;
	return {
		backendPort,
		frontendPort,
		backendOrigin: `http://127.0.0.1:${backendPort}`,
		frontendOrigin: `http://127.0.0.1:${frontendPort}`
	};
}
