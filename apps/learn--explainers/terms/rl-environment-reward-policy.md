---
term: Environment, state, action, reward, policy
slug: rl-environment-reward-policy
aliases: [reinforcement learning loop, RL environment, reward signal, agent-environment loop, reward hacking]
group: Evaluation and method
level: 3
one_liner: A chooser sees a state, takes an action, gets a reward from the environment, and its policy is the rule it improves.
why_it_matters_here: The transition chooser is framed as a policy iterating against one score, so the score has to be trustworthy before the chooser is tuned against it.
related: [veto-vs-weighted-score, mutation-test, noise-band-and-bootstrap, offline-render, external-validation]
sources:
  - Sutton and Barto, Reinforcement Learning, An Introduction, 2nd ed., MIT Press, 2018 (chapter 3)
  - Amodei et al., "Concrete Problems in AI Safety", arXiv 1606.06565, 2016 (reward hacking)
  - Wikipedia, "Reinforcement learning" (secondary pointer)
status: draft
---

## In plain words

Reinforcement learning (RL) words describe any loop where a chooser tries things and learns
from a score. The environment is the world it acts in: here the test track pairs, the renderer
and a pinned scorer version. The state is what it sees right now: a pair of tracks and their
analysis (grid, key, energy, vocals). The action is what it picks: a transition plan, meaning a
style, a mix point and the curves for each lane. The reward is the number that comes back: the
quality score M of the rendered result. The policy is the rule that maps a state to an action,
and iterating means changing the policy to earn more reward. The famous trap is reward hacking:
the policy finds what the score pays for rather than what a crowd enjoys. The defense is to
validate the score first and never change the score and the policy in the same round.

## Picture

```text
          +------------------------------------------------+
          |                  ENVIRONMENT                   |
          |   fixture pairs + offline renderer + scorer    |
          +------------------------------------------------+
             |                  ^                     |
     STATE   |          ACTION  |             REWARD  |
  track pair |   transition plan|         M of the    |
  + analysis |  style, mix point|     rendered result |
             v                  |                     v
          +------------------------------------------------+
          |                     POLICY                     |
          |       the proposer, picks a plan per pair      |
          +------------------------------------------------+

   one round = change the policy, re-run every pair, compare M
   never change the scorer and the policy in the same round
```

## In Open DJ

Not yet in code. The proposed loop (an Open DJ design, not on main) is this: the environment is the fixture pairs, the offline renderer
(`apps/audio-engine/src/plan.rs`) and the scorer version; the action is a proposed transition plan; the
reward is M; the policy is a proposer (proposed, not yet on main) and later the try-er's ranker. It is not RL training in the neural-network sense: the
policy improves through numbered agent rounds logged with before and after scores.
