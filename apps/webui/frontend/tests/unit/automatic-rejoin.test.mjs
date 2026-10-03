import assert from 'node:assert/strict';
import { test } from 'node:test';
import { installAutomaticRejoinRunner, runAutomaticRejoin } from '../../src/lib/rb/automatic-rejoin.ts';
import { ScopedCommandScheduler } from '../../src/lib/rb/performance-command-scheduler.ts';

// Exercise the actual runner and scheduler, without replacing an engine or API.
test('automatic rejoin claims every deck and sync behind prior work', async () => {
    const scheduler = new ScopedCommandScheduler();
    const restore = installAutomaticRejoinRunner((work) => scheduler.run([1, 2, 3, 4, 'sync'], work));
    const events = [];
    let release;
    const prior = scheduler.run([1], async () => {
        events.push('prior');
        await new Promise((resolve) => { release = resolve; });
    });
    try {
        const rejoin = runAutomaticRejoin(async () => { events.push('rejoin'); });
        const later = scheduler.run([2], async () => { events.push('later'); });
        await Promise.resolve();
        await Promise.resolve();
        assert.deepEqual(events, ['prior']);
        release();
        await Promise.all([prior, rejoin, later]);
        assert.deepEqual(events, ['prior', 'rejoin', 'later']);
    } finally { restore(); }
});

test('automatic rejoin preserves failures and releases the scheduler claim', async () => {
    const scheduler = new ScopedCommandScheduler();
    const restore = installAutomaticRejoinRunner((work) => scheduler.run([1, 2, 3, 4, 'sync'], work));
    try {
        const failure = new Error('operation rejected');
        await assert.rejects(runAutomaticRejoin(async () => { throw failure; }), (error) => error === failure);
        assert.equal(await scheduler.run(['sync'], async () => 42), 42);
    } finally { restore(); }
});
