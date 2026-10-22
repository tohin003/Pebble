# Security policy

## Reporting a vulnerability

Please report security problems privately, through GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**
(https://github.com/tohin003/Pebble/security/advisories/new). Do not open a public issue, pull request or discussion
for a vulnerability.

Include what you can:

- the Pebble version (in Finder, Get Info on Pebble.app) and your macOS version;
- what an attacker needs (for example: a web page Pebble reads, a file in a project, a connector, a shared Chrome tab,
  or code already running on the Mac) and what they gain;
- steps to reproduce, ideally with synthetic data. Never send real keys, tokens or personal data.

Pebble is maintained by one person. Please allow time for a reply and a fix, and keep the details private until a fixed
release is out or 90 days have passed, whichever comes first. You will be credited in the release notes unless you ask
not to be.

## Supported versions

| Version | Supported |
| --- | --- |
| 1.0.x (the latest release) | Yes |
| Earlier versions (local previews, never published) | No |

Security fixes ship as a new release; update by running the installer again.

## Scope

In scope:

- the app: the native shell, its route allowlist and windows, the local service, its tools, permissions, sandboxes,
  memory, storage and providers;
- Pebble for Chrome and its native messaging host;
- the Keychain helper, the installer (`install.sh`), and the build, signing, install and release scripts;
- anything that lets a model, a web page, a file, a memory, a connector or a generated view widen what Pebble may do,
  approve an action, read Pebble's own folders or secrets, or send data somewhere the person did not choose.

Out of scope, though still welcome as ordinary reports if Pebble could do better:

- problems in third-party services Pebble talks to (model providers, Microsoft Bing, Open-Meteo, connectors you add);
- attacks that need code already running as your macOS user, beyond what [the architecture](docs/ARCHITECTURE.md)
  says Pebble defends against;
- a model giving a wrong or unsafe answer that Pebble's checks then stop.

## What Pebble does and does not promise

- Approvals happen only in Pebble's main window; nothing a model or a page says can grant access.
- Commands, tests and document parsing run under macOS's `sandbox-exec` with deny-by-default profiles. That limits
  them; it is not a guarantee against every attack, and `sandbox-exec` is an older macOS interface.
- Prompt injection from pages and files is contained by these boundaries, not made impossible.
- AgentGuard checks evidence such as test runs; it is a reliability layer, not a security layer.
- Releases are signed with the project's own stable identity, not an Apple Developer ID, and are not notarized.
  `SHA256SUMS` shows that a download is intact, not who made it; it is not signed.
- Pebble does not defend against other software running as your own macOS user. Such software can read Pebble's data folder and change its settings, and it can start Pebble's own backend so that the backend reads your saved API key or ChatGPT sign-in without a prompt and sends it to a server named in settings that software controls. The macOS Keychain cannot tell this apart from Pebble's own use. After 1.0, each saved secret will be bound to its destination inside its Keychain record, as connector credentials already are (`docs/HARNESS_SIGNING_IDENTITY.md`, "Limits of the check").
- No independent security audit has been done.
