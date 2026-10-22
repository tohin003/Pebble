# Local model selection

Pebble can run a model on your Mac with the bundled llama.cpp runtime (**Settings › Models › Add a model file**). It
downloads no model by itself: you choose a GGUF file. This page records which model the project tests with, and why.

## The default the project tests with

**Microsoft FrogNano-4B-2609, Q4_K_M GGUF**, pinned in [config/model.json](../config/model.json) with its exact file and
SHA-256. It was chosen on 8 October 2026 as a provisional default for coding and engineering help on an 8 GB Mac. It is
a fit for that purpose, not a claim that FrogNano is the best small model on every benchmark.

`scripts/bootstrap_model.py` downloads it into `models/` (about 2.8 GB) after checking its SHA-256, together with the
pinned llama.cpp runtime.

## Why this model

FrogNano is a Qwen3.5-4B derivative released on 22 September 2026. Its additional training uses complete
software-engineering interactions with tools and test feedback, which matches what Pebble asks of a local model.
Microsoft's same-harness evaluation reports SWE-bench Verified average single-attempt success rising from 39.4% for
Qwen3.5-4B to 61.5% for FrogNano. Those runs used about 131K of context and 150 steps; they do not predict results with
Pebble's 4K local profile or with this community quantization.

Sources: [Microsoft model card](https://huggingface.co/microsoft/FrogNano-4B-2609),
[technical report](https://arxiv.org/abs/2609.07925),
[Leaf reference harness](https://github.com/microsoft/FrogNano).

## Alternatives considered

| Model | Fit | Decision |
| --- | --- | --- |
| FrogNano-4B-2609 | Repository tools, editing, verification; mainly English and Python | Provisional default, tested locally |
| Stock Qwen3.5-4B | Broader documented assistant, reasoning, multilingual and tool abilities | The preferred alternative if general use shows regressions |
| Jan-Code-4B | Small code model with a simple GGUF ecosystem | Viable; no comparable evaluation shows a win |
| Granite-4.0-H-Tiny | Enterprise assistant and tool focus; 7B total weights despite its mixture-of-experts savings | More resident memory than a 4B-class model |

Sources: [Qwen3.5-4B](https://huggingface.co/Qwen/Qwen3.5-4B), [Jan-Code](https://huggingface.co/janhq/Jan-code-4b),
[Granite](https://huggingface.co/ibm-granite/granite-4.0-h-tiny).

FrogNano's general conversation, multilingual and multimodal abilities are not established by its coding training.
Local GGUF models in Pebble read text only.

## The file and the runtime

- Quantization by `bartowski/FrogNano-4B-2609-GGUF`, at the revision pinned in `config/model.json`.
- Size: 2,797,526,816 bytes (about 2.80 GB). Q4_K_M: mixed precision, mostly 4-bit weights.
- The SHA-256 is checked before the file is used.
- Runtime: the official native macOS arm64 llama.cpp release b11499, also checked by SHA-256.

Sources: [quantized file](https://huggingface.co/bartowski/FrogNano-4B-2609-GGUF),
[runtime release](https://github.com/ggml-org/llama.cpp/releases/tag/b11499).

## How Pebble runs a local model

One model at a time, a 4,096-token context, four CPU threads, Metal offloading and flash attention. The model loads when
you send a message and rests after five idle minutes. Quick answers run without thinking; a deliberate repair may think
for up to 512 tokens. These settings favour an 8 GB Mac's memory and latency, and differ on purpose from the published
long-context evaluation.

The runtime listens only on `127.0.0.1`, with a random private key that stays in Pebble's service and never reaches the
interface. The runtime itself runs no tools: Pebble's host checks and approves every action, and AgentGuard checks the
evidence.

## Measured on the project's Mac

On an Apple M2 with 8 GiB of memory, with the model already loaded:

| Check | Result |
| --- | --- |
| Initial scored smoke checks | 6 of 7 passed |
| A chunking bug, after explicit test feedback | Repaired; all four cases passed |
| A synthetic read, edit and test task | Passed in 14.16 seconds |
| Median decode rate on short requests | 23.51 tokens per second |
| First visible text of a short streamed request | 0.306 seconds |
| Retrieval from a 3,068-token synthetic source | Passed in 13.72 seconds |
| Start-up in the recorded run | 17.20 seconds (31.21 seconds on the first installation) |
| A request to the local API without its key | Refused with HTTP 401 |

These are single runs on synthetic tasks, not latency percentiles, a SWE-bench result or a head-to-head comparison.
The first attempt at the chunking task had a real bug (each chunk wrapped in an extra list); the record keeps that
failure rather than a corrected prompt's result. A small local model still makes first-attempt coding mistakes, and
none of these checks shows resistance to prompt injection.
