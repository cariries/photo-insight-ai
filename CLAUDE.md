# Photo Insight

Offline photo classifier and hashtagger. See `README.md` for usage and `docs/PLAN.md` for epics and decisions (update the decisions log when making a non-obvious choice).

- Layout: `photo_insight/` (config, llm, images, hashtags, store, pipeline, export, server, cli) and `static/` (vanilla JS UI, no build step).
- Run: `.venv/bin/photo-insight serve`. Tests: `.venv/bin/python -m pytest -q`. Use `PHOTO_INSIGHT_HOME=<tmp>` to avoid touching the user's `~/.photo-insight`.
- Never modify original photos. Never overwrite XMP sidecars not written by us (`x:xmptk="Photo Insight"`).
- Any HTTP client to localhost must bypass the shell proxy (`trust_env=False` / `NO_PROXY=127.0.0.1,localhost`).
- Don't commit real user photos; `samples/` is git-ignored.
