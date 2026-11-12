import assert from 'node:assert/strict';
import { before, describe, it } from 'node:test';
import { loadTypeScriptModule } from './load-typescript.mjs';

let mod;

before(async () => {
	mod = await loadTypeScriptModule('src/lib/rb/auto-play.ts');
});

function apply(prior, phase, source = 1, follower = 2) {
	return mod.nextFollowerUnloadStreak(prior, { phase, source, follower });
}

/** Pre-#5691 bug: increment only on follower-unload, never reset on other phases. */
function applyOldRule(priorCount, phase) {
	if (phase === 'follower-unload') return priorCount + 1;
	return priorCount;
}

describe('nextFollowerUnloadStreak (#5691)', () => {
	it('issue sequence unload-load-unload-unload must not reach disarm threshold', () => {
		let streak = null;
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'load');
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'follower-unload');
		assert.equal(streak?.count, 2);
		assert.ok(streak.count < 3, 'controller would disarm at 3');
	});

	it('mutation control: old rule reaches 3 on the same four events', () => {
		const phases = ['follower-unload', 'load', 'follower-unload', 'follower-unload'];
		let oldCount = 0;
		for (const phase of phases) {
			oldCount = applyOldRule(oldCount, phase);
		}
		assert.equal(oldCount, 3, 'old rule wrongly exhausts the budget');
	});

	it('three consecutive follower-unload events on the same pair yield count 3', () => {
		let streak = null;
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'follower-unload');
		assert.equal(streak?.count, 3);
	});

	it('load after follower-unload clears the streak', () => {
		let streak = apply(null, 'follower-unload');
		streak = apply(streak, 'load');
		assert.equal(streak, null);
	});

	it('commit after follower-unload clears the streak', () => {
		let streak = apply(null, 'follower-unload');
		streak = apply(streak, 'commit');
		assert.equal(streak, null);
	});

	it('follower deck change starts a new streak at 1', () => {
		let streak = null;
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'follower-unload', 1, 3);
		assert.equal(streak?.count, 1);
		assert.equal(streak?.follower, 3);
	});

	it('source deck change starts a new streak at 1', () => {
		let streak = null;
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'follower-unload');
		streak = apply(streak, 'follower-unload', 3, 2);
		assert.equal(streak?.count, 1);
		assert.equal(streak?.source, 3);
	});

	it('throws on an unknown phase rather than guessing a default', () => {
		assert.throws(
			() => mod.nextFollowerUnloadStreak(null, { phase: 'nonsense', source: 1, follower: 2 }),
			/unhandled handoff phase/
		);
	});
});
