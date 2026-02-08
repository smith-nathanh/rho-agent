---
title: FAQ
description: Common setup and usage questions.
order: 11
---

## Why does docs sync use my local `rho-agent` checkout?

For local development speed. CI/production sync from GitHub.

## How do docs get published to the website?

Changes merged to `rho-agent/main` under `docs/site/**` trigger a dispatch workflow that tells `rho-site` to rebuild.

## Why are my docs changes not visible on the site?

Check:

1. Docs were committed and pushed to `main`.
2. `rho-agent` workflow ran successfully.
3. `rho-site` dispatch workflow ran successfully.
4. Deploy hook secret is configured (if your host requires explicit deploy trigger).

## Can I keep internal notes in `docs/`?

Yes. Keep public website docs in `docs/site`, and internal notes in other paths such as `docs/internal`.
