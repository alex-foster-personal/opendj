import assert from 'node:assert/strict';
import { describe, it } from 'node:test';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const root = join(dirname(fileURLToPath(import.meta.url)), '..', '..');

describe('browser-confirm-dialog', () => {
	it('exposes remember and default checkboxes', () => {
		const src = readFileSync(
			join(root, 'src/lib/components/rb/browser/BrowserConfirmDialog.svelte'),
			'utf8'
		);
		assert.match(src, /Don't show this again/);
		assert.match(src, /Default to this/);
	});
});
