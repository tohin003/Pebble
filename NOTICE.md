# Pebble notices

Pebble
Copyright 2026 Tohin Majumder

Pebble is licensed under the Apache License, Version 2.0. The licence text is [LICENSE](LICENSE). The app carries the
same notices in `Contents/Resources/resources/` (`NOTICE.md`, `LICENSE` and `licenses/`), built from
[src-tauri/resources/NOTICE.md](src-tauri/resources/NOTICE.md).

## Third-party software

The complete licence and notice texts of every component the app ships are in `src-tauri/resources/licenses/`.
`THIRD-PARTY-NOTICES.txt` there is generated for each release from the exact build inputs (the Rust crates in
`src-tauri/Cargo.lock`, the production npm packages in `package-lock.json`, the Python distributions in
`backend/requirements.lock`, CPython and the PyInstaller bootloader) by `scripts/release_inventory.py notices`; the
other files there are the texts named below.

- **AgentGuard** (Apache-2.0), vendored in `backend/vendor/agentguard/` as Pebble's local reliability engine. Its
  licence is `backend/vendor/AGENTGUARD-LICENSE` (in the app, `resources/AGENTGUARD-LICENSE`). The files Pebble changed
  carry a "Modified for Pebble" header and are listed in `backend/vendor/PROVENANCE.json`. The adapter and security
  broker are original Pebble code. AgentGuard is not a security sandbox.
- **bloub** by Jérémy Perret (https://github.com/jeremy-prt/bloub), pinned revision
  `b4bb3c1b5f93c7b87a2e8d620f667c4093d97749`, MIT: the animated pebble geometry and animation engine, vendored in
  `src/vendor/bloub/` with its licence and used with a custom Vue renderer. Licence: `src/vendor/bloub/LICENSE` (in the
  app, `resources/licenses/bloub-LICENSE`). The app icon is a new vector drawing inspired by the selected pebble
  avatar.
- **llama.cpp** (https://github.com/ggml-org/llama.cpp), release b11499, MIT: the local inference runtime. It is not in
  this source tree; the build fetches it against its pin in `config/model.json`. Licence:
  `src-tauri/resources/licenses/llama.cpp-LICENSE`.
- **CPython** (PSF-2.0, with the licences of the libraries it incorporates, among them OpenSSL) and the **PyInstaller**
  bootloader (GPL-2.0-or-later with the PyInstaller bootloader exception, which permits distributing the frozen
  application under its own terms) run Pebble's local service in the app.
- **Pillow** decodes raster attachments. HEIC and HEIF photos are decoded by macOS itself (ImageIO, through
  `/usr/bin/sips` in the confined parser); no HEIF library ships with Pebble.
- The interface uses Tauri, Vue, marked, DOMPurify, Phosphor Icons and Sucrase; the local service uses Pydantic,
  PyYAML, pypdf, certifi, tree-sitter, jsonschema, PyJWT, cryptography, tiktoken and their dependencies. Each keeps its
  own licence, reproduced in `THIRD-PARTY-NOTICES.txt`.

## Tokenizer

Local estimates of cloud-context size use the `o200k_base` vocabulary of OpenAI's tiktoken (MIT). It is not in this
source tree: builders fetch it once (see [BUILDING.md](BUILDING.md)), and the app bundles it in `resources/tokenizer/`
so that it never downloads it. Source: the file tiktoken itself fetches,
`https://openaipublic.blob.core.windows.net/encodings/o200k_base.tiktoken`, SHA-256
`446a9538cb6c348e3516120d7c08b09f57c36495e2acfffe59a5bf8b0cfb1a2d` (checked at load by
`backend/pebble/token_budget.py`). Licence basis: OpenAI publishes it for use with tiktoken; no separate licence is
stated for the vocabulary file, so it is redistributed unchanged on the same basis as tiktoken itself.

## Models

No model weights are included in this source tree or in the app. The default local model the project tests with is
**FrogNano-4B-2609** by Microsoft (https://huggingface.co/microsoft/FrogNano-4B-2609, MIT), a derivative of the base
model **Qwen3.5-4B** (https://huggingface.co/Qwen/Qwen3.5-4B), in the community Q4_K_M GGUF pinned in
`config/model.json`. Its licence and the base model's terms are those on the two model cards. Online inference is
optional and uses the provider and account the person chooses.

## Services

Pebble's web search sends search words to Microsoft Bing's RSS results feed, and its weather answers use the
Open-Meteo API, whose free service is for non-commercial use (https://open-meteo.com/en/terms). Neither is affiliated
with Pebble.
