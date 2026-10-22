# Pebble 1.0 benchmarks

Source-tree only. Nothing here is imported by `backend/pebble` or frozen into the app (a test and a phase-24
validator row check it). Method and result tables: [docs/PEBBLE_V1_BENCHMARKS.md](../docs/PEBBLE_V1_BENCHMARKS.md).
Specification: `docs/PEBBLE_V1_INSPECTION.md` §7.

| Folder | What it holds |
|---|---|
| `corpus_24a/` | 60 tasks in 12 families: canonical prompt plus terse and conversational paraphrases, 15 multi-turn tasks, a trap per family, fresh values; graders that read a product-neutral output; reference solutions (test-only answers) |
| `plan_24a.py` | plans (digest over prompts, paraphrases, follow-ups, fixtures, oracles, grader source, seed and thresholds), registration that refuses a changed corpus, the person's go-ahead stamp, content-free signed results, analysis and report tables |
| `drivers/` | Pebble (owned App), baseline (the 18a loop with a transcript) and Codex CLI cell drivers, throwaway workspaces, the egress allowlist |
| `corpus_24g/` | the AgentGuard ablation cases (R, B, S) with predicted tables for 0.6.1 and after t24-gate |
| `agb/` | the ablation arms (ON timed, OFF `NullGuardAdapter`, SHADOW observe-only), recorder executors and the scripted runner |
| `perf/` | PB1-PB5 measurements; PB6-PB10 name their existing measurers |
| `schema/results.schema.json` | the results format |
| `fixtures/` | where the fixtures come from (all generated) |

## Order of a live campaign (parent-run, after the person's go-ahead, D13)

1. The person signs in the **separate evaluation connection** once (V1-015):
   `scripts/evaluate_harness.py eval-connection --home <eval home> --ledger <ledger> --metadata <file> --confirm-sign-in`.
   It is never the installed app's connection; every live command refuses the app's connection and host ids.
2. For the Codex comparison, the parent installs Codex CLI with approval and the person signs it in under a scratch
   `CODEX_HOME` (never `~/.codex`, no `AGENTS.md` or `config.toml` there).
3. `scripts/bench_harness.py plan --kind harness|codex --claim-set claim --report plan.json`, then
   `register plan.json --ledger <ledger>`, witness the registry head outside the ledger, then, after asking the
   person, `stamp plan.json --ledger <ledger> --decision D13 --statement "..." --confirm-person`.
4. Run cells: `bench_harness.py run plan.json --product pebble|baseline ... --confirm-live` and
   `bench_codex.py run plan.json ... --confirm-live` (dry run first: `bench_codex.py dry-run plan.json`).
5. `bench_harness.py analyse plan.json results/*.json --ledger <ledger> --report analysis.json --markdown tables.md`.

Scripted runs (the tests) validate plumbing and scoring only; they are never quality evidence.
