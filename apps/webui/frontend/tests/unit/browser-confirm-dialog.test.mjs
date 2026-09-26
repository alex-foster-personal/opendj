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

	it('forwards checkbox state through the secondary (Move) callback', () => {
		const dialogSrc = readFileSync(
			join(root, 'src/lib/components/rb/browser/BrowserConfirmDialog.svelte'),
			'utf8'
		);
		const panelSrc = readFileSync(
			join(root, 'src/lib/components/rb/BrowserPanel.svelte'),
			'utf8'
		);
		assert.match(dialogSrc, /onSecondary\?\.\(\{ remember, setDefault \}\)/);
		assert.match(panelSrc, /onSecondary=\{\(opts\) =>/);
		assert.match(panelSrc, /resolve\(\{ ok: false, \.\.\.opts \}\)/);
	});
});
