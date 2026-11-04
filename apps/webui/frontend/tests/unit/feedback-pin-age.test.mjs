// requirement: PGCL-7693b
/**
 * Pin 8bd1522ce260: a day count hides older comment pins, and a pin under
 * 3 days old counts as new (yellow outline in the marker layer).
 */
import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

const DAY = 24 * 60 * 60 * 1000;
const NOW = Date.parse('2026-10-10T12:00:00Z');

let age;
before(async () => {
	age = await loadTypeScriptModule('src/lib/rb/feedback-pin-age.ts');
});

function iso(ms) {
	return new Date(ms).toISOString();
}

test('0 days shows every pin, including a very old one', () => {
	assert.equal(age.isPinWithinVisibleDays(iso(NOW - 400 * DAY), 0, NOW), true);
});

test('a positive day count hides pins older than that number and keeps the rest', () => {
	assert.equal(age.isPinWithinVisibleDays(iso(NOW - 7 * DAY), 7, NOW), true);
	assert.equal(age.isPinWithinVisibleDays(iso(NOW - 7 * DAY - 1000), 7, NOW), false);
	assert.equal(age.isPinWithinVisibleDays(iso(NOW - 2 * DAY), 7, NOW), true);
});

test('a pin with no timestamp stays visible', () => {
	assert.equal(age.isPinWithinVisibleDays('not-a-date', 3, NOW), true);
});

test('new means under 3 days old', () => {
	assert.equal(age.isNewPin(iso(NOW - 2.9 * DAY), NOW), true);
	assert.equal(age.isNewPin(iso(NOW - 3 * DAY), NOW), false);
	assert.equal(age.isNewPin(iso(NOW + DAY), NOW), true);
	assert.equal(age.PIN_NEW_MAX_DAYS, 3);
});

test('the day count is an integer from 0 to 365, and 0 is the default', () => {
	assert.equal(age.PIN_VISIBLE_DAYS_DEFAULT, 0);
	assert.equal(age.isPinVisibleDays(0), true);
	assert.equal(age.isPinVisibleDays(365), true);
	assert.equal(age.isPinVisibleDays(-1), false);
	assert.equal(age.isPinVisibleDays(1.5), false);
	assert.equal(age.isPinVisibleDays(366), false);
});
