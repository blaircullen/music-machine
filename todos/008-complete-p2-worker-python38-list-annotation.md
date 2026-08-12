---
status: pending
priority: p2
issue_id: "008"
tags: [bug, code-review, sonic-analyzer, python-compat]
---

# Generic Type Annotation list[float] Breaks Python <3.9 in worker.py

## Problem Statement
The Essentia Docker image (`ghcr.io/mtg/essentia:latest`) ships Python <3.9. CLAUDE.md explicitly warns: "Do NOT use generic type annotations (`set[int]`, `tuple[X, Y]`, `list[str]`) — these require 3.9+." `worker.py:112` uses `list[float]` as an inline annotation, which raises `TypeError` on Python 3.8.

## Findings
- **File:** `sonic-analyzer/worker.py:112`
```python
vector: list[float] = []  # ← TypeError on Python 3.8
```
- The rest of the file correctly avoids generic annotations (line 46 uses `# type: ignore`, line 93 uses old-style comment annotations)

## Proposed Solution

**Option A (Recommended) — Use typing import:**
```python
from typing import List
vector: List[float] = []
```

**Option B — Use old-style comment:**
```python
vector = []  # type: List[float]
```

**Option C — Remove annotation entirely:**
```python
vector = []
```
The type is obvious from context; the annotation adds no runtime value.

## Acceptance Criteria
- [ ] `worker.py` imports and runs without error in the Essentia container (Python <3.9)
- [ ] No other `list[...]`, `dict[...]`, `set[...]`, or `tuple[...]` generic annotations in `sonic-analyzer/worker.py`

## Effort: Tiny
