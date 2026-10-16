test(e2e): wait for mirror leadership before the contrast audit samples

The UICONTRAST-01 audit sampled /performance while the page could still be
an AGENT-18 lease-blocked follower: a previous page's 10 s ui-mirror lease
(the preceding spec's /performance page, or this test's own pre-reload page)
was still live, so the follower banner "Another Open DJ window is in
control" was on screen and was reported as a NEW finding on
default/performance-more and -less. After every skin reload the spec now
waits for an accepted mirror PUT from the reloaded document, the engine
naming that client as lease holder, and the banner gone.

-Claude

Co-Authored-By: Claude Opus 5.5 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_015tqdurX98UJLc8Frpbj2R3
(cherry picked from commit 922f55acbec03561aea448e1b04aee5e8aeb215f)

