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
