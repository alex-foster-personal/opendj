// The update feed the shell installs from ((private path) section b).
import * as assert from 'node:assert/strict';
import { test } from 'node:test';

import { FEED_URL_ENV, UPDATE_FEED_URL, resolveFeedUrl } from '../src/updater';


test('the feed is the public issue-assets latest-release directory', () => {
	assert.equal(UPDATE_FEED_URL, 'https://github.com/alex-foster-personal/issue-assets/releases/latest/download');
	assert.equal(resolveFeedUrl({}), UPDATE_FEED_URL);
});

test('the override replaces the feed directory and nothing else', () => {
	assert.equal(resolveFeedUrl({ [FEED_URL_ENV]: 'https://proof-feed.invalid/feed/' }), 'https://proof-feed.invalid/feed');
	assert.equal(resolveFeedUrl({ [FEED_URL_ENV]: '   ' }), UPDATE_FEED_URL);
});

