import type { ChildProcess } from 'node:child_process';

/** Types for stop-fixture-server.mjs, for the TypeScript specs that import it. */
export declare function stopFixtureServer(
	serverProcess: ChildProcess,
	apiBase: string
): Promise<{ code: number | null; signal: NodeJS.Signals | null }>;
