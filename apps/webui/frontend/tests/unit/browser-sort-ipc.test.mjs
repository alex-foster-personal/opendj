import assert from 'node:assert/strict';
import { test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

test('browser sort IPC fails fast outside a native browser', async () => {
	const { installBrowserSortIpc } = await loadTypeScriptModule(
		'src/lib/components/rb/browser/browser-sort-ipc.ts'
	);
	assert.throws(() => installBrowserSortIpc({}), /browser sort IPC requires a browser window/);
});
