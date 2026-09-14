/**
 * Route-local helpers for the /admin Playground tab.
 *
 * API console: raw same-origin fetch (not $lib/api/client) so 4xx/5xx are results.
 * SQL console: typed client for POST /api/v1/admin/sql-query.
 */

import { API_BASE } from '$lib/api/base';
import { ApiError, api, unwrap } from '$lib/api/client';

export type PlaygroundMethod = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

export interface PlaygroundHttpResult {
	status: number;
	headers: Record<string, string>;
	body: string;
}

export interface SqlQueryResult {
	columns: string[];
	rows: unknown[][];
	truncated: boolean;
	row_count: number;
}

export function parsePlaygroundPath(raw: string): string {
	const path = raw.trim();
	if (!path.startsWith('/api/v1')) {
		throw new Error('path must start with /api/v1');
	}
	if (path.length > 7 && path[7] !== '/' && path[7] !== '?') {
		throw new Error('path must be /api/v1 or /api/v1/...');
	}
	if (path.includes('://')) {
		throw new Error('absolute URLs are not allowed');
	}
	if (path.startsWith('//')) {
		throw new Error('protocol-relative URLs are not allowed');
	}
	if (path.includes('\\')) {
		throw new Error('backslashes are not allowed in the path');
	}
	const queryAt = path.indexOf('?');
	const pathPart = queryAt === -1 ? path : path.slice(0, queryAt);
	if (pathPart.includes('..')) {
		throw new Error('path segments may not contain ..');
	}
	return path;
}

export async function sendPlaygroundRequest(input: {
	method: PlaygroundMethod;
	path: string;
	bodyText: string;
}): Promise<PlaygroundHttpResult> {
	const path = parsePlaygroundPath(input.path);
	const url = `${API_BASE}${path}`;
	const init: RequestInit = {
		method: input.method,
		credentials: 'same-origin',
		cache: 'no-store'
	};
	if (input.method !== 'GET' && input.bodyText.trim() !== '') {
		try {
			JSON.parse(input.bodyText);
		} catch {
			throw new Error('body is not valid JSON');
		}
		init.headers = { 'Content-Type': 'application/json' };
		init.body = input.bodyText;
	}
	let response: Response;
	try {
		response = await fetch(url, init);
	} catch (error) {
		const message = error instanceof Error ? error.message : String(error);
		throw new Error(`network request failed: ${message}`);
	}
	const headers: Record<string, string> = {};
	const contentType = response.headers.get('content-type');
	if (contentType) headers['content-type'] = contentType;
	const etag = response.headers.get('etag');
	if (etag) headers.etag = etag;
	const rawBody = await response.text();
	let body = rawBody;
	if (rawBody.trim() !== '') {
		try {
			body = JSON.stringify(JSON.parse(rawBody), null, 2);
		} catch {
			body = rawBody;
		}
	}
	return { status: response.status, headers, body };
}

function asObject(value: unknown, ctx: string): Record<string, unknown> {
	if (typeof value !== 'object' || value === null || Array.isArray(value)) {
		throw new Error(`sql query: ${ctx} is not an object`);
	}
	return value as Record<string, unknown>;
}

function asStringArray(value: unknown, ctx: string): string[] {
	if (!Array.isArray(value) || value.some((item) => typeof item !== 'string')) {
		throw new Error(`sql query: ${ctx} is not a string array`);
	}
	return value;
}

function asRowArray(value: unknown, ctx: string): unknown[][] {
	if (!Array.isArray(value) || value.some((row) => !Array.isArray(row))) {
		throw new Error(`sql query: ${ctx} is not an array of arrays`);
	}
	return value as unknown[][];
}

function asBoolean(value: unknown, ctx: string): boolean {
	if (typeof value !== 'boolean') {
		throw new Error(`sql query: ${ctx} is not a boolean`);
	}
	return value;
}

function asInteger(value: unknown, ctx: string): number {
	if (typeof value !== 'number' || !Number.isInteger(value)) {
		throw new Error(`sql query: ${ctx} is not an integer`);
	}
	return value;
}

export function parseSqlQueryResult(raw: unknown): SqlQueryResult {
	const obj = asObject(raw, 'response');
	return {
		columns: asStringArray(obj.columns, 'columns'),
		rows: asRowArray(obj.rows, 'rows'),
		truncated: asBoolean(obj.truncated, 'truncated'),
		row_count: asInteger(obj.row_count, 'row_count')
	};
}

export function clampSqlLimit(limit: number): number {
	if (!Number.isFinite(limit)) return 200;
	return Math.min(500, Math.max(1, Math.trunc(limit)));
}

export function formatSqlCell(value: unknown): string {
	if (value === null) return 'null';
	return String(value);
}

export async function fetchSqlQuery(sql: string, limit: number): Promise<SqlQueryResult> {
	try {
		const data = await unwrap(
			api.POST('/api/v1/admin/sql-query', {
				body: { sql, limit: clampSqlLimit(limit) }
			})
		);
		return parseSqlQueryResult(data);
	} catch (error) {
		if (error instanceof ApiError) {
			const bodyText = await error.response.text();
			throw new Error(`POST /api/v1/admin/sql-query failed (HTTP ${error.status}): ${bodyText}`);
		}
		throw error;
	}
}
