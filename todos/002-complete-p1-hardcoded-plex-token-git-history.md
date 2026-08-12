---
status: pending
priority: p1
issue_id: "002"
tags: [security, code-review]
---

# Hardcoded Plex Token in Source / Git History

## Problem Statement
The Plex token `mxrEzLiMjZ1FftGMZaiq` is hardcoded as the default fallback in two files and is now permanently embedded in git history on `feature/sonic-analysis`. If this branch is ever pushed to a public remote, the token is exposed — Plex tokens are long-lived and grant full library access.

## Findings
- `backend/routes/sonic.py:27`: `PLEX_TOKEN = os.environ.get("PLEX_TOKEN", "mxrEzLiMjZ1FftGMZaiq")`
- `backend/feedback_service.py:33`: `plex_token = os.environ.get("PLEX_TOKEN", "mxrEzLiMjZ1FftGMZaiq")`
- Note: `MEMORY.md` lists a different token (`fzVAhz-21g7CfJvA7jK8`). Two different tokens are now in source — one may be a secondary/legacy token.

## Proposed Solution

**Option A (Recommended) — Remove hardcoded fallbacks:**
```python
PLEX_TOKEN = os.environ.get("PLEX_TOKEN")
if not PLEX_TOKEN:
    raise RuntimeError("PLEX_TOKEN environment variable is required")
```
Ensure `docker-compose.yml` / `.env` supplies the value.

**Option B — Rotate the token:** If removing the fallback is too disruptive now, at minimum rotate the exposed token in Plex Settings → Manage → Authorized Devices and update docker-compose.yml. Then remove the hardcoded default in a follow-up.

## Acceptance Criteria
- [ ] No hardcoded token string in any source file
- [ ] App startup fails with a clear error if `PLEX_TOKEN` is not set
- [ ] Token supplied via `docker-compose.yml` env section or `.env` file (gitignored)
- [ ] If branch will be made public: token rotated in Plex before push

## Effort: Small
