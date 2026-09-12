# Working on ProjectZen as a team

Three of us share this repo. Everyone runs the app on their own desktop; there
is no shared server. This page is the whole workflow.

## One-time setup per machine

```bash
git clone https://github.com/Soumen15129/ProjectZen.git
cd ProjectZen
```

Do **not** clone into a OneDrive- or Dropbox-synced folder. The sync client
rewrites files under `.git/` while git is writing to them, which corrupts the
index. Use a plain local path such as `C:\dev\projectzen`.

Backend dependencies:

```bash
pip install -r middleware/requirements.txt
```

Claude authentication — each person uses their own Enterprise seat. There is no
API key anywhere in this project:

```bash
npm install -g @anthropic-ai/claude-code
claude /login
```

Run it:

```bash
Start-ProjectZen.bat
```

Health check: `GET http://localhost:8000/health`.
If generation fails, check `GET http://localhost:8000/health/llm` first — it
almost always means the `claude` CLI is not logged in on that machine.

## What is not in the repo

These are gitignored on purpose and will not arrive with a clone. Get them from
whoever has them, or regenerate:

| Path | What it is |
|---|---|
| `middleware/.env` | Local overrides. Usually not needed — see `.env.example`. |
| `storage/` | SQLite DB and generated files. Recreated automatically on startup. |
| `Input/`, `REF FILE/` | **Client source documents.** Never commit these. |
| `GENERATED OUTPUT/`, `CLAUDE DESKTOP GENERATED/` | Generated deliverables. |
| `PRESENTATION/`, `LOG/` | Decks and local logs. |
| `installer/payload/`, `installer/python/`, `installer/wheels/` | Build artefacts, ~2.5 GB. |

**This repository is public.** Client documents, client names, and anything from
a live engagement must stay on your desktop. CI fails the build if any of the
paths above become tracked, but do not rely on that — the guard catches
accidents, not deliberate overrides.

## Making a change

`main` is protected: no direct pushes, no force pushes. Everything goes through
a pull request with one approval.

```bash
git checkout main
git pull                          # always start from current main
git checkout -b describe-the-change

# ... edit, test locally by actually running the app ...

git add -p                        # stage deliberately, not with -A
git commit -m "Explain why, not just what"
git push -u origin describe-the-change
gh pr create --fill
```

Then ask one of the other two to review. Once approved and CI is green, merge,
and delete the branch.

Keeping up to date with others' work:

```bash
git checkout main && git pull
```

## What CI checks

The `Guard` workflow runs on every push and PR. It does **not** test document
generation — a GitHub runner has no signed-in `claude` seat, so generation
cannot run there. What it does check:

- **Client data and secret scan** — fails if any client-data path or a `.env`
  file becomes tracked, or if a credential pattern appears in file contents.
- **Python syntax check** — compiles every module under `middleware/`, and
  asserts every mandatory import resolves to stdlib, a `requirements.txt` entry,
  or a committed file.

That second check exists because this repo was previously in a state where
`llm_client.py` was imported everywhere but had never been committed — a fresh
clone could not start. If you add a new module, commit it; if you add a new
dependency, add it to `requirements.txt`. CI will tell you if you forget.

Because `main` requires these checks to pass, a red build blocks the merge. Fix
it on your branch rather than merging around it.

## Testing before you open a PR

There is no full automated test suite; generation needs a live Claude seat.
Before requesting review, run the app and exercise the path you changed. For
backend-only changes:

```bash
python -m compileall -q middleware
python -m pytest middleware/tests -q     # smoke tests only
```

## Conventions

- Read [CLAUDE.md](CLAUDE.md) before touching `middleware/`. It explains the
  architecture, and `knowledge_graph.py` is the source of truth for cascade
  behaviour — read that module first if you are changing anything cascade-related.
- `frontend/index.html` embeds `APP_HTML` as a JS template literal. **Any
  backslash in JS inside that literal is silently eaten by the parser.** Write
  those sections without backslashes.
- Prefer small PRs. Three people on one codebase with limited test coverage
  means large merges are where things break.
