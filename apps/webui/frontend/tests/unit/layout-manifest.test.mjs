import assert from 'node:assert/strict';
import { before, test } from 'node:test';

import { loadTypeScriptModule } from './load-typescript.mjs';

let m;

before(async () => {
	m = await loadTypeScriptModule('src/lib/layout/layout-manifest.ts', {
		viteApiBase: 'https://layout-manifest.example.test'
	});
});

const valid = () => ({
	schema: 1,
	root: {
		direction: 'column',
		children: [
			{
				direction: 'row',
				tiers: [864],
				children: [
					{ id: 'browser.tree', size: { default: 220, min: 160, max: 480 }, collapsible: true }
				]
			},
			{ size: 'flex' }
		]
	}
});

test('[if] schema is missing or not 1 [then] parseLayoutManifest throws naming the supported schema', () => {
	const { schema: _s, ...noSchema } = valid();
	assert.throws(() => m.parseLayoutManifest(noSchema), /\$\.schema.*supported schema is 1/);
	assert.throws(() => m.parseLayoutManifest({ ...valid(), schema: 2 }), /supported schema is 1/);
});

test('[if] a node id is not in LAYOUT_REGION_IDS [then] it throws naming the path and the allowed ids', () => {
	const doc = valid();
	doc.root.children[1].id = 'made.up';
	assert.throws(
		() => m.parseLayoutManifest(doc),
		/\$\.root\.children\[1\]\.id.*allowed ids are browser\.tree/
	);
});

test('[if] a node has a key outside the allowed set [then] it throws naming the key path', () => {
	const doc = valid();
	doc.root.children[0].children[0].colour = 'red';
	assert.throws(
		() => m.parseLayoutManifest(doc),
		/\$\.root\.children\[0\]\.children\[0\]\.colour: unknown key/
	);
});

test('[if] size.min > size.max or default outside min..max [then] it throws naming the path and both bounds', () => {
	const doc = valid();
	doc.root.children[0].children[0].size = { default: 200, min: 500, max: 300 };
	assert.throws(
		() => m.parseLayoutManifest(doc),
		/\$\.root\.children\[0\]\.children\[0\]\.size: min 500 .*max 300/
	);
	const doc2 = valid();
	doc2.root.children[0].children[0].size = { default: 900, min: 160, max: 480 };
	assert.throws(
		() => m.parseLayoutManifest(doc2),
		/size\.default: 900 is outside min 160\.\.max 480/
	);
});

test('[if] the same region id appears twice [then] it throws naming both paths', () => {
	const doc = valid();
	doc.root.children[1].id = 'browser.tree';
	assert.throws(
		() => m.parseLayoutManifest(doc),
		/\$\.root\.children\[0\]\.children\[0\]\.id and \$\.root\.children\[1\]\.id/
	);
});

test('[if] a valid manifest is parsed [then] the result round-trips through JSON unchanged', () => {
	const parsed = m.parseLayoutManifest(valid());
	assert.deepEqual(JSON.parse(JSON.stringify(parsed)), parsed);
	assert.deepEqual(parsed, valid());
});
