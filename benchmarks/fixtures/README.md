# Benchmark fixtures

No fixture file is committed here. Every fixture is generated from source, so its bytes are covered by the plan
digests:

- **Corpus 24a**: `benchmarks/corpus_24a/fixtures.py` (text files, one ASCII PDF built by `make_pdf`, a 40-file
  source-search repository with look-alikes). The values are new; none of 18a.3's answers is reused (a test checks).
- **Corpus 24g.1**: `benchmarks/corpus_24g/__init__.py` (a small Python project; R11 adds 1,600 modules so the
  AgentGuard mirror's 1,500-file cut falls inside a real package, plus a dependency manifest).
- **Performance**: `benchmarks/perf/pb.py` generates synthetic projects and seeds synthetic chats.

The drivers copy fixtures into a fresh throwaway folder per cell under `/private/tmp/pebble-bench-<plan>/` (24a) or
`/private/tmp/pebble-agb-<plan>/` (ablation). No instruction file (AGENTS.md, CLAUDE.md, GEMINI.md, PEBBLE.md) may
exist in a cell's folder or any folder above it.
