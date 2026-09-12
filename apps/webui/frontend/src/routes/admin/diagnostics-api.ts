/**
 * Route-local fetch helper for GET /api/v1/admin/ports.
 *
 * Same fail-fast contract as quality-api.ts: the response is structurally
 * validated before render, and HTTP failures surface status + body.
 */

import { ApiError, api, unwrap } from '$lib/api/client';

export interface WorktreePorts {
	backend: number;
	frontend: number;
	api_proxy_target: string;
}

function asObject(value: unknown, ctx: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`worktree ports: ${ctx} is not an object`);
	}
	return value as Record<string, unknown>;
}

function asInteger(value: unknown, ctx: string): number {
	if (typeof value !== 'number' || !Number.isInteger(value)) {
		throw new Error(`worktree ports: ${ctx} is not an integer`);
	}
	return value;
}

function asString(value: unknown, ctx: string): string {
	if (typeof value !== 'string') throw new Error(`worktree ports: ${ctx} is not a string`);
	return value;
}

export function parseWorktreePorts(raw: unknown): WorktreePorts {
	const obj = asObject(raw, 'response');
	return {
		backend: asInteger(obj.backend, 'backend'),
		frontend: asInteger(obj.frontend, 'frontend'),
		api_proxy_target: asString(obj.api_proxy_target, 'api_proxy_target')
	};
}

export async function fetchWorktreePorts(): Promise<WorktreePorts> {
	try {
		const data = await unwrap(api.GET('/api/v1/admin/ports', { cache: 'no-store' }));
		return parseWorktreePorts(data);
	} catch (error) {
		if (error instanceof ApiError) {
			const bodyText = await error.response.text();
			throw new Error(`GET /api/v1/admin/ports failed (HTTP ${error.status}): ${bodyText}`);
		}
		throw error;
	}
}
