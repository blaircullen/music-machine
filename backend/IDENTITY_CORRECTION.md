# Identity Correction Pipeline (Phase 2)

Artist-tag correction driven by the resolver verdicts in `track_identity`.
Field-scoped, kill-switch-gated, snapshotted, fully reversible. Runs inside the
`music-machine` container (`docker exec -w /app music-machine python <script>`).

## Safety model
- Nothing writes unless `settings.identity_act_enabled = 'true'` (the kill
  switch). `tagger.write_metadata` re-checks per file; reads fail closed.
- Every write is preceded by `tag_backup.snapshot_tags`, so any change reverts
  via `correction_pass.rollback_correction(id)`.
- Each change is logged to the `identity_corrections` audit table
  (status pending|applied|failed, old/new artist, snapshot_id, sha before/after).

## Scripts
- `correction_pass.py` — core. `run(track_ids=..., fields=('artist',), dry_run=)`.
  CLI: `--ids-file F --fields artist [--apply] [--rollback ID]`. Dry-run is the
  default; `--apply` writes. `fields` restricts which tags may change.
- `category_select.py <category> [out_file]` — read-only. Emits track_ids for
  `fill_empty_artist | concat_feat_artist | artist_spelling | artist_reattribution
  | all_artist`. Excludes trash/staging paths.
- `apply_batch.py <ids_file>` — flips kill switch on (inside try), applies
  artist-only corrections for the ids, ALWAYS re-locks in finally, verifies.
- `rollback_ids.py <id> [id ...]` — switch-gated rollback of specific corrections.

## Typical batch
```
python category_select.py concat_feat_artist /app/ids.txt          # pick
python correction_pass.py --ids-file /app/ids.txt --fields artist  # dry-run review
python apply_batch.py /app/ids.txt                                 # apply (gated)
```

## Notes / open items
- WAV files fail (`write_metadata` uses Vorbis-style keys; WAV needs ID3 frames).
- `conflict` rows have no PICK — not auto-fixable (manual).
- `album_only_change` is likely compilation-pressing noise — skip.
- Kill-switch flip: `UPDATE settings SET value='true'|'false' WHERE key='identity_act_enabled'`.
