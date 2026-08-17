import { execFileSync } from 'node:child_process';

export interface WebuiDevConfig {
	backendPort: number;
	frontendPort: number;
	apiProxyTarget: string;
}

type Environment = Record<string, string | undefined>;
type Service = 'backend' | 'frontend' | 'all';

const MIN_PORT = 1024;
const MAX_PORT = 65_535;

const HOSTNAME = /^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)*$/;

/**
 * Hostnames a reverse proxy may present in the Host header, from
 * `MUSIC_DJ_ALLOWED_HOSTS` (comma-separated).
 *
 * Vite 5.4.12+ answers 403 to any Host it was not told about, which is why the
 * tailnet remote runner (Tailscale HTTPS Serve -> vite) 403s under its
 * MagicDNS name while 127.0.0.1 serves fine. Unset means the empty list, which
 * is Vite's own default: loopback only. Wildcards and the blanket `true` are
 * rejected on purpose - they reopen the DNS-rebinding hole the check closes.
 */
export function parseAllowedHosts(rawValue: string | undefined): string[] {
	if (rawValue === undefined || rawValue.trim() === '') {
		return [];
	}
	const hosts = rawValue
		.split(',')
		.map((host) => host.trim())
		.filter((host) => host !== '');
	if (hosts.length === 0) {
		throw new Error('MUSIC_DJ_ALLOWED_HOSTS was set but named no hostname');
	}
	for (const host of hosts) {
		if (!HOSTNAME.test(host)) {
			throw new Error(
				`MUSIC_DJ_ALLOWED_HOSTS must list bare hostnames, got ${JSON.stringify(host)}`
			);
		}
	}
	return hosts;
}

/**
 * Resolve `MUSIC_DJ_ALLOWED_HOSTS` from its two sources: an explicit shell value
 * wins, and the repository root `.env` is the fallback.
 *
 * Vite does not put the root `.env` on `process.env` at config time, so a value
 * living only there is invisible unless the caller loads it and passes it in.
 * `??` rather than `||` is deliberate: an empty shell value is an explicit
 * "loopback only" and must not silently fall through to the file.
 */
export function resolveAllowedHosts(shellEnv: Environment, rootEnv: Environment): string[] {
	return parseAllowedHosts(shellEnv.MUSIC_DJ_ALLOWED_HOSTS ?? rootEnv.MUSIC_DJ_ALLOWED_HOSTS);
}

export function claimAndCheckWebuiDevConfig(
	repositoryRoot: string,
	service: Service
): WebuiDevConfig {
	const moduleArgs = ['run', '--no-sync', 'python', '-m', 'apps.webui.port_config'];
	const runCommand = (commandArgs: string[]): string =>
		execFileSync('uv', [...moduleArgs, ...commandArgs, '--json'], {
			cwd: repositoryRoot,
			encoding: 'utf8',
			stdio: ['ignore', 'pipe', 'inherit']
		});

	parseWebuiDevConfigPayload(runCommand(['claim']));
	return parseWebuiDevConfigPayload(runCommand(['check', '--service', service]));
}

function requirePortValue(value: unknown, name: string): number {
	if (typeof value !== 'number' || !Number.isSafeInteger(value)) {
		throw new Error(`${name} must be an integer`);
	}
	if (value < MIN_PORT || value > MAX_PORT) {
		throw new Error(`${name} must be between ${MIN_PORT} and ${MAX_PORT}, got ${value}`);
	}
	return value;
}

export function parseWebuiDevConfigPayload(payload: string): WebuiDevConfig {
	let parsed: unknown;
	try {
		parsed = JSON.parse(payload);
	} catch (error) {
		throw new Error(`invalid JSON port configuration: ${error instanceof Error ? error.message : error}`);
	}
	if (typeof parsed !== 'object' || parsed === null || Array.isArray(parsed)) {
		throw new Error('port configuration JSON must be an object');
	}
	const { backend, frontend, api_proxy_target: apiProxyTarget } = parsed as Record<string, unknown>;
	const backendPort = requirePortValue(backend, 'backend');
	const frontendPort = requirePortValue(frontend, 'frontend');
	if (backendPort === frontendPort) {
		throw new Error('backend and frontend ports must be different');
	}
	const expectedApiProxyTarget = `http://127.0.0.1:${backendPort}`;
	if (apiProxyTarget !== expectedApiProxyTarget) {
		throw new Error(
			`api_proxy_target must equal ${JSON.stringify(expectedApiProxyTarget)}, got ${JSON.stringify(apiProxyTarget)}`
		);
	}
	return { backendPort, frontendPort, apiProxyTarget };
}

function requirePort(environment: Environment, name: string): number {
	const rawValue = environment[name];
	if (rawValue === undefined || rawValue.trim() === '') {
		throw new Error(`${name} is required in the worktree root .env`);
	}
	if (!/^[0-9]+$/.test(rawValue)) {
		throw new Error(`${name} must be an integer, got ${JSON.stringify(rawValue)}`);
	}
	return requirePortValue(Number(rawValue), name);
}

export function resolveWebuiDevConfig(environment: Environment): WebuiDevConfig {
	const backendPort = requirePort(environment, 'MUSIC_DJ_BACKEND_PORT');
	const frontendPort = requirePort(environment, 'MUSIC_DJ_FRONTEND_PORT');
	if (backendPort === frontendPort) {
		throw new Error('backend and frontend ports must be different');
	}
	return {
		backendPort,
		frontendPort,
		apiProxyTarget: `http://127.0.0.1:${backendPort}`
	};
}
