---
term: Fresh-eyes review
slug: fresh-eyes-review
aliases: [unprimed review, blind review, cold read, independent review, red team]
group: Evaluation and method
level: 1
one_liner: Send a reviewer who has seen none of the discussion, only the goal and the material, and ask what the team missed.
why_it_matters_here: The people and agents who built a measure share its blind spots, so Open DJ schedules unprimed reviewers to find them.
related: [external-validation, mutation-test, calibration-against-ratings, rl-environment-reward-policy]
sources:
  - Fagan, "Design and Code Inspections to Reduce Errors in Program Development", IBM Systems Journal 15(3), 1976
  - Klein, "Performing a Project Premortem", Harvard Business Review, September 2007
  - Wikipedia, "Red team" (secondary pointer)
status: draft
---

## In plain words

The people who built something share its assumptions, and an assumption everyone shares is
invisible to all of them. A fresh-eyes review sends someone who has seen none of the discussion,
a new person or a new agent, with only the goal and where to find the material, and asks what is
missing or wrong. "Unprimed" is the important word. Hand the reviewer your hypothesis, your
reasoning or your worries and it will tend to check exactly those and agree, which feels like
confirmation but is only an echo. Keep the brief short, let it look anywhere, and write down
what it finds whether or not the team adopts it, so a later reader can see what was raised and
why it was set aside. It is cheap, so it is worth doing on a schedule rather than only when
something already feels off.

## Picture

```text
 primed review                         fresh-eyes review
 +---------------------------+         +---------------------------+
 | brief  our plan, our      |         | brief  the goal, where    |
 | reasoning, our worries    |         | the fixtures live, the    |
 +---------------------------+         | current score, nothing    |
               |                       | else                      |
               v                       +---------------------------+
 +---------------------------+                       |
 | reviewer checks what      |                       v
 | it was told to check      |         +---------------------------+
 +---------------------------+         | reviewer finds what no    |
               |                       | one told it to look for   |
               v                       +---------------------------+
       "looks good"                                  |
       an echo of the brief                          v
                                           logged, adopted or not
```

## In Open DJ

Proposed, not yet on main: the plan for the quality measure schedules it: every third round,
a fresh agent gets a two-paragraph brief (the goal, the fixture location, the current score,
nothing else) and reports what it thinks is missing, and its findings are logged whether or not
they are adopted. Pull requests get a related check: when no reviewer has covered a pull request's current head,
`just review` (`(private path)`) runs a chain of independent model reviewers that did
not write the change.
