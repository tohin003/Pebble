"""Pebble 1.0 benchmarks (Phase 24, track t24-bench). Source-tree only: never imported by backend/pebble, never frozen.

- corpus_24a: the 60-task claim corpus (12 families x 5), with paraphrases, multi-turn follow-ups and graders that
  read a product-neutral normalised output.
- corpus_24g: the AgentGuard ablation corpus (AGB-24).
- drivers: the Pebble, baseline and Codex CLI cell drivers.
- agb: the ablation arms and recorder executors (test-only bypass; refuses outside its scratch sandbox).
- perf: the PB1-PB9 measurement helpers.
"""
