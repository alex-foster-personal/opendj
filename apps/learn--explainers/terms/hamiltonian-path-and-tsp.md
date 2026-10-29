---
term: Hamiltonian path and the traveling salesman problem
slug: hamiltonian-path-and-tsp
aliases: [TSP, traveling salesman, shortest Hamiltonian path, open path, 2-opt, Or-opt, Held-Karp]
group: Evaluation and method
level: 3
one_liner: Ordering a playlist is finding the cheapest route that visits every track once, with each hop costing how rough that transition is.
why_it_matters_here: It explains why the current one-pass ordering leaves bad transitions at the end of a set, and what the planned optimizer does about it.
related: [beam-search, camelot-wheel, veto-vs-weighted-score, held-out-set]
sources:
  - Applegate, Bixby, Chvatal and Cook, The Traveling Salesman Problem, A Computational Study, Princeton University Press, 2006
  - Held and Karp, "A Dynamic Programming Approach to Sequencing Problems", Journal of SIAM 10(1), 1962
  - Wikipedia, "Travelling salesman problem" and "Hamiltonian path" (secondary pointers)
status: draft
---

## In plain words

Treat each track as a town and each transition as a road whose length is how rough that mix
would be. A set order is a route that visits every town exactly once, and the best order is the
shortest such route: a shortest Hamiltonian path. The traveling salesman problem (TSP) is the
famous version that returns home, a cycle. A DJ set is an open path instead, often with a fixed
opener and closer, and its roads are one-way: A into B is not the same mix as B into A. Trying
every order explodes (20 tracks have about 2.4 billion billion orders), so exact methods such as
Held-Karp only suit about a dozen tracks. Greedy picks the cheapest next track every time; it is
fast but strands the awkward tracks at the end. Two-ended growth works in from both the opener
and the closer, which spreads the leftovers. The cycle trick adds a dummy town that costs zero to
enter and leave, solves an ordinary loop, then cuts the loop at the dummy. Local moves such as
2-opt (reverse a stretch) and Or-opt (move one to three tracks) then polish the result.

## Picture

```text
 each number is the cost of one hop, lower is smoother

 greedy, always the cheapest next track from the opener S
 S --1-- a --1-- b --1-- c --2-- d --6-- e --9-- T      total 20
                                  the junk piles up at the end

 two-ended start, then 2-opt and Or-opt
 S --1-- a --2-- d --2-- c --2-- e --2-- b --3-- T      total 12
                     spread out, no 9-cost cliff

 cycle trick for an open path with a fixed opener and closer
 S ... T --0-- DUMMY --0-- S       solve as a loop, cut at DUMMY
```

## In Open DJ

`apps/dj_copilot/solver.py` (PLAY IT) builds the order forward from one start with a beam of
width 8 and no improvement pass afterward. The playlist-ordering research (Tue 27 Oct 2026)
proposes an asymmetric open-path solver: contract locked pairs, add the zero-cost dummy, seed
from greedy runs plus the current beam result, polish with Or-opt and 2-opt, and use Held-Karp
on 12 or fewer tracks as the test oracle. Its first trap is "junk at the ends": fail when the
rate of abrupt edges in the last 20 percent is more than 1.5 times the rate in the middle.
