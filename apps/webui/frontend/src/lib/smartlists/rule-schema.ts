/**
 * Rule-AST field/operator constants, transcribed from the Python source of
 * truth so the browser editor can never drift from what the backend
 * accepts:
 *
 *  - FIELD_TYPES / ALLOWED_FIELDS: apps/shared/smartlists/schema.py
 *  - ALLOWED_OPS_BY_FIELD, LOGICAL_OPS: apps/shared/smartlists/schema.py
 *  - order_by allowlist: apps/smartlists/repo.py `_ALLOWED_ORDER_BY`
 *    (mirrors evaluator.py `_ORDER_BY_COLUMNS`)
 *
 * Do not add a field, op, or order_by value here that the Python side does
 * not already accept.
 */

export type FieldType = 'string' | 'number' | 'date' | 'list';

export type FieldName =
	| 'genre'
	| 'bpm'
	| 'key'
	| 'rating'
	| 'energy'
	| 'custom_tags'
	| 'color_tag'
	| 'added_date'
	| 'last_played'
	| 'paired_with';

export type Op = '=' | '!=' | '<' | '<=' | '>' | '>=' | 'between' | 'in' | 'contains' | 'missing';

export type LogicalOp = 'and' | 'or' | 'not';

export const FIELD_TYPES: Record<FieldName, FieldType> = {
	genre: 'string',
	bpm: 'number',
	key: 'string',
	rating: 'number',
	energy: 'number',
	custom_tags: 'list',
	color_tag: 'string',
	added_date: 'date',
	last_played: 'date',
	paired_with: 'string'
};

export const ALLOWED_FIELDS: FieldName[] = Object.keys(FIELD_TYPES) as FieldName[];

const NUMERIC_OPS: Op[] = ['=', '!=', '<', '<=', '>', '>=', 'between', 'missing'];
const STRING_OPS: Op[] = ['=', '!=', 'contains', 'in', 'missing'];
const DATE_OPS: Op[] = ['=', '<', '<=', '>', '>=', 'between', 'missing'];
const LIST_OPS: Op[] = ['contains', 'in'];
const PAIRED_WITH_OPS: Op[] = ['=', '!=', 'in'];

export const ALLOWED_OPS_BY_FIELD: Record<FieldName, Op[]> = {
	genre: STRING_OPS,
	bpm: NUMERIC_OPS,
	key: STRING_OPS,
	rating: NUMERIC_OPS,
	energy: NUMERIC_OPS,
	custom_tags: LIST_OPS,
	color_tag: STRING_OPS,
	added_date: DATE_OPS,
	last_played: DATE_OPS,
	paired_with: PAIRED_WITH_OPS
};

export const LOGICAL_OPS: LogicalOp[] = ['and', 'or', 'not'];

export const ORDER_BY_OPTIONS: string[] = [
	'added_date desc',
	'added_date asc',
	'bpm asc',
	'bpm desc',
	'rating desc',
	'rating asc',
	'energy asc',
	'energy desc',
	'random'
];

export const RELATIVE_DATE_UNITS = ['d', 'h', 'm', 'w'] as const;
export type RelativeDateUnit = (typeof RELATIVE_DATE_UNITS)[number];

export function isRelativeDateLiteral(value: string): boolean {
	return /^-?\d+[dhmw]$/.test(value);
}
