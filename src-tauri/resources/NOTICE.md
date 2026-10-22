# Pebble notices

Pebble
Copyright 2026 Tohin Majumder

Pebble is licensed under the Apache License, Version 2.0. The licence text is `resources/LICENSE`.

## Third-party software in this app

The complete licence and notice texts of every component this app ships are in `resources/licenses/`:
`THIRD-PARTY-NOTICES.txt` is generated from the exact build inputs (the Rust crates in `Cargo.lock`, the production
npm packages in `package-lock.json`, the Python distributions in `backend/requirements.lock`, CPython and the
PyInstaller bootloader), and the other files there are the texts named below.

- **AgentGuard** (Apache-2.0), vendored as Pebble's local reliability engine. Its licence is
  `resources/AGENTGUARD-LICENSE`; the files Pebble changed are listed in `backend/vendor/PROVENANCE.json`.
  The adapter and security broker are original Pebble code. AgentGuard is not a security sandbox.
- **bloub** by Jérémy Perret (https://github.com/jeremy-prt/bloub), pinned revision
  `b4bb3c1b5f93c7b87a2e8d620f667c4093d97749`, MIT: the animated pebble geometry and animation engine, used with a
  custom Vue renderer. Licence: `resources/licenses/bloub-LICENSE`. The app icon is a new vector drawing inspired by
  the selected pebble avatar.
- **llama.cpp** (https://github.com/ggml-org/llama.cpp), release b11499, MIT: the local inference runtime. Licence:
  `resources/licenses/llama.cpp-LICENSE`.
- **CPython** (PSF-2.0, with the licences of the libraries it incorporates, among them OpenSSL) and the
  **PyInstaller** bootloader (GPL-2.0-or-later with the PyInstaller bootloader exception, which permits distributing
  the frozen application under its own terms) run Pebble's local service.
- **Pillow** decodes raster attachments. HEIC and HEIF photos are decoded by macOS itself (ImageIO, through
  `/usr/bin/sips` in the confined parser); no HEIF library ships with Pebble.
- The interface uses Tauri, Vue, marked, DOMPurify, Phosphor Icons and Sucrase; the local service uses Pydantic,
  PyYAML, pypdf, certifi, tree-sitter, jsonschema, PyJWT, cryptography, tiktoken and their dependencies. Each keeps its
  own licence, reproduced in `THIRD-PARTY-NOTICES.txt`.

## Tokenizer

Local estimates of cloud-context size use the `o200k_base` vocabulary of OpenAI's tiktoken (MIT), bundled in
`resources/tokenizer/` so that the app never downloads it. Source: the file tiktoken itself fetches,
`https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken`, SHA-256
`446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d` (checked at load by
`backend/pebble/token_budget.py`). Licence basis: OpenAI publishes it for use with tiktoken; no separate licence is
stated for the vocabulary file, so it is redistributed unchanged on the same basis as tiktoken itself.

## Models

No model weights are included in this app. The default local model, downloaded only on request, is
**FrogNano-4B-2609** by Microsoft (https://huggingface.co/microsoft/FrogNano-4B-2609, MIT), a derivative of the base
model **Qwen3.5-4B** (https://huggingface.co/Qwen/Qwen3.5-4B), in the community Q4_K_M GGUF pinned in
`resources/model.json`. Its licence and the base model's terms are those on the two model cards. Online inference
is optional and uses the provider and account the person chooses.

## Services

Pebble's web search sends search words to Microsoft Bing's RSS results feed, and its weather answers use the
Open-Meteo API, whose free service is for non-commercial use (https://open-meteo.com/en/terms). Neither is affiliated
with Pebble.
