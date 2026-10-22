# Phase 16 hostile fixtures (T7)

These are small, deterministic, untrusted test inputs for the gates in the Phase 16 threat model (`docs/HARNESS_PHASE16_THREAT_MODEL.md`, a maintainers' record that is not published).

- Nothing here is fetched, resolved or executed against the network. Hosts are served by local fakes.
- The 2 MB page and the PDF bombs are generated in memory by `kit.py`, so the folder stays well under its 200 KB budget.

## Layout

| Path | Cases | Gate |
| --- | --- | --- |
| `web/hidden_injection.html`, `web/injection_obfuscated.html` | W1, W1b | G1 |
| `web/fake_dates.html`, `web/fake_dates_fresh_claim.html` | W2, W2b | G2 |
| `web/redirect_chains.json` | W3 (24 chains) | G3 |
| `web/large_page.json` | W4 (generated; sha256 pinned) | G4 |
| `web/pdf_bombs.json` | W5 (six kinds, five generated) | G5 |
| `web/conflict_primary.html`, `web/conflict_secondary.html`, `web/conflict_mirror.html` | W6 | G6 |
| `web/rss_unsafe_links.xml`, `web/rss_doctype.xml` | W7, W7b | G7 |
| `web/child_claims.json` | W8 | G8 |
| `web/memory_bait.html` | W9 | G9 |
| `web/cases.json` | Expectations for every web HTML and RSS file | — |
| `ui/ax_click_send.json` | U1 | G10 |
| `ui/ax_mislabelled_secure.json` | U2 | G11 |
| `ui/ax_moved_element.json` | U3 | G12 |
| `ui/ax_spoofed_title.json` | U4 | G13 |
| `ui/ax_autosubmit_form.json`, `ui/autosubmit_form.html` | U5 | G14 |
| `ui/navigation_during_act.json` | U6 | G15 |
| `ui/unsupported_targets.json` | U7 | G16 |
| `connectors/draft_email_is_send.json` | C1 | G17 |
| `connectors/recipient_injection.json` | C2 | G18 |
| `connectors/token_echo.json` | C3 | G19 |
| `connectors/ignores_base_url.json` | C4 | G20 |
| `connectors/revocation_mid_send.json` | C5 | G21 |
| `connectors/duplicate_after_restart.json` | C6 | G22 |
| `cross/exposure_texts.json` | X1 | G23 |
| `manifest.json` | Every case: gate, owners, files, planned test names, calibration | — |
| `kit.py` | Loaders and generators | — |
| `check_fixtures.py` | Fixture self-check (below) | — |

## Formats

- **`pebble-p16-fixture/1`** (web and cross-cutting). Each case has an `expect` block and a `required` block:
  - `expect` is what the owning module does today;
  - `required` is the gate;
  - `finding` explains any gap between them, or a gap fixed at merge (F1, F2, F4) with the expectation it replaced.
- **`pebble-p16-fake-ax/1`** (UI). It uses macOS AX attribute names (`AXRole`, `AXSubrole`, `AXTitle`, `AXDescription`, `AXHelp`, `AXValue`, `AXIdentifier`, `AXFrame: [x, y, w, h]`, `children`).
  - Keys starting with `_` are fixture annotations, never AX attributes. `_ref` names an element so a test can map it to T3's `e<n>@<observation_id>`.
  - Browser variants use a DOM element list and CDP method/params names.
  - Times are seconds relative to the observation, and `clock` is the host clock.
- **`pebble-p16-fake-mcp/1`** (connectors). Each case carries:
  - the server's tool specs and the person's reviewed `effects` map;
  - human turns, untrusted outputs and the model's calls;
  - JSON-RPC result shapes matching T5's loopback fake.

  The token is a fixed fake value.

All cases use the host clock `1791800000.0` (2026-10-12T09:33:20Z). Open items (`O<n>`), residuals (`R<n>`) and findings (`F<n>`) refer to the threat model.

## Loading

Load `kit.py` by path, so no installed package named `fixtures` can shadow it:

```python
import importlib.util
from pathlib import Path

FIXTURES = Path(__file__).parent / 'fixtures' / 'phase16'
spec = importlib.util.spec_from_file_location('p16_kit', FIXTURES / 'kit.py')
kit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(kit)

page = kit.large_page(kit.load('web/large_page.json'))   # 2.1 MB, built in memory
```

The kit provides:

- `kit.load`, `kit.text` and `kit.data`, which read files and refuse paths outside this folder;
- `kit.pdf(spec)`, which builds a `pdf_bombs.json` kind with pypdf;
- `kit.echo_body(response, token)`, which builds a `token_echo.json` upstream body;
- `kit.fill(text, ids)`, which replaces `{primary}`-style placeholders with observed source IDs.

## Users

`backend/tests/test_phase16_security.py` drives every case against the merged code, one test or more per gate. It loads `kit.py` by path and compiles it in memory, so no bytecode lands here. UI cases go through a fake AX backend (converted from `pebble-p16-fake-ax/1`), and through T4's fake CDP peer for browser variants. Connector cases go through loopback MCP servers, and the credential route through an in-process egress broker. When a merged module uses a different refusal code than a first-pass proposal, the fixture records the module's code and the reason.

## Self-check

`check_fixtures.py` checks:

- the layout, the 200 KB budget, and that the manifest, gates, test names and open items match the threat model;
- that the generators are deterministic, and that UI `_ref`s and connector tool names resolve;
- every `calibrated: T1` expectation, against T1's finished `sources.py`, `web.py` and `source_ledger.py` through local fakes.

It is not the security suite, and pytest does not collect it by default. Run it by path:

```sh
.runtime/agent-env/bin/python -m pytest backend/tests/fixtures/phase16/check_fixtures.py --basetemp=<owned temp dir>
```

Python writes `__pycache__/` here (bytecode for `kit.py` and `check_fixtures.py`). The budget excludes it, and it is safe to delete. Add `-B` where the environment allows it.
