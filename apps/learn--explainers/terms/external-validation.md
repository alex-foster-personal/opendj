---
term: External validation
slug: external-validation
aliases: [criterion validity, construct validity, validation against ground truth, real-world positives]
group: Evaluation and method
level: 2
one_liner: Prove a measure against outside evidence, such as real DJs' mixes and published studies, before spending anyone's ears on it.
why_it_matters_here: It keeps the transition score from encoding one person's taste and saves the founder's listening time for the questions only he can answer.
related: [auc, mutation-test, calibration-against-ratings, held-out-set, fresh-eyes-review]
sources:
  - Cronbach and Meehl, "Construct Validity in Psychological Tests", Psychological Bulletin 52(4), 1955
  - Steyerberg, Clinical Prediction Models, 2nd ed., Springer, 2019 (internal versus external validation)
  - Wikipedia, "External validity" (secondary pointer)
status: draft
---

## In plain words

A measure tuned on one person's ratings may end up measuring that person. And a person's
listening time is the scarcest thing the project has. External validation tests a measure
against evidence from outside the team first. Real-world positives are transitions real DJs
chose to play and publish, assumed to be mostly fine; damage copies of them on purpose (one beat
late, one bar late, a random bar) and the measure must tell the real ones from the broken ones.
Published studies and labeled datasets add more: if a study reports how often DJs mix at phrase
boundaries, a measure that is right should reproduce that share. If a measure cannot tell a real
DJ's mix from the same mix shifted one beat, no amount of taste tuning will rescue it. Human
spot checks come last, aimed only where the measure and the outside evidence disagree.

## Picture

```text
 cheapest, least biased evidence first; a person's ears last

 1  real DJ transitions  vs  the same ones broken    AUC target 0.90
    one beat late, one bar late, a random bar
                 |
                 v
 2  published studies and labeled datasets          reproduce their
    voice labels, cue placement, mix lengths        reported shares
                 |
                 v
 3  mutation tests of the measure itself            each defect drops M
                 |
                 v
 4  a ten minute human spot check                   only where 1 to 3
                                                    disagree
```

## In Open DJ

`apps/lyrics/vocal_presence.py` is the worked example: its score was first validated on
MTG-Jamendo tracks with unanimous three-annotator voice labels (held-out AUC 0.970), and only
then was its threshold set from the founder's blind ratings. The proposed quality-measure plan
(not yet on main) plans the same order for transitions: real-DJ
discrimination on a 500-transition sample, reproduction of published DJ-mix findings, and
mutation tests, with an evidence ledger marking each claim validated-external, validated by the
founder, rejected or needing a recheck.
