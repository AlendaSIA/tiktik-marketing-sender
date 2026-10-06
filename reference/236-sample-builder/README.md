# 236 hand-built sample builder + guarded job runner (reference only, 2026-10-06)

Copies of two ops scripts that live in gs://jaunais-za-aizv04022026-vps-deploy/src-staging/ . NOT imported by anything,
NOT pinned by the job wrapper. Kept here so the next conversation can read them.

- vs6-236-mkfill.py - builds the hand fill for ONE contact for the ZARYS week of letter 236 (own-goods block, Plānie
  cimdi top 12, Biezie cimdi top 4, Citas ZARYS preces top 4 with the diversity rule, Teipi). The sample Raivis
  approved 06.10 16:48 came from it. It is a sample tool: the per-contact 236 is rebuilt on contract fields on MAIN's block.
- vs7-guard.py - runner for Cloud Run job tiktik-draft-test: state | lock | proof-term | proof-expiry N |
  exec '<json args>' | send '<json args>'. Relocks in finally + on SIGTERM/INT/HUP; the open wrapper expires after 300 s.
