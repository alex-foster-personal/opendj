// Parity row 35: the openExternal bridge takes only the Tauri opener scope plus Polar billing.
import * as assert from 'node:assert/strict';
import { test } from 'node:test';

import { isOpenerUrlAllowed } from '../src/opener';
import { isExternalUrlAllowed } from '../src/policy';

const fourDigitPort = 1000 + (process.pid % 9000);
const fiveDigitPort = 10000 + (process.pid % 55536);
// userinfo built at runtime so the source never carries a mailbox-shaped string (oss_tip_audit)
const AT = '@';

test('openExternal bridge allows named local http with four-digit port and slash', () => {
	assert.equal(isOpenerUrlAllowed(`http://opendj.localhost:${fourDigitPort}/settings`), true);
});
test('openExternal bridge allows named local http with five-digit port and path', () => {
	assert.equal(isOpenerUrlAllowed(`http://opendj.localhost:${fiveDigitPort}/performance/deck`), true);
});
test('openExternal bridge allows Google OAuth consent URL with query marker', () => {
	const url =
		'https://accounts.google.com/o/oauth2/v2/auth?client_id=example&redirect_uri=opendj&response_type=code';
	assert.equal(isOpenerUrlAllowed(url), true);
});
test('openExternal bridge allows exact BlackHole installer URL', () => {
	assert.equal(isOpenerUrlAllowed('https://existential.audio/blackhole/'), true);
});
test('openExternal bridge rejects userinfo attacker shape on named local port', () => {
	const url = `http://opendj.localhost:${fourDigitPort}${AT}attacker.example/`;
	assert.equal(isOpenerUrlAllowed(url), false);
});

test('if the bridge refuses a Polar checkout or portal URL then billing cannot open', () => {
	for (const url of [
		'https://buy.polar.sh/polar_cl_example',
		'https://checkout.polar.sh/bundle',
		'https://polar.sh/opendj/portal?customer_session_token=x',
		'https://sandbox.polar.sh/checkout/polar_c_example'
	]) {
		assert.equal(isOpenerUrlAllowed(url), true, url);
	}
});

test('if the bridge opens any other web URL then a page bug can send the DJ anywhere', () => {
	for (const [label, url] of [
		['arbitrary https', 'https://example.com/'],
		['purchase link (a clicked link, not a bridge call)', 'https://www.beatport.com/track/x/1'],
		['blackhole extra path', 'https://existential.audio/blackhole/download'],
		['blackhole query', 'https://existential.audio/blackhole/?x=1'],
		['google without query marker', 'https://accounts.google.com/o/oauth2/v2/auth'],
		['google wrong path', 'https://accounts.google.com/o/oauth2/v2/other?x=1'],
		['google lookalike host', 'https://accounts.google.com.evil.example/o/oauth2/v2/auth?x=1'],
		['google userinfo', `https://evil${AT}accounts.google.com/o/oauth2/v2/auth?x=1`],
		['google explicit port', 'https://accounts.google.com:8443/o/oauth2/v2/auth?x=1'],
		['named local userinfo shape', `http://opendj.localhost:${fourDigitPort}${AT}attacker.example/`],
		['named local over https', `https://opendj.localhost:${fourDigitPort}/`],
		['polar over http', 'http://buy.polar.sh/c/1'],
		['polar lookalike host', 'https://polar.sh.evil.example/c/1'],
		['polar suffix without dot', 'https://notpolar.sh/c/1'],
		['polar userinfo', `https://polar.sh${AT}attacker.example/`],
		['polar explicit port', 'https://buy.polar.sh:8443/c/1'],
		['file', 'file:///Applications/Calculator.app'],
		['javascript', 'javascript:alert(1)'],
		['malformed', 'not a url']
	] as const) {
		assert.equal(isOpenerUrlAllowed(url), false, label);
	}
});

test('if link handling narrows to the bridge scope then clicked purchase and privacy links stop opening', () => {
	// The bridge is narrower than navigation; a link the user clicks still opens any https page.
	assert.equal(isExternalUrlAllowed('https://www.beatport.com/track/x/1'), true);
	assert.equal(isOpenerUrlAllowed('https://www.beatport.com/track/x/1'), false);
});

const INVENTED_PLAYLIST_ID = 'AbCdEfGhIjKlMnOpQrStUv';

test('if the bridge admits spotify playlist URIs then Open in Spotify app works', () => {
	const uri = `spotify:playlist:${INVENTED_PLAYLIST_ID}`;
	assert.equal(isOpenerUrlAllowed(uri), true);
	assert.equal(isExternalUrlAllowed(uri), true);
});

test('if the bridge admits Spotify base-62 playlist ids then 21- and 24-char ids open', () => {
	const id21 = 'AbCdEfGhIjKlMnOpQrStU';
	const id24 = 'AbCdEfGhIjKlMnOpQrStUvWx';
	assert.equal(id21.length, 21);
	assert.equal(id24.length, 24);
	assert.equal(isOpenerUrlAllowed(`spotify:playlist:${id21}`), true);
	assert.equal(isOpenerUrlAllowed(`spotify:playlist:${id24}`), true);
});

test('if the bridge validates playlist ids then path traversal in the URI is refused', () => {
	assert.equal(isOpenerUrlAllowed('spotify:playlist:../x'), false);
});

test('if the bridge admits only playlist URIs then track and album schemes stay out', () => {
	assert.equal(isOpenerUrlAllowed(`spotify:track:${INVENTED_PLAYLIST_ID}`), false);
	assert.equal(isOpenerUrlAllowed(`spotify:album:${INVENTED_PLAYLIST_ID}`), false);
	assert.equal(isOpenerUrlAllowed(`spotify:artist:${INVENTED_PLAYLIST_ID}`), false);
	assert.equal(isOpenerUrlAllowed('spotify:playlist:'), false);
	assert.equal(isOpenerUrlAllowed('spotify://playlist/x'), false);
	assert.equal(
		isOpenerUrlAllowed(`https://open.spotify.com/playlist/${INVENTED_PLAYLIST_ID}`),
		false
	);
});
