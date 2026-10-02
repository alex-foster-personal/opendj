/**
 * Endpoints for vocals demucs overlay e2e (#274).
 */
export const VOCALS_DEMUCS_OVERLAY_BACKEND_PORT = 8705;
export const VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT = 5278;

const RESERVED = new Set([
	4455, 4456, 5214, 5216, 5273, 5277, 5311, 5320, 5321, 5322, 5323, 5324, 5326, 5399, 5173,
	8585, 8682, 8685, 8686, 8688, 8690, 8691, 8692, 8695, 8696, 8697, 8698, 8699, 8700, 8703,
	8704, 9402, 9405, 9408, 9414, 9473
]);

export interface VocalsDemucsOverlayEndpoints {
	backendOrigin: string;
	frontendOrigin: string;
	backendPort: number;
	frontendPort: number;
}

function requirePort(name: string, fallback: number): number {
	const raw = process.env[name];
	const value = raw === undefined || raw.trim() === '' ? fallback : Number(raw);
	if (!Number.isSafeInteger(value) || value < 1024 || value > 65_535) {
		throw new Error(`${name} must be a port between 1024 and 65535, got ${raw}`);
	}
	if (RESERVED.has(value)) {
		throw new Error(`${name}=${value} is claimed by another lane; pick another`);
	}
	return value;
}

export function resolveEndpoints(): VocalsDemucsOverlayEndpoints {
	const backendPort = requirePort(
		'VOCALS_DEMUCS_OVERLAY_BACKEND_PORT',
		VOCALS_DEMUCS_OVERLAY_BACKEND_PORT
	);
	const frontendPort = requirePort(
		'VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT',
		VOCALS_DEMUCS_OVERLAY_FRONTEND_PORT
	);
	if (backendPort === frontendPort) {
		throw new Error('vocals demucs overlay backend and frontend ports must differ');
	}
	return {
		backendPort,
		frontendPort,
		backendOrigin: `http://127.0.0.1:${backendPort}`,
		frontendOrigin: `http://127.0.0.1:${frontendPort}`
	};
}
