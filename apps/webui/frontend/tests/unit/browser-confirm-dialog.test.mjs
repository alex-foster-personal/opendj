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

	// PR #4014 (Sol P2 r4167041291): a superseding confirmation must not inherit
	// the previous prompt's "Don't show this again" / "Default to this" ticks.
	// The dialog keeps them in component state, so the panel remounts it per
	// request by keying it on the pending request object. Structural, because
	// node tests cannot mount Svelte components (load-typescript.mjs stubs them).
	it('remounts the dialog for each confirmation request', () => {
		const panelSrc = readFileSync(
			join(root, 'src/lib/components/rb/BrowserPanel.svelte'),
			'utf8'
		);
		assert.match(
			panelSrc,
			/\{#key browserConfirmPending\}\s*<BrowserConfirmDialog[\s\S]*?\/>\s*\{\/key\}/
		);
		// The supersede path must still replace the request OBJECT, which is the
		// key; mutating the old one in place would keep the stale checkboxes.
		assert.match(panelSrc, /browserConfirmPending = \{/);
	});
});
