// Loopback health surface owned by the desktop shell, not the engine.
//
// Port of apps/desktop/src-tauri/src/shell_health.rs. Agents and the bootstrap
// page read `.engine.shell.json` for the port, then ask here when the engine
// port is dead or stale (INSTALL-23).

import * as fs from 'node:fs';
import * as http from 'node:http';
import * as path from 'node:path';

import { OUTPUT_HEALTH_PATH, SWITCH_OUTPUT_PATH, type OutputAction } from './output-health';
import { utcTimestampIso } from './shell-log';

export const HEALTH_PATH = '/api/v1/health';
export const RELAUNCH_PATH = '/api/v1/relaunch';
export const SHELL_LOCK_FILE = '.engine.shell.json';

export interface ShellHealthSnapshot {
	status: string;
	engine: string;
	lock_pid: number | null;
	lock_port: number | null;
	exit_code: number | null;
	reason: string | null;
}

export const DEFAULT_SNAPSHOT: ShellHealthSnapshot = {
	status: 'ok',
	engine: 'running',
	lock_pid: null,
	lock_port: null,
	exit_code: null,
	reason: null
};

export class ShellHealthServer {
	private snapshot: ShellHealthSnapshot = { ...DEFAULT_SNAPSHOT };
	private relaunchRequested = false;

	private constructor(
		private readonly server: http.Server,
		readonly port: number
	) {}

	static async start(
		dataDir: string,
		outputHealth: (action: OutputAction) => string
	): Promise<ShellHealthServer> {
		let instance: ShellHealthServer | null = null;
		const server = http.createServer((req, res) => {
			const respond = (status: number, body: string): void => {
				res.writeHead(status, { 'Content-Type': 'application/json', Connection: 'close' });
				res.end(body);
			};
			const target = (req.url ?? '').split('?')[0];
			if (instance === null) return respond(404, '{"status":"not_found"}');
			if (req.method === 'POST' && target === RELAUNCH_PATH) {
				instance.relaunchRequested = true;
				return respond(202, '{"status":"accepted"}');
			}
			if (req.method === 'GET' && target === HEALTH_PATH) {
				return respond(200, JSON.stringify({ ...instance.snapshot, checked_at: utcTimestampIso() }));
			}
			if (req.method === 'GET' && target === OUTPUT_HEALTH_PATH) return respond(200, outputHealth('probe'));
			if (req.method === 'POST' && target === SWITCH_OUTPUT_PATH) return respond(200, outputHealth('switch'));
			return respond(404, '{"status":"not_found"}');
		});
		server.keepAliveTimeout = 750;
		server.requestTimeout = 5_000;
		const port = await new Promise<number>((resolve, reject) => {
			server.once('error', (error) => reject(new Error(`binding shell health listener failed: ${error.message}`)));
			server.listen(0, '127.0.0.1', () => {
				const address = server.address();
				if (address === null || typeof address === 'string') {
					reject(new Error(`reading shell health port failed: ${String(address)}`));
				} else {
					resolve(address.port);
				}
			});
		});
		try {
			writeShellJson(dataDir, port);
		} catch (error) {
			server.close();
			throw error;
		}
		instance = new ShellHealthServer(server, port);
		return instance;
	}

	update(snapshot: ShellHealthSnapshot): void {
		this.snapshot = { ...snapshot };
	}

	current(): ShellHealthSnapshot {
		return { ...this.snapshot };
	}

	takeRelaunchRequest(): boolean {
		const requested = this.relaunchRequested;
		this.relaunchRequested = false;
		return requested;
	}

	close(): Promise<void> {
		return new Promise((resolve) => this.server.close(() => resolve()));
	}
}

export function writeShellJson(dataDir: string, port: number): void {
	const file = path.join(dataDir, SHELL_LOCK_FILE);
	try {
		fs.writeFileSync(file, `${JSON.stringify({ health_port: port, shell_pid: process.pid })}\n`, { mode: 0o600 });
		fs.chmodSync(file, 0o600);
	} catch (error) {
		throw new Error(`writing ${file} failed: ${(error as Error).message}`);
	}
}
