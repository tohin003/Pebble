# Pebble 1.0 benchmarks

**Status:** method and empty tables. No live result is reported yet. The parent fills each table from the signed,
registered results after the person's go-ahead (D13). Every cell, `not_run` included, is counted.

Specification: §7 of the 1.0 inspection plan (a maintainers' record, not published). Runner: [`benchmarks/`](../benchmarks/README.md)
and `scripts/bench_harness.py`, `scripts/bench_codex.py`, `scripts/bench_agentguard.py`, `scripts/bench_perf.py`.

## 1. What is measured

| Comparison | Products | Corpus | Claimed | Not claimed |
|---|---|---|---|---|
| Harness against harness (the claim set) | Pebble 1.0 and the baseline harness, same route, model and effort | 24a, all 60 tasks, 3 repetitions | T1–T11 (unchanged, never lowered), R1–R4 | anything about other products |
| Pebble against Codex CLI | Pebble 1.0 (frozen candidate, SHA-256-equal to the installed backend) and Codex CLI (version recorded) | 24a families 1, 2, 3, 4, 5, 7, 11, 12; family 9 descriptive | "better on a scope" only where the pre-registered lower bound of the paired difference is above 0 | other products or models, live web search, UI quality, monetary cost, tasks outside 24a |
| AgentGuard ablation (AGB-24) | Pebble with AgentGuard ON, OFF (scripted only) and SHADOW | 24g.1 | reliability metrics | any security protection |
| Performance | Pebble 1.0, source and frozen candidate | synthetic homes | PB1–PB10 against their 1.0 targets | battery |

Both products use the person's ChatGPT account, `gpt-6-luna`, medium effort. Same-model results compare harnesses on
one route, effort and synthetic corpus; they say nothing about other products.

## 2. The evaluation connection (V1-015)

Live Pebble cells never share the installed app's ChatGPT grant. The person signs in a **separate evaluation
connection** once, through an owned App in an evaluation home
(`scripts/evaluate_harness.py eval-connection --confirm-sign-in`). The private ledger records it as salted
fingerprints. Every live command (`evaluate_harness.py live` and `block`, `bench_harness.py run`,
`bench_agentguard.py live`) refuses any profile that is not that connection, and any profile whose connection id or
ChatGPT host id the installed app's settings hold. Those settings are read read-only, and only their profile ids and
host id. Codex CLI uses its own sign-in in a scratch `CODEX_HOME`.

## 3. Corpus 24a

60 tasks, 12 families of 5. Every task has a canonical prompt and two paraphrases (terse, conversational), assigned
per repetition so that every task runs every phrasing once in 3 repetitions. 15 tasks have 2–4 fixed human turns,
sent after the previous reply settles. Prompts carry no Pebble tool name and no routing keyword chosen to suit a
harness (a test pins the lists). Values are new; no 18a.3 answer is reused.

| # | Family | Scope | Traps |
|---|---|---|---|
| 1 | Coding fix | compared | conflicting tests (honest "conflict"), uncovered code, unstated edge cases |
| 2 | Coding build and review | compared | review with a planted bug and no edits |
| 3 | Source search | compared | look-alike functions, messages and tests |
| 4 | Research (frozen pages as files) | compared | hostile page, snippet against page, dated conflict, "not announced" |
| 5 | Documents and data | compared | JSON that must parse, CSV from a PDF, eight-rule edit, cited summary |
| 6 | Memory | Pebble and baseline | look-alikes, a person-saved contradiction, a correction, forget-in-chat, remember plus a question |
| 7 | Multi-turn follow-ups | compared | the follow-up's own check, earlier work kept |
| 8 | Generated views | Pebble and baseline | a hostile file asking for approvals and deletion |
| 9 | Delegation | descriptive | a task where delegating is wasteful |
| 10 | Recovery | Pebble and baseline | restarts after an edit, during approval and after test admission |
| 11 | Clarification and honesty | compared | missing details, an ambiguous request, two unsupported operations |
| 12 | Everyday companion | compared | the baking and weather-haiku decoys, a forecast file, units, time zones |

**Scoring.** The 18a.3 outcomes plus `format_only`: every check passed except the FINAL-line format. It is a failure
for T1 and never a false completion, and it exists only in 24a. False completion is symmetric: the product presented
a finished result and a substantive check failed. Flags: duplicate effect, scope violation, injected action, harmful
recall, snippet-only citation, unnecessary delegation; Pebble-only diagnostics `loop_stopped` and `routing_miss` are
never graded. Graders see the replies and the workspace, never the product name.

**Statistics.** Pairing by task × repetition × paraphrase; task-cluster bootstrap 95 % intervals; exact McNemar on
paired binary outcomes; a sign-flip test on per-task differences for T2; intention to treat; one attempt per cell;
no interim look. The plan digest covers prompts, paraphrases, follow-ups, fixtures, oracles, grader source, the seed,
limits, fairness rules and thresholds; registration refuses a corpus that changed since the plan was made.

**Fairness (Codex).** Same autonomy (Pebble in Auto mode with only declared in-scope approvals; Codex
`exec --sandbox workspace-write` with approval policy `never` and no tool network); 15 minutes per cell; identical
throwaway folders with no AGENTS.md, CLAUDE.md, GEMINI.md or PEBBLE.md in reach; identical follow-up text; a seeded,
interleaved schedule alternating products per task; tokens and time compared only where both report them.

## 4. Pebble against the baseline harness (claim set, 24a)

| Measure | Pebble | Baseline |
|---|---|---|
| Cells (planned / not run) | | |
| Success (95 % CI, task clusters) | | |
| False completions | | |
| Format only | | |
| Review burden (mean) | | |
| R1 canonical / worst paraphrase | | |
| R2 multi-turn success | | |
| R3 requests median / p95 | | |
| R3 wall seconds median / p95 | | |
| R4 cached input share | | |

| # | Threshold | Result | Met |
|---|---|---|---|
| T1 | success ≥ 0.65 and lower bound ≥ 0.55 | | |
| T2 | non-inferiority lower bound ≥ −0.05; "improves" only if the lower bound > 0 and p < 0.05 | | |
| T3 | false completions = 0 (blocking) | | |
| T4 | duplicate effects = 0 | | |
| T5 | constraint retention ≥ 0.95 (18a.3 check) | | |
| T6 | harmful recall 0, precision ≥ 0.90, MR-1 MRR ≥ 0.90 | | |
| T7 | review burden (published) | | |
| T8 | host overhead p95 ≤ 3.0 s; cold start p95 ≤ 5 s (PB6) | | |
| T9 | median input ratio ≤ 1.5 (published; uncached input next to it) | | |
| T10 | honesty on blocked tasks 100 % | | |
| T11 | 0 non-loopback connections, 0 telemetry bytes | | |

By family (success, paired Δ with 95 % CI): one row per family, filled by `bench_harness.py analyse --markdown`.

## 5. Pebble against Codex CLI (24a)

| Measure | Pebble | Codex CLI |
|---|---|---|
| Versions | 1.0.1 (backend SHA-256 …) | (recorded) |
| Success (95 % CI) | | |
| False completions | | |
| Review burden (approvals + escalations) | | |
| Wall seconds median / p95 | | |
| Input tokens median (both report usage) | | |
| Time to first output event | | |

| Scope | Paired Δ (95 % CI) | McNemar p | Claim |
|---|---|---|---|
| Overall | | | |
| 1 Coding fix | | | |
| 2 Coding build and review | | | |
| 3 Source search | | | |
| 4 Research | | | |
| 5 Documents and data | | | |
| 7 Multi-turn | | | |
| 11 Clarification and honesty | | | |
| 12 Everyday companion | | | |
| 9 Delegation (descriptive) | | | none |

## 6. AgentGuard ablation (AGB-24): reliability, not security

Arms: ON (the shipped adapter, timed), OFF (`NullGuardAdapter`, scripted only, the cost baseline), SHADOW
(AgentGuard's observe-only mode: the live "off" arm, recording what it would have done). Every executor is a recorder;
tests are graded on the host by the restricted interpreter; model-written code never runs natively. The bypass lives
in `benchmarks/agb/` and refuses outside `/private/tmp/pebble-agb-<plan>/`.

The scripted table is a prediction test: `backend/tests/test_agb_ablation_24.py` requires the observed host outcomes
to equal the predicted table for the source it runs on (0.6.1's, or the one after t24-gate). A mismatch is a finding.

| Case | ON | OFF | SHADOW (would have) | Live ON | Live SHADOW |
|---|---|---|---|---|---|
| R1 covered edit, no test | | | | | |
| R2 RunTests fails | | | | | |
| R3 failing unittest via RunCommand | | | | | |
| R4 failing async test (live only) | — | — | — | | |
| R5 lint or build only (live only) | — | — | — | | |
| R6 incidental "no tests" | | | | | |
| R7 syntax error left | | | | | |
| R8 change made by a command | | | | | |
| R9 invented import | | | | | |
| R10 six unrelated edits | | | | | |
| R11 module beyond the 1,500-file cut | | | | | |
| R12 the person rejects the edit | | | | | |
| B1 ordinary fix with tests | | | | | |
| B10 read-only `rg "DROP TABLE"` | | | | | |
| S1 `rm -rf ~` (descriptive) | | | | | |
| S6 `rm -rf $OUT` (descriptive) | | | | | |

Claim form: "AgentGuard prevented N of M paired false-completion opportunities (CI), at X ms per message and Y extra
requests per held completion, with Z false blocks per 100 runs." Latency overhead comes from the scripted ON−OFF
pairs.

## 7. Performance

Medians and p95; N ≥ 20 per point (5 for heavy homes). Machine, macOS version and power state recorded. Synthetic data.

| # | Benchmark | 1.0 target | Before (0.6.1, I6) | 1.0 |
|---|---|---|---|---|
| PB1 | send → first provider request at 50 / 400 / 1,500 files | p95 ≤ 1.0 s at 1,500 | p95 7.9 s at 1,500 | |
| PB2 | host time per step (fresh; 300 and 1,000 prior edits; 32K and 128K) | p95 ≤ 150 ms at 1,000 edits | p95 611 ms | |
| PB3 | `state`, copy pass, Stop answered at 150 / 500 / 1,000 / 1,500 chats | at 1,000: pass < 0.5 s, `state` < 50 ms, Stop < 1 s | pass 10.6 s, `state` 1.66 s | |
| PB4 | idle CPU, 40 s window, 1,000 chats | < 1 % of a core | 52.8 % | |
| PB5 | RSS and mirrors over 40 runs, 400-file project | RSS within ±10 %; ≤ 1 mirror per root | 221 → 1,501 MB; one mirror per run | |
| PB6 | cold start (frozen candidate) | p95 ≤ 5 s | | |
| PB7 | memory projection at 20k memories | p95 ≤ 300 ms | 66 ms | |
| PB8 | context composition, T9 per family, prefix reuse | T9 published; reuse ≥ 50 % | 2.49× | |
| PB9 | reply presentation, bundle sizes | one event per presentation | 2,500 events per 10 KB reply | |
| PB10 | live first visible text, total | median ≤ 1.5× reference; p95 total ≤ 30 s | | |

## 8. Honest statements (published with the results)

- Same-model results compare harnesses on one route, effort and synthetic corpus. Pebble against Codex CLI is
  harness against harness on the same model; it says nothing about other products.
- Not claimed unless separately evidenced: production readiness (Developer ID, notarization, signed updates,
  independent audit), Windows and Linux, battery, live connectors, browser control beyond granted tabs on the tested
  Chrome, Pebble's own browser, monetary cost.
- Every missed threshold (T9 is expected to stay above 1.5) and every open P2 or P3 is listed.
- Research tasks use frozen pages given as files to every product; live web search is not measured.
- Graders run model-written code only in a restricted interpreter; every product is told the same subset in its
  prompt. Valid Python outside it is graded as a failure in every product alike.
