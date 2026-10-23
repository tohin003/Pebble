### Overall

| Measure | pebble | claude_code |
|---|---|---|
| Cells | 135 | 135 |
| Success | 79.3 % | 82.2 % |
| Success 95 % CI (task clusters) | 68.1 % – 88.9 % | 71.9 % – 91.9 % |
| False completions | 21 | 17 |
| Format only | 1 | 1 |
| Review burden (mean) | 0.04 | 0.00 |
| R1 canonical / worst paraphrase | 82.2 % / 75.6 % | 80.0 % / 80.0 % |
| R2 multi-turn success | 88.9 % | 80.6 % |
| R3 requests median / p95 | 3.0 / 6.0 | — / — |
| R3 wall s median / p95 | 10.9 / 39.9 | 9.7 / 17.6 |
| R4 cached input share | 1.4 % | 87.8 % |

Paired difference (pebble − claude_code): -3.3 %, 95 % CI -9.2 % to 2.5 %; McNemar b=3, c=7, p=0.344.

### By family

| Family | Scope | pebble | claude_code | Paired Δ (95 % CI) |
|---|---|---|---|---|
| coding_fix | compared | 80.0 % | 93.3 % | -13.3 % (-26.7 % to 0.0 %) |
| coding_build_review | compared | 93.3 % | 100.0 % | -6.7 % (-20.0 % to 0.0 %) |
| source_search | compared | 100.0 % | 100.0 % | 0.0 % (0.0 % to 0.0 %) |
| research | compared | 0.0 % | 20.0 % | -20.0 % (-46.7 % to 0.0 %) |
| documents | compared | 93.3 % | 93.3 % | 0.0 % (0.0 % to 0.0 %) |
| multi_turn | compared | 100.0 % | 100.0 % | 0.0 % (0.0 % to 0.0 %) |
| delegation | descriptive | 100.0 % | 100.0 % | 0.0 % (0.0 % to 0.0 %) |
| clarification | compared | 53.3 % | 33.3 % | 20.0 % (0.0 % to 46.7 %) |
| companion | compared | 93.3 % | 100.0 % | -6.7 % (-20.0 % to 0.0 %) |

### Claims (pre-registered rule)

Pebble is better than Claude Code CLI on a scope only where the pre-registered 95% lower bound of the paired success difference (task-cluster bootstrap) is above 0.

| Scope | Lower 95 % bound | Better? |
|---|---|---|
| overall | -9.2 % | no claim |
| coding_fix | -26.7 % | no claim |
| coding_build_review | -20.0 % | no claim |
| source_search | 0.0 % | no claim |
| research | -46.7 % | no claim |
| documents | 0.0 % | no claim |
| multi_turn | 0.0 % | no claim |
| clarification | 0.0 % | no claim |
| companion | -20.0 % | no claim |
