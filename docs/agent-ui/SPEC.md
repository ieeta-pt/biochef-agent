# biochef-agent local UI, specification

Status: approved design, ready to build in parts. Owner: Jorge Miguel Silva (`jorgeMFS`).
Written against biochef-agent `master` at `06a30d6`, biochef-coordinator `master` at `614cfe4`, and `ieeta-pt/biochef-coordinator#6` at `89a10d0`, which is the contract for every field name in this document (section 6.7).

This document is the brief for building the page that a node's data manager uses inside a Trusted Research Environment (TRE). Read it whole before writing code. Where a topic is listed as open in section 13, stop and ask; do not guess.

## 0. Material that comes with this spec

| What | Where |
|---|---|
| Design source | MIE 2027 submission, available from Jorge |
| Screenshots of each screen, 1440 px wide | `docs/agent-ui/mockups/01-inbox.png` … `07-node.png` |
| Source of each mock screen (HTML with inline styles, exact colours and spacing) | `docs/agent-ui/mockups/source/*.dc.html` |
| Field names of everything the coordinator stores or relays | `ieeta-pt/biochef-coordinator#6` |
| Team rules for branches and PRs | `biochef-infra/CONTRIBUTING.md` |

The mocks are the target for layout, wording and states, except where section 0.1 says otherwise. The `.dc.html` files are not code to copy as is; they use a design tool runtime (`<x-dc>`, `{{holes}}`, `<sc-if>`) that is not in this repo, so they do not open on their own. Read them for exact colours, sizes, copy and state logic, then rebuild each screen as Jinja2 templates with Tailwind classes.

### 0.1 Where this document overrides the mocks

The images and sources in `mockups/` were not redrawn after this document was corrected. This document wins.

| Mock | Shows | Build this instead |
|---|---|---|
| All | Active menu item on a lighter background | White text at 3.97:1 fails section 5. Use the darker colours of section 5 |
| 01, 02 | `min_cell_count`, "owner" | `min_count`, "proposer" (6.7) |
| 01 | "Approving v3 withdraws v2" | "Approving v3 supersedes v2" |
| 02 | v1 as `Withdrawn` | v1 as `superseded`, shown as "Superseded" |
| 03 | `bcftools:1.21`, an image per step | `repo@digest` per step (6.1). There is no image per step |
| 03, 04 | `gwas-qc 1.4.0` as a workflow name | Name and version exist only for library workflows (6.7). Otherwise show the short digest |
| 04 | Nine checks | Ten checks (6.3) |
| 01 | "Paulo Neves" named in the failed identity alert | A member whose identity failed has no verified name: show the shortened `ls_aai_id` (7.3) |
| 04 | `req_7c03` from 28 Oct under "last 24 hours", rows out of order | Everything listed is inside 24 hours, newest first |
| 06 | Edit only | Edit and Remove (8.6) |
| All | A fixed node name | Whatever `BIOCHEF_NODE_NAME` says. Node names depend on the deployment |
| 07 | "steps run without network" | "Network isolation: off" in warn colours, until part 9b (8.7) |
| 07 | No check of the audit log against the decisions | Add the check of 8.7 |
| 01, 03 | "✓ Signed by <person>" | "Proposed by <proposer>" on a study and nothing on a workflow, until a signature is really verified (section 13) |
| 02 | "automatic, when v2 was approved" | "automatic, when v3 was approved by <user>", naming the version and who approved it |
| 04 | `req_8f22` as a row of its own, "Repeated · joined req_8f21" | A repeat is the same `request_id`, so it has no row: `req_8f21` says "Delivered again once" in its panel (6.3) |
| 06 | No visa value | A "Visa value" column and field (6.6) |

## 1. What the page is for

### 1.1 Where the agent sits

```
LS Login ──ID token──► Editor (member's browser, biochef-app)
                          │ signs study versions, workflow versions, requests
                          ▼
                     Coordinator (stores and forwards only, reads no payload or result)
                          ▲ agent polls over mutual TLS, outbound only
 TRE ─────────────────────┼──────────────────────────────────────────
 │  data manager ──► agent UI (this spec) ──► Policy Engine (own repo, own UI)
 │                        │ launches steps with no network
 │                        ▼
 │                   runner (apptainer today, Podman where a TRE needs it)
 │                        │ outputs
 │                        ▼
 │   output rules ──► egress: TRE airlock OR manual review here ──► agent encrypts for recipients, signs ──► coordinator
```

### 1.2 What the data manager does on this page, and nothing else

1. Approves, refuses or withdraws **study versions**.
2. Approves, refuses or withdraws **workflow versions** inside an approved study.
3. Watches what the agent did with **requests** (accepted, refused with a reason, joined to an existing run).
4. Follows each **run** (steps, logs, manifest) and, when the node is configured for it, **releases or withholds** outputs.
5. **Binds datasets** named by studies to data on this node, when the node allows bindings from the page.
6. Reads the **node's state**: keys, federation node list, coordinator connection, runner, settings, audit log.

Requests are never approved one by one. The agent checks each request itself (section 6.3).

### 1.3 Out of scope

- The Policy Engine's own rules and UI. It will live in its own repo as a submodule of the agent; neither exists yet. The agent UI only shows its verdict and links to it (`BIOCHEF_POLICY_ENGINE_URL`).
- Anything the coordinator or the editor does.
- User accounts. The agent has none (section 4).
- Editing the agent's config file from the page. Settings are read only on the page.

## 2. Ground rules for the implementation

1. Follow `biochef-infra/CONTRIBUTING.md`: branch from `master` as `feat/agent-ui-<part>`, one PR per part in section 12, squash merge, one approval from someone other than the author. Never push to `master`.
2. Stop after each part and report back to `jorgeMFS` with what changed, how it was tested, and what is next. Do not start the next part on your own.
3. Synthetic data only. Fixtures use invented names and the dataset ids in the mocks. Never real data, never real keys.
4. Every new setting is read with a literal `os.getenv("BIOCHEF_…")` and documented in **both** `README.md` and `example.env`. `tests/test_settings_are_documented.py` must scan every package of the repo, not only `*.py` at the root. A process that does not import `convert.py` has to call `load_dotenv()` itself, because that module is the only one that loads `.env` today.
5. Keep the existing API (`/convert`, `/runs…`, `/ga4gh/wes/v1/…`) and `openapi.json` unchanged unless a part says otherwise. If an interface changes, `openapi.json` and the README change in the same PR.
6. Match the house style of the repo: module docstrings that explain *why*, explicit failures over silent defaults, refuse to start on a half configured setting (see `auth.py` and `audit.py`).
7. Python floor is 3.11 (`.python-version`, `run.sh`, CI matrix 3.11, 3.12, 3.13). Everything must work on 3.11.
8. No network at runtime for the UI. No CDN, no Google Fonts, no external script. Everything the page loads is served by the agent.

## 3. Technology stack

All versions pinned. Add new Python pins to `requirements.txt` and make `pip check` pass.

| Layer | Choice | Version | Notes |
|---|---|---|---|
| Web framework | FastAPI | 0.136.1 (already pinned) | UI routes live in a **separate ASGI app** (section 3.1) |
| Templates | Jinja2 | 3.1.6 (already pinned) | `autoescape=True` for every template |
| Interactivity | htmx | **2.0.11** (npm `latest` tag, 22 Sep 2026) | Vendored file `ui/static/vendor/htmx-2.0.11.min.js` with its 0BSD licence. Do **not** use htmx 4.x yet (major rewrite, published 28 Aug 2026) |
| CSS | Tailwind CSS standalone CLI | **4.3.3** (16 Jul 2026) | Do not take 4.3.4 or 4.3.5 (released 2 and 6 Nov 2026). A `make css` target (and the Dockerfile, once `ieeta-pt/biochef-agent#73` adds one) downloads the binary for the host from the GitHub release, Linux x64 and macOS arm64 at least, checks its SHA-256 against a value committed in the repo, and builds `ui/static/app.css`. Commit the built CSS too, so the agent runs without the CLI |
| Fonts | Roboto 400/500/700 and Roboto Mono 400/500, woff2, latin and latin-ext | from `@fontsource/roboto` 5.3.0 and `@fontsource/roboto-mono` 5.3.0, OFL 1.1 | Vendored under `ui/static/fonts/` with the licence file. Same family as the BioChef app |
| Database | SQLModel on SQLAlchemy | sqlmodel 0.0.37, SQLAlchemy 2.0.49 (already pinned) | SQLite by default (`BIOCHEF_DB_URL=sqlite:////var/lib/biochef/agent.db`), Postgres possible later |
| Migrations | Alembic | 1.20.1 (new pin; needs SQLAlchemy ≥ 2.0, Mako) | From the first table. No `create_all` in production paths |
| Signatures | `cryptography` | 50.0.1 (already pinned) | Ed25519 for the node, ES256 on P-256 to check members (section 7). `cryptography` does not read `age-x25519`, so `pyrage` 1.4.0 does |
| Canonical JSON | `rfc8785` | 0.1.4 (new pin) | Bytes that signatures cover (section 7.2) |
| ID tokens | PyJWT | 2.13.0 (already pinned) | Reuse the JWKS fetching pattern in `passports.py` |
| Tests | pytest 8.4.2, httpx 0.28.1 (already pinned) | | FastAPI `TestClient` for every route. Browser tests are optional and come last (part 7), with `playwright` 1.64.1 and `pytest-playwright` 0.10.0 in a separate `requirements-dev.txt` |

No Node.js, no npm, no bundler in the agent repo. No Alpine.js. If a screen needs more than htmx gives, write a few lines of plain JS in a static file (never inline, because of the CSP).

### 3.1 Process layout

The current app (`main.py`) wraps everything in `AuthenticationMiddleware`, which authenticates **callers of the API** (`BIOCHEF_AUTH=none|bearer|passport`). The UI authenticates **people inside the TRE** in a different way (section 4). `audit.py` also says on purpose that there is no endpoint to read the trail, because "an audit trail reachable over the same API it audits is a thing an attacker reads".

So the UI is a **second ASGI app** in a new package `ui/`, served on its own port and bound to an interface inside the TRE only:

```
fastapi run main.py                       # existing API, unchanged
python -m ui                              # new, started by run-ui.sh
```

Both processes share the same database and the same audit file. Add a `run-ui.sh` next to `run.sh`. The UI never mounts the API routes and the API never serves the UI or the audit trail. `python -m ui` takes the host and port from the same settings the app checks, `.env` included, and keeps uvicorn's proxy headers off, because uvicorn otherwise rewrites the client address from `X-Forwarded-For` for a local proxy, and section 4 decides trust on that address. `uvicorn ui.app:app` still works, with `--no-proxy-headers`.

**The database is the only channel between the two processes.** Runs, logs, slots and retained workspaces live in the memory of the API process (`runs.py`, `main.py`), where a second process cannot reach them. So:

- The API process writes `run_summary`, including `workspace_path`, step times and the log tails, capped at `BIOCHEF_MAX_LOG_BYTES`.
- The UI asks for a cancellation by setting `cancel_requested`; the API process reads it and calls its existing cancel logic.
- The coordinator poll (part 8) runs in the API process, so that an accepted request starts a run there.
- `BIOCHEF_EGRESS_MODE` is what makes an agent a node. A plain agent leaves it unset, needs no database and runs none of this.
- After a restart the API process rebuilds retention from the database.

The agent runs on the same machine as the jobs. `os.killpg`, the local run directory and `BIOCHEF_LOCAL_ROOT` all require it.

### 3.2 Package layout

```
ui/
  __init__.py
  app.py            # FastAPI app with docs, redoc and openapi off, middleware (identity, CSRF, security headers), routers
  settings.py       # reads the UI settings, load_dotenv, refuses to start on a half configured setting
  identity.py       # who is using the page (section 4)
  csrf.py
  routes/           # inbox.py, studies.py, workflows.py, requests.py, runs.py, datasets.py, node.py
  templates/
    base.html       # sidebar layout
    partials/       # htmx fragments
    inbox.html, study.html, workflow.html, requests.html, run.html, datasets.html, node.html
  static/
    app.css         # built by Tailwind, committed
    app.js          # adds the CSRF header to htmx requests; no inline script (section 9)
    vendor/htmx-2.0.11.min.js, LICENSE-htmx
    fonts/…, LICENSE-fonts
    img/biochef-white.svg   # copy of biochef-app/img/BioChefWhite.svg
  tailwind/input.css      # @theme tokens (section 5)
store/
  db.py             # engine, SQLite pragmas
  states.py         # allowed state transitions
  models.py         # SQLModel tables (section 6)
  repo.py           # all reads and writes go through here
  migrations/       # alembic
  seed.py, fixtures/  # section 11
node/
  keys.py           # node signing and encryption keys (section 7)
  decisions.py      # build, sign and record decisions
  review.py         # a decision's state change and signed decision, and undo
  policy.py         # PolicyEngine protocol and a fake (section 6.5)
  identity.py       # member identity check (section 7.3)
  verify.py         # request checks (section 6.3)
  requests.py       # receive a request, check it once, record the verdict
  age.py            # age-x25519 with the node key
  datasets.py       # dataset bindings (section 6.6)
  federation.py     # the signed node list (section 7.4)
  egress.py         # egress pipeline (section 6.5)
  runtime.py        # the API process's side of the database channel (section 3.1)
```

Names can change if the repo's flat layout argues against packages; say so in the part 1 report.

## 4. Who is using the page

- The agent has **no user accounts and no passwords**. The TRE decides who may open the page. How depends on the deployment; a reverse proxy with Keycloak in front of the UI port is one way.
- `BIOCHEF_UI_IDENTITY=header|dev`. No default: refuse to start without it. Also refuse to start without `BIOCHEF_AUDIT_LOG`: unset, `audit.record` writes nothing and reports nothing, and every decision must be in the trail.
  - `header`: the proxy passes the user name in the header named by `BIOCHEF_UI_IDENTITY_HEADER` (for example `X-Forwarded-User`). The UI accepts that header **only** from the addresses in `BIOCHEF_UI_TRUSTED_PROXIES` (comma separated CIDRs). From any other address, or without the header, or with the header repeated, empty or holding control characters, answer 401 and do not render any page.
  - `dev`: a fixed user from `BIOCHEF_UI_DEV_USER`. Refuse to start in `dev` unless `BIOCHEF_UI_HOST` is `127.0.0.1` or `::1`. Also check the client address of each request, because a `--host` given on the command line bypasses the variable. Show a red "development identity" bar on every page.
- There are no roles in the agent. Whoever the TRE lets in is a data manager. **Every** decision records that user name in the audit trail and in the decision object.
- The sidebar footer shows "Signed in through the TRE · <user>".

## 5. Look and feel

The page must look like the BioChef app (`biochef-app/src/index.js` theme and `src/components/Navbar.js`). Use these tokens in `ui/tailwind/input.css` under `@theme`:

| Token | Value | Use |
|---|---|---|
| `--color-brand` | `#009688` | BioChef teal (MUI primary). Selected borders, graph highlights |
| `--color-brand-strong` | `#00796B` | Sidebar background, primary buttons, links. Used instead of `#009688` wherever text is white or the colour is text, because white on `#009688` is 3.67:1 |
| `--color-brand-deep` | `#00574B` | Link hover, active tab text, active sidebar item |
| `--color-brand-hover` | `#00695C` | Sidebar item on hover |
| `--color-brand-soft` | `#E0F2F1` | Selected rows, info pills |
| `--color-bg` | `#F5F5F5` | Page background (MUI `background.default`) |
| `--color-surface` | `#FFFFFF` | Cards |
| `--color-line` / `--color-line-soft` | `#E0E0E0` (cards) / `#EEEEEE` (rows) | Borders |
| `--color-head` | `#FAFAFA` | Table headers |
| `--color-text` | `#14181F` | Body text |
| `--color-muted` / `--color-label` | `#5B6472` / `#4A5260` | Secondary text / labels |
| `--color-ok` / `--color-ok-soft` | `#1E6B3A` / `#E7F3EB` | Approved, passed checks |
| `--color-warn` / `--color-warn-soft` | `#BF360C` / `#FBE9E7` | Waiting, re-approval needed, held back. Deep orange, the app's secondary family |
| `--color-bad` / `--color-bad-soft` | `#B3261E` / `#FCE8E6` | Refused, failed check |
| `--color-mute-soft` | `#EEEEEE` | Withdrawn, unchanged |
| `--font-sans` | `Roboto, Helvetica, Arial, sans-serif` | |
| `--font-mono` | `"Roboto Mono", ui-monospace, monospace` | Ids, hashes, paths, logs |
| radius | 8px cards, 6px buttons and inputs, 4px pills | |

Layout rules taken from the mock:

- Vertical sidebar on the left, 232px, background `brand-strong`, white logo (96px wide) and a pill "Agent · <node name>" (`BIOCHEF_NODE_NAME`). Items: Inbox (count of waiting versions), Studies, Requests (count of refused in the last 24 h), Datasets, Node. The active item has a `brand-deep` background and a 3px white left border. Footer: coordinator status dot and last poll, link "Open Policy Engine ↗", signed in user.
- Content area padding 32px, page title 26px weight 500, intro text 14px muted, max width 760px for intro text.
- Section labels: 12px, weight 500, uppercase, letter spacing 0.06em, colour `#4A5260`.
- Every control is a real `<button>`, `<a href>`, `<input>` with `<label>`. Minimum target 44px high. Text contrast at least 4.5:1. No emoji anywhere.
- Below 900px the sidebar stacks above the content and wide tables scroll inside their own box.
- No dark mode for now.

## 6. Domain model and rules

### 6.1 Tables

Field names follow `ieeta-pt/biochef-coordinator#6` wherever a field comes from a coordinator document. Keep every signed document **byte for byte** as received (`raw` column) next to the parsed fields; the parsed fields are for display and queries, the raw bytes are what signatures cover.

| Table | Key fields |
|---|---|
| `study` | `id` (`study_id` from the document), `title` (empty until the document carries one, section 13), `created_at` |
| `study_version` | `id`, `study_id`, `version` (int), `digest`, `previous_digest`, `raw`, `goal`, `proposer`, `members` (json), `nodes` (json, `StudyNode`), `datasets` (json, this node's dataset ids), `output_rules` (json: `aggregates_only`, `min_count`, `types`, `max_bytes`), `recipients_mode`, `received_at`, `state` (6.2), `state_changed_at` |
| `member_key` | `study_version_id`, `member`, `ls_aai_id`, `signing_key` (P-256 JWK as json), `result_key` (age public key), `binding_token`, `identity_verified` (bool), `identity_error`, `display_name` (the `name` claim of the verified token, section 7.3) |
| `workflow_version` | `id`, `study_id`, `study_version_id`, `digest`, `raw`, `name`, `version` and `workflowhub_id` (null unless a library workflow), `steps` (json: id, `repo`, digest, signature status, `paramValues`), `received_at`, `state`, `state_changed_at` |
| `decision` | `id`, `subject_type` (`study_version`, `workflow_version`, `request`, `run`, `egress`, `dataset_binding`), `subject_id`, `subject_hash`, `action` (`approve`, `refuse`, `withdraw`, `release`, `withhold`, `bind`, `unbind`, `cancel`), `user`, `comment`, `dac_confirmed`, `reason`, `at`, `object_raw`, `signature`, `posted_to_coordinator_at` |
| `request` | `request_id`, `study_id`, `study_version` (int, as named, because it may not exist here), `study_digest`, `workflow_digest`, `member`, `node`, `datasets` (json), `not_after`, `payload_digest`, `raw`, `received_at`, `verdict` (`accepted`, `refused`; null until checked), `failed_check` (6.3 code), `run_id`, `repeats`, `last_repeat_at` (the same `request_id` delivered again, 6.3) |
| `run_summary` | `run_id`, `request_id`, `state` (existing WES states), `steps` (json: id, state, `started_at`, `ended_at`), `started_at`, `ended_at`, `exit_code`, `workspace_path`, `manifest_path`, `log_stdout`, `log_stderr`, `node_logs` (json), `cancel_requested`, `egress_state` (6.5), `egress_files` (json: file, size, verdict, reason), `airlock_ticket`, `airlock_at`, `retain_until` |
| `dataset_binding` | `dataset_id`, `source` (`config` or `page`), `type` (`localpath` today), `location` (relative to `BIOCHEF_LOCAL_ROOT`), `visa_value` (6.6), `bound_by`, `bound_at` |
| `outgoing_message` | `id`, `run_id`, `kind` (`withheld_result` today), `document` (the signed document, canonical bytes), `created_at`, `sent_at`. One of each kind per run (6.5) |
| `federation_list` | `version`, `raw`, `signature`, `nodes` (json), `received_at`. An older version than the newest stored is refused (7.4) |
| `node_status` | One row: `last_poll_at`, `last_success_at`, `last_error`. Written by the poll, read by the sidebar and the Node page |

Tool images do not exist per step: the agent runs one image (`BIOCHEF_CONTAINER_IMAGE`) and fetches each tool as a bundle, `repo@digest`.

The in memory `RunStore` stays, and `run_summary` is written from its state transitions. A run must never depend on the UI being up.

### 6.2 Study and workflow version states

```
study_version:    pending ──approve──► approved ──withdraw──► withdrawn
                     │                    │
                     └──refuse──► refused └──(newer version approved)──► superseded
workflow_version: pending ──approve──► approved ──withdraw──► withdrawn
                     │                    │
                     └──refuse──► refused └──(study version superseded)──► superseded
```

Rules:

1. **Only one approved version of a study at a time.** Approving v3 moves the approved v2 to `superseded` in the **same transaction**, and writes two decisions: `approve v3` and `withdraw v2` (reason `superseded`), both signed and both posted. Both name the user who approved v3. A database constraint enforces the rule. Runs, logs and decisions of v2 stay readable in the study's history. A version moved this way is shown as "Superseded".
2. While v3 is `pending`, v2 stays `approved` and keeps serving requests. If v3 is refused, v2 is untouched.
3. A study version can be approved only if **every** member has `identity_verified = true` (section 7.3). Otherwise the Approve button is disabled and the page says which member failed and why (mock: "Rare variants pilot v1").
4. Before approving a study version the data manager ticks "The data access committee has granted access for this study". Store it in the decision (`dac_confirmed: true`). The tick stays even with the visa check: visas arrive only inside each request, so at approval there is nothing to check.
5. Workflow versions are approved **per study version**. Different parameter values give a different hash and so a different workflow version (mock: two variants of `gwas-qc 1.3.2`). Several versions of the same workflow can be approved at once in one study version.
6. Withdrawing a workflow version is a signed decision of its own and does not need a new study version.
7. When v3 of a study is approved, the workflow approvals of v2 do not carry over: each must be approved again, and the workflows of the superseded version move to `superseded` too.

### 6.3 Request checks

A request is checked in this exact order. The first failure stops the checks, refuses the request and records its code. The Requests screen lists all ten and marks later ones "not reached".

| # | Check | Refusal code |
|---|---|---|
| 1 | ES256 signature by a member of the named study version, with the `signing_key` listed for that member. A request naming a version this node does not hold fails here | `bad_signature` |
| 2 | `node` is this node | `wrong_node` |
| 3 | The named study version is `approved` on this node and its digest equals `study_digest` | `study_not_approved` |
| 4 | `workflow_digest` is `approved` for that study version | `workflow_not_approved` |
| 5 | Not expired (`not_after` later than now) | `expired` |
| 6 | Not seen before, by `request_id`, which the member signs. The same signed request delivered again **joins** the run if it was accepted, and gets the **same refusal** if it was refused, even when the reason has since gone. Its row keeps the first verdict and counts the repeat. A new `request_id` is a new request, even with the same payload | `replay` when the same `request_id` arrives on a different signed request |
| 7 | Payload decrypts with this node's encryption key, and its digest equals `payload_digest` | `cannot_decrypt` |
| 8 | Workflow digest recomputed from the decrypted tools and parameter values equals `workflow_digest` | `hash_mismatch` |
| 9 | The member's visas allow the named datasets (reuse `passports.py` visa checking): for each dataset, a visa of the required type whose value is exactly the binding's `visa_value` (6.6). A binding with no `visa_value` matches no visa | `visa_missing` |
| 10 | Every named dataset has a binding on this node | `dataset_not_bound` |

Write this as a pure function `verify(request, store, keys, now) -> Verdict` with one test per row, plus tests for repeats. Every verdict writes an audit event. A refusal is also a signed decision posted to the coordinator, so the requester sees why.

Repeats, in detail:

- Checks 1 to 5 run on every delivery, so a repeat after `not_after` is refused as `expired`, and `not_after` is how long the node holds a `request_id` to check 6. A refused request is sent again under a new `request_id`.
- Only the same signed `job_request` is a repeat. The rows are not changed by anything else delivered under the id: a `replay` is refused with a signed decision, since the member signed it, and a delivery that fails checks 1 to 5 is only audited.
- A repeat writes the audit event of check 6 and no new decision.

**Blocked on the agent API**: `ieeta-pt/biochef-coordinator#6` fixes the shape of a request, a study version and the encrypted payload, but not the agent side. Until the agent API is in `biochef-coordinator`, build and test against fixtures in `tests/fixtures/federation/` that follow those models. The payload holds the member's visas and a WES request.

### 6.4 Run states

Keep the eight WES states the agent already has (`QUEUED`, `INITIALIZING`, `RUNNING`, `COMPLETE`, `EXECUTOR_ERROR`, `SYSTEM_ERROR`, `CANCELING`, `CANCELED`) and the four step states (`PENDING`, `RUNNING`, `COMPLETE`, `FAILED`). Do not add egress states to the WES state; egress is a separate field (6.5). Do not hard code a retry limit: it depends on the job and the TRE and is set later. `BIOCHEF_RUN_TIMEOUT` defaults to 900 s, which ends any longer run, so a node sets it to fit its jobs.

### 6.5 Egress

`BIOCHEF_EGRESS_MODE=airlock|manual`. No default: refuse to start without it.

```
COMPLETE run
   │
   ▼
1 output rules (Policy Engine verdict per file: pass or held back, with reason;
                 until it is connected, the agent's own check, see below)
   │
   ├── airlock mode: 2a hand the passing files to the TRE airlock, poll its decision
   │                 egress_state: awaiting_airlock → released | withheld
   │
   └── manual mode:  2b wait for the data manager: Release (passing files) or Withhold
                     egress_state: awaiting_review → released | withheld (signed decision)
   ▼
3 released: encrypt with `age-x25519` for the study's recipients, sign the ciphertext with the node key, bind it to the request
   ▼
4 post to the coordinator    egress_state: sent

withheld: at the moment of withholding, a signed notice with no files is kept in
          `outgoing_message`; part 8 sends it and sets `sent_at`
```

- `egress_state` values: `not_ready`, `rules_pending`, `awaiting_airlock`, `awaiting_review`, `released`, `withheld`, `sent`.
- Files held back by the output rules never leave, in either mode.
- **Result document**: follow `Partial` and its in-toto `Statement` in `ieeta-pt/biochef-coordinator#6`. Keep `recipients_mode`, but implement only `requester` for now: #6 has only the submitter, and `all_members` is being raised there.
- **Retention changes**: today `retention.py` starts the `BIOCHEF_KEEP_OUTPUTS` clock (default 3600 s) when the run ends, and `BIOCHEF_MAX_RETAINED_RUNS` evicts by age. An airlock review can take longer, so outputs would be deleted before egress. Change it so the clock starts when `egress_state` reaches `sent` or `withheld`. A run waiting for egress counts as active: it is never evicted, and when every slot is active new runs get 503, as the store already does. Retained workspaces are in memory, so after a restart the API process rebuilds them from `run_summary.workspace_path`.
- **Withheld results**: the requester is told. The `Partial` with `decision: withheld`, an empty `subject` and no ciphertext is built and signed when the result is withheld, by a person or by the airlock, and stored as an `outgoing_message`, so what goes out is what was decided then. Undo of a withholding deletes the notice while it is unsent, and is refused once it has gone. `egress_state` stays `withheld`.
- The airlock interface is TRE specific. Define a small `Airlock` protocol (`submit(files, unverified) -> ticket`, `status(ticket) -> pending|released|withheld`) with a `FakeAirlock` for tests. **Blocked** until the TRE's interface is known.
- The Policy Engine is called through a `PolicyEngine` protocol with a fake for tests, until the submodule exists: `check(run, output_rules) -> [FileVerdict]` per output file, and `check_rules(output_rules) -> RulesVerdict` for the study level verdict in the Inbox.
- **Until the Policy Engine is connected**, the agent checks what a file's name and size tell: `types` (the last suffix) and `max_bytes`. `min_count` and `aggregates_only` need the contents, so on every passing file of a study that sets them they are marked "not verified" (`FileVerdict.unverified`). In manual mode a data manager may still release, with an explicit confirmation; the signed decision says which rules were not verified, and so does the audit event. In airlock mode the run goes to the airlock with the same marking (`unverified`). The Inbox shows "The agent's own check" with what it leaves open.

### 6.6 Dataset bindings

- `BIOCHEF_DATASET_BINDINGS_FILE`: YAML file with `dataset_id → {type: localpath, location: <relative path>, visa_value: <value>}`. Loaded at start, shown on the page as "config file, locked". PyYAML is already pinned.
- **`visa_value`** is the exact value the data access committee issues in a visa for the dataset, usually a URL. The dataset id is the node's own name for the data and visas do not carry it. Check 9 compares with `passports.satisfies`, exactly. A binding without one refuses every request for the dataset as `visa_missing` where a visa is required, and the Datasets page and the request's panel say why. It is signed with the location in every `bind` and `unbind` decision (`subject_hash` covers both).
- `BIOCHEF_DATASET_BINDINGS_FROM_UI=true|false`. When `true`, the page can add and remove bindings (signed `bind` / `unbind` decisions, audit event). A binding from the page can never override one from the config file; the config wins and the page shows the conflict.
- Every `location` is resolved under `BIOCHEF_LOCAL_ROOT` with the same rule `datasource.LocalPathSource` applies (refuse anything that resolves outside). Do not write a second resolver: move the check out of `LocalPathSource.fetch` into one function both call. `fetch` takes files only, and a binding names a folder, so part 5 also defines how a bound folder reaches a run.
- Bindings need `localpath` in `BIOCHEF_DATA_SOURCES` and `BIOCHEF_LOCAL_ROOT` set. Refuse to start with a bindings file or `FROM_UI=true` otherwise.
- A study version that names an unbound dataset can still be approved; its requests are refused with `dataset_not_bound` until it is bound. The Inbox shows "Not bound to local data yet · Bind it".

### 6.7 What `ieeta-pt/biochef-coordinator#6` decides

That PR is the contract, and this document follows it. Where it has no field for something on the mocks:

| Mock shows | #6 has | Do this |
|---|---|---|
| Study name | `goal` | Show `study.title` if set, otherwise `study_id` and the first line of `goal` |
| Member name | `member`, `ls_aai_id` | Show `display_name` if set, otherwise `ls_aai_id` shortened. Never `member` alone, which is an opaque id such as `m-02` |
| Workflow name and version | Only on `LibraryWorkflow` | Show the short digest otherwise |
| Visa required per dataset | Nothing | Show the node's `BIOCHEF_PASSPORT_REQUIRE_VISA` |

## 7. Keys and signatures

### 7.1 Node keys

- Two keys per node: **Ed25519 signing key** (signs every decision and every result) and **X25519 encryption key** (request payloads are encrypted for it with `age-x25519`).
- Paths from `BIOCHEF_NODE_SIGNING_KEY` and `BIOCHEF_NODE_ENCRYPTION_KEY`, PEM, file mode 0600, **outside** any container image and outside the repo. Refuse to start if the file is missing, unreadable, or readable by group or others.
- A CLI to create them: `python -m node.keys init --out <dir>`. It prints the two public key fingerprints for registration with the federation. Never print private keys. Never commit keys (CONTRIBUTING rule).
- Fingerprint format on the page: `ed25519:` or `x25519:` plus the first 4 and last 4 hex chars of the SHA-256 of the raw public key, full value on "Show fingerprints".

### 7.2 Signed decisions

- Canonical form: JSON Canonicalization Scheme (RFC 8785), with the `rfc8785` package 0.1.4 (Trail of Bits, pure Python, no runtime dependencies; new pin). Use no floats in signed objects.
- Decision object fields: `schema: "biochef.node-decision.v1"`, `node`, `subject_type`, `subject_id`, `subject_hash`, `action`, `user`, `comment`, `dac_confirmed` (study approvals), `at` (UTC, seconds), `reason` (for automatic withdrawals and refusals).
- Signature: Ed25519 over the canonical bytes, base64url. Store object and signature; post both.
- `ieeta-pt/biochef-coordinator#6` defines no decision format yet. **Must be aligned with the agent API** before part 8. Treat `v1` as provisional.

### 7.3 Member identity check

For each member listed in a study version the agent checks `binding_token`, the LS Login ID token that came with the member's keys:

1. Signed by LS Login (issuer `BIOCHEF_LS_LOGIN_ISSUER`, keys from its JWKS; reuse the fetching and retry pattern of `passports.py`).
2. Audience is the editor's LS Login client (`BIOCHEF_EDITOR_CLIENT_ID`).
3. The token names the member.
4. The token's `nonce` equals the hash of the member's two public keys (`signing_key` and `result_key`; exact construction from the editor; **blocked** on biochef-app). This check stays closed: until the construction is defined, a member whose token passes every other check still counts as not verified, with that reason in `identity_error`, and no name is stored.
5. Expired tokens are accepted only if signed with a past LS Login key in the federation's signed key list. Until that list exists, accept only unexpired tokens and say so on the page.

**Member names.** A name comes only from the `name` claim of `binding_token`, read after the checks above pass, and is stored as `member_key.display_name`. The token's `sub` must equal the member's `ls_aai_id`. If the claim is missing, show the shortened `ls_aai_id`. A failed check leaves no name. No other source is accepted, including anything in the study document.

The page shows only "✓ identity verified" or "✗ identity not verified" per member, with keys behind "Show keys". Keys stay reachable because a data manager needs to see which key was there when an identity check fails.

### 7.4 Federation node list

`BIOCHEF_FEDERATION_KEY` (public key that signs the node list) and the current list from the coordinator. Refuse older list versions than the one stored. Node page shows "Up to date · N nodes".

## 8. Screens

Each screen below lists its route, what it reads, what it can do, and its states. htmx fragments are `GET` or `POST` routes under `/ui/_/…` returning partial templates; full pages are under `/ui/…`. `/` redirects to `/ui/inbox`.

### 8.1 Inbox (`/ui/inbox`, mock 01)

- Left: cards for every `pending` study version and workflow version, newest first. Card shows kind label, received time, name and version, one line summary of what changed, and a pill: "Re-approval needed" (new version of an approved study), "New" (new study), "New workflow version", or "Check failed · cannot be approved".
- Clicking a study card loads its detail on the right (`hx-get="/ui/_/inbox/study/{id}"`, `hx-target="#detail"`, `hx-push-url`). A workflow card goes to the Workflow screen.
- Study detail: compared with the approved version (link), "Proposed by <proposer>" pill (no tick: nothing verifies the signature yet, section 13), Members (name, role, "+" for new, identity status, "Show keys" toggle), Datasets on this node (binding status, required visa), Nodes (all on the signed node list), Result recipients, Output rules (diff against approved version in mono, red removed, green added, plus the Policy Engine verdict line with link), the DAC tick, optional comment, buttons **Approve and sign** and **Refuse and sign**, and the line "Approving v3 supersedes v2. Only one version of a study is valid at a time."
- After a decision: the detail is replaced by a status line ("v3 approved and signed. v2 was superseded at the same time; its runs and logs stay in the study history.") with **Undo before the next poll**, which is allowed only while the decision has not been posted (`posted_to_coordinator_at` is null). Undo deletes the unsent decision and writes an audit event.
- Failed identity: red alert naming the member, Approve disabled (`disabled` attribute, not hidden), Refuse available.
- Empty state: "Nothing is waiting for this node."

### 8.2 Studies (`/ui/studies`, `/ui/studies/{study_id}`, mock 02)

- Left list: approved studies, then superseded and withdrawn ones. Each card: name and version, state pill, the approved workflows and any that wait ("3 workflows approved · 1 waiting", nothing about waiting when none does, so the card agrees with the Workflows tab) and the requests, a "v3 in Inbox" pill when a newer version waits.
- Right: header with version, "Approved <date> by <user>", **Withdraw approval** (confirmation step in the page, never a browser `confirm()`).
- Tabs (htmx, `role="tablist"`): **Overview** (members with identity status, dataset and visa, recipients, output rules with Policy Engine link, versions table with state, date and who decided, including "automatic, when v3 was approved by <user>"), **Workflows** (table: workflow, parameter values, hash short, state, request count, action Review or Withdraw), **Requests** (every request of this study, newest first, 20 per page with "Show N older").

### 8.3 Workflow version (`/ui/workflows/{id}`, mock 03)

- Breadcrumb back to the study. Header: name and version, "hash over tools and parameter values" with short hash, and the state. No signature pill: `ieeta-pt/biochef-coordinator#6` has no signer for a workflow.
- **Steps graph**: dataset node, one box per step, arrows, then an "Output rules, then egress" box. Boxes are buttons; selecting one shows the step description if the step carries one (#6 has no such field, so today only the fixtures do), `repo@digest`, signature status from the hub (`signing.py`), parameter values. Render the graph as plain HTML boxes in a horizontal scroll box; it does not need to be draggable. Colour step boxes by run step state when shown inside a run (8.5).
- **What changed**: diff against the approved version of the same workflow (tool digests, parameter values).
- Link "See recipe.json and the generated Snakefile" opens both read only: the workflow JSON as received, and the Snakefile from `convert.py`, without running anything. `convert.py` logs in on first use and `parse_biochef_workflow` takes a `resolve` function, so the page reads tools from the local cache and never reaches the registry. Nothing in the cache proves it is the bundle a step pins, so the page says so above the Snakefile.
- Approve and sign, Refuse and sign, with the line "1.3.2 stays approved. Requests name the hash they want, so each runs only the version it asked for."

### 8.4 Requests (`/ui/requests`, mock 04)

- Title "Latest requests". Last 24 hours across all studies; the full list per study is on the study page, and the sentence that says so links to Studies.
- Filter tabs All / Accepted / Refused with counts.
- Table: request id (button), study, workflow, member, received, verdict pill ("Refused · workflow not approved", "Accepted · run COMPLETE", "Accepted · result sent").
- Side panel: the ten checks for the selected request with ✓, ✗ or "not reached", expiry, "Delivered again N times, last <time>" with what the repeats got (6.3), for `visa_missing` each named dataset whose binding has no visa value, link "Open the run" when accepted, and for refusals "The refusal is signed with the node key and sent to the coordinator, so the requester sees why."

### 8.5 Run (`/ui/runs/{run_id}`, mock 05)

- Breadcrumb from Requests. Header: "Run of <request>", study, workflow, member, start and end, WES state pill, egress pill.
- Steps strip coloured by step state (green complete, teal running, red failed, grey pending), with duration, and an arrow from each step to the next (none before the first, none for a single step).
- Tabs:
  - **Egress**: the four stage cards of section 6.5 for the node's mode, file table (file, size, output rule verdict; held back rows in warn colours; "not verified" rules on passing files), Release / Withhold buttons in manual mode only, with the confirmation of 6.5 when rules were not verified, the state of a withheld result's notice, retention line.
  - **Logs**: the run wide log and one tab per step. The run wide log is live while the run goes (read `run_summary.log_stdout` and `log_stderr` every 2 s with `hx-trigger="every 2s"`, only while the run is active). Per step logs (`node_logs`) exist only after the run ends; say so in the tab. Download button. Logs never leave the node.
  - **Manifest**: workflow and tools with digests, inputs, outputs, runner and exit code, build evidence reference, "Download run.json" (the file at `run_summary.manifest_path`, while the workspace is retained).
- **Cancel** button while the run is `QUEUED`, `INITIALIZING` or `RUNNING`: sets `cancel_requested`, recorded as a decision with the user name (`subject_type = run`, `action = cancel`). The API process does the cancelling (3.1).

### 8.6 Datasets (`/ui/datasets`, mock 06)

- Table: dataset id, source type, location under the data root, visa value ("Not set" in warn colours with the reason when the node requires a visa), studies using it, bound by ("config file, locked" or "this page · <user>" with Edit and Remove). The bind and edit forms have a visa value field. Edit is offered only where a pending or approved study names the dataset, because the page refuses to bind or rebind any other; Remove is always offered for a page binding and asks first, in the page and never with `confirm()`, as Withdraw does: "Remove the binding of <dataset>? Requests that need it are refused until it is bound again." and **Remove and sign**.
- "Waiting for a binding" card for every dataset named by a pending or approved study version with no binding: prefilled id, type select, folder input, **Bind and sign**, the note "Paths are resolved under the data root on the Node page. The agent refuses anything outside it." Hidden when `BIOCHEF_DATASET_BINDINGS_FROM_UI=false`, replaced by "Bindings on this node come only from the config file."

### 8.7 Node (`/ui/node`, mock 07)

- Four cards: Node keys ("Ready", "Show fingerprints"), Federation ("Up to date · N nodes"), Coordinator ("Online", last poll; "Offline since …" in bad colours when the last successful poll is older than 3 × the poll interval), Runner (name, busy slots of `BIOCHEF_MAX_CONCURRENT_RUNS` counted from `run_summary`, and "Network isolation: off" in warn colours for `subprocess` and for `apptainer` until part 9b).
- Settings, read only: egress mode (and the other option), dataset bindings mode, data root, retention, tool signature mode (`BIOCHEF_SIGNING_MODE`).
- TES API, read only, from `tes_listener` and `tes_task` (14.4): "Running" with the pid, the host and since when, and when it last renewed, or "Not running" with who held the database last; the count of tasks in each state, in the order of a task's life; the newest ten tasks with time, short id, owner, image (by its short digest) and state. It reads only what a list needs, never a command or a log, offers nothing to press, and the UI does not import the listener. A place that has not been renewed for the lease (30 seconds, one number shared with the listener) is not running.
- Audit log: newest 50 events from the JSONL trail (time, event, subject, user, whether signed), every kind of event in words (an event with no line of its own is put into words from its name, so a new kind is readable before its line is written), a study or workflow version by its title and not its number (the number only where the row it names is gone), an undo naming the subject of the decision it took back, a runner event naming the runner and a TES event the task. "Export (JSON lines)" downloads the file. Read the file with `audit.read`; never add an audit endpoint to the API app.
- Audit check: `decisions.decide` writes the audit event before the caller commits, so a failed commit leaves a `node.decision` event for a decision that does not exist. That order stays, because the opposite one loses events. The Node screen crosses the trail with the `decision` table and lists the events with no decision (time, subject, action, user, `decision_id`) in warn colours; with none it says so. The list is read only and nothing repairs the trail. An event counts as matched only when a row has the same `decision_id`, `subject_type`, `subject_id`, `action` and `user`, because SQLite can give the id of a rolled back row to a later decision, so the id alone would hide the event. An event followed by a `node.decision.undone` event for the same `decision_id` is accounted for: its decision was deleted on purpose (8.1).

## 9. Security

- Headers on every UI response:
  `Content-Security-Policy: default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self'; font-src 'self'; connect-src 'self'; form-action 'self'; frame-ancestors 'none'; base-uri 'none'`
  plus `X-Content-Type-Options: nosniff`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` on HTML.
- No inline `<script>` and no inline `style=""` attributes in templates (CSP). htmx config goes in a `<meta name="htmx-config">` tag; set `allowEval` to false and `includeIndicatorStyles` to false. FastAPI's `/docs` and `/redoc` load from a CDN, so both and `openapi.json` are off in the UI app.
- CSRF: every `POST` carries a token. Use a per session token in a cookie (`HttpOnly`, `SameSite=Strict`, `Secure` in `header` mode) and the same token in a `<meta>` tag. `ui/static/app.js` adds it as a header to htmx requests, because `hx-headers` cannot read a `<meta>` without eval; plain forms send it in a hidden field. Reject mismatches with 403.
- All state changes are `POST`. `GET` never changes state.
- Jinja2 autoescape on; never mark data from documents, logs or members as safe. Log text is rendered as text in a `<pre>`.
- Rate of refreshes: live log polling only while the run is active and only on the visible tab. With eval off, `hx-trigger` filters do not run, so `app.js` handles the visible tab.

## 10. Settings added by this work

All read with `os.getenv`, all in `README.md` and `example.env` (test enforced). A setting is read and required only from the part that uses it.

| Variable | Default | From part | Meaning |
|---|---|---|---|
| `BIOCHEF_DB_URL` | `sqlite:////var/lib/biochef/agent.db` | 1 | Database |
| `BIOCHEF_NODE_SIGNING_KEY` | none, required | 2 | Ed25519 private key path |
| `BIOCHEF_NODE_ENCRYPTION_KEY` | none, required | 2 | X25519 private key path |
| `BIOCHEF_UI_HOST` | `127.0.0.1` | 3 | Interface the UI binds to |
| `BIOCHEF_UI_PORT` | `8081` | 3 | UI port |
| `BIOCHEF_UI_IDENTITY` | none, required | 3 | `header` or `dev` |
| `BIOCHEF_UI_IDENTITY_HEADER` | none, required with `header` | 3 | Header carrying the user name |
| `BIOCHEF_UI_TRUSTED_PROXIES` | none, required with `header` | 3 | CIDRs allowed to set that header |
| `BIOCHEF_UI_DEV_USER` | none, required with `dev` | 3 | Fixed user for development |
| `BIOCHEF_NODE_NAME` | none, required | 3 | Shown in the sidebar and in decisions |
| `BIOCHEF_POLICY_ENGINE_URL` | none | 3 | Where "Open Policy Engine" points |
| `BIOCHEF_LS_LOGIN_ISSUER` | none, required | 4b | LS Login issuer URL |
| `BIOCHEF_EDITOR_CLIENT_ID` | none, required | 4b | Editor's LS Login client id |
| `BIOCHEF_DATASET_BINDINGS_FILE` | none | 5 | YAML bindings from config |
| `BIOCHEF_DATASET_BINDINGS_FROM_UI` | `false` | 5 | Allow bindings from the page |
| `BIOCHEF_EGRESS_MODE` | none, required by the UI | 7 | `airlock` or `manual` |
| `BIOCHEF_FEDERATION_KEY` | none, required | 7.4 | Public key that signs the node list |
| `BIOCHEF_PODMAN_SELINUX` | none, required with `BIOCHEF_RUNNER=podman` | 9 | `disable`: steps run without SELinux separation and the workspace is mounted as it is. `keep`: podman's labels stay on, so on an enforcing host `BIOCHEF_RUN_ROOT` must carry a label containers may use (`container_file_t`). No default, because it depends on the host |
| `BIOCHEF_COORDINATOR_URL` | none, required | 8 | Coordinator base URL |
| `BIOCHEF_COORDINATOR_POLL_SECONDS` | `30` | 8 | Poll interval |
| `BIOCHEF_COORDINATOR_CLIENT_CERT`, `BIOCHEF_COORDINATOR_CLIENT_KEY`, `BIOCHEF_COORDINATOR_CA` | none, required | 8 | mTLS material |

`BIOCHEF_AUDIT_LOG` already exists. It stays optional for the API process and is required by the UI (section 4).

## 11. Fixtures and development mode

- `python -m store.seed --fixture mock` loads the data of the mocks, as corrected in section 0.1: CARDIO-PT cohort v2 approved and v3 pending, v1 superseded, Rare variants pilot v1 with a failed identity, Sepsis biomarkers v1, Microbiome pilot v1 withdrawn, workflows `gwas-qc` 1.3.2 in two parameter variants, 1.4.0 pending, `cohort-summary` 0.2.0, the eight named requests (the six of mock 04 plus `req_7b88` and `req_7a10` of mock 02) and enough generated ones to reach the mock's counts of 14 and 6, run `req_8f21` with its three output files, three dataset bindings. All names and ids are invented.
- Seeded times are relative to the moment of seeding, so the 24 hour views stay filled. The seed refuses a database that already holds data.
- The seed has one run for each way a node can review egress: `run_8f21` waits for the airlock and `run_7c04` (of `req_7c04`, 30 hours old, so that mock 04 keeps its five rows) waits for the agent's own review, so Release and Withhold are reachable with `BIOCHEF_EGRESS_MODE=manual` without editing the database.
- `seed()` writes nothing to the audit trail. `python -m store.seed` does, when `BIOCHEF_AUDIT_LOG` is set: each seeded decision's event, the checks of the named requests and the run states, each at the time of what it records and oldest first, so the Node screen of a demo has a trail and its audit check has nothing to flag.
- Until part 2 exists, seeded decisions carry the signature `unsigned-fixture`. From part 2 the seed signs them with a key generated on the spot.
- Test keys are generated at test time; never commit a private key, even a test one.
- With `BIOCHEF_UI_IDENTITY=dev`, a seeded database and fake Policy Engine and Airlock, every screen of the mocks must be reproducible locally with one command (`./run-ui.sh --dev`).

## 12. Work plan

Each part is one PR. Do them in this order unless `jorgeMFS` says otherwise. Parts marked **parallel** can start at any time.

| # | Part | Depends on | Done when |
|---|---|---|---|
| 1 | **Store**: Alembic setup, tables of 6.1, `store/repo.py`, seed command | nothing | Migrations run up and down on SQLite; repo tests cover each table; the one approved version rule holds in the database; seed loads the mock data |
| 2 | **Node keys and decisions**: `node/keys.py` with `init` CLI, JCS canonical form, `node/decisions.py` (build, sign, store, audit) | 1 | Signing and verification round trip tested; refuses world readable keys; every decision writes one audit event with the user |
| 3 | **UI shell** (**parallel**): `ui/app.py`, identity (header and dev), CSRF, security headers, base template with sidebar, Tailwind build with pinned CLI and checksum, vendored htmx and fonts, empty pages. The sidebar counts stay empty until part 4 | nothing | Each page returns 200 for a trusted user and 401 otherwise, and `/` redirects; refuses to start without `BIOCHEF_AUDIT_LOG`; CSP header present; no external request when loading any page |
| 4 | **Inbox, Studies, Workflow version** with seeded data: approve, refuse, withdraw, undo, one valid version rule, identity check from fixtures, DAC tick | 1, 2, 3 | Every state of mock screens 01 to 03 reachable; rule 6.2.1 tested (approve v3 supersedes v2 in one transaction); Approve disabled on failed identity |
| 4b | **Member identity**: `node/identity.py`, the ID token checks of 7.3 (signature through LS Login's JWKS, audience, expiry, `sub`), with the nonce check closed | 1, 4 | One test per check; a token that passes everything still counts as not verified, with the reason recorded and no name stored; a failed check leaves no name |
| 5 | **Datasets**: config file loading, page bindings, resolution through `LocalPathSource` | 1, 2, 3 | Outside root refused; config wins over page; switch `FROM_UI=false` hides the form |
| 6 | **Request checks** (`node/verify.py`) and Requests screen | 1, 2, 5, fixtures of the request format | One test per check row of 6.3 and for the join; screen 04 matches section 0.1 |
| 7 | **Run screen and egress**: `run_summary` written from run state changes, with step times and log tails, `cancel_requested` read by the API process, logs and manifest tabs, egress pipeline both modes with fakes, cancel, retention change | 4, 6 | Retention starts at `sent` or `withheld`, and survives a restart of the API process (tested); a run waiting for egress is never evicted (tested); manual Release and Withhold are signed; airlock mode shows the airlock state; optional Playwright test of the approve and release flow |
| 7b | **Node screen**: the cards, settings, audit log and audit check of 8.7 | 1, 2, 3 | Screen 07 matches section 0.1; audit export works; the API app gains no audit route; the audit check is tested for a failed commit, an undone decision and an id reused after a rollback |
| 7.4 | **Federation node list** (7.4): check the list's signature with `BIOCHEF_FEDERATION_KEY`, refuse older versions | 1, 7b | Bad signature and older version refused; the Node card shows "Up to date · N nodes" |
| 8 | **Coordinator client**: poll over mTLS in the API process, fetch study versions, workflow versions, requests and the federation node list; post decisions and results | The agent API in `biochef-coordinator` | Integration test against the coordinator running locally with `docker compose` |
| 9 | **Podman runner** (**parallel**) in `runner.py`, same seam as `apptainer`, steps with `--network=none`. Snakemake has no podman mode, so `podman-step` is the shell its rules run under | nothing | `ci/runner_parity.py` extended to `podman` and passing (same result, step not on the host, neighbours hidden, environment not inherited); documented in README |
| 9b | **Network isolation for apptainer** (**parallel**) | nothing | `ci/runner_parity.py` proves a step cannot reach the network; the Node card shows it on |

After each part: run the full suite (`pytest -p no:cacheprovider --strict-markers`), `pip check`, `python -m compileall -q -f $(git ls-files '*.py')`, and report.

## 13. Decisions

Taken:

| Topic | Decision |
|---|---|
| Workflow approvals on a new study version | Approved again (6.2.7) |
| DAC tick | Stays (6.2.4) |
| Field names and formats | `ieeta-pt/biochef-coordinator#6`, except that `recipients_mode` stays (6.5) |
| Where the agent runs | On the same machine as the jobs (3.1) |
| Expired ID tokens | Unexpired only, until the signed list of past LS Login keys exists (7.3.5) |
| Who may undo a decision before the next poll | Anyone signed in, audited |
| Steps | Shown as `repo@digest` |
| Repeated requests | By `request_id`: the same one joins the run or repeats the refusal; a new one is a new request; the window is `not_after` (6.3) |
| Visa values | `visa_value` on each binding, compared exactly; none means `visa_missing`, with the reason on the page (6.6) |
| Apptainer without `--net` support | The run fails rather than going on with the network (part 9b) |
| SELinux under podman | `BIOCHEF_PODMAN_SELINUX=disable\|keep`, required, no default (10) |
| Withheld results | The signed notice is kept as an unsent message when the result is withheld; part 8 only sends it (6.5) |
| Egress without the Policy Engine | The agent checks `types` and `max_bytes` and marks `min_count` and `aggregates_only` not verified; manual release needs a signed, audited confirmation, and the airlock gets the same marking (6.5) |

Still open. Ask `jorgeMFS` before building anything that depends on these.

1. The agent API of the coordinator, including the decision format (7.2).
2. The editor's exact nonce construction for member keys (7.3.4). Until it is defined the check stays closed, so no real member counts as verified and no real study version can be approved.
3. The TRE airlock interface (6.5).
4. Who holds the signed list of past LS Login keys, and how nodes get it (7.3.5).
5. `all_members` as a recipients mode in `ieeta-pt/biochef-coordinator#6` (6.5).
6. A `title` field on `StudyVersion` in `ieeta-pt/biochef-coordinator#6`. Until then the page shows `study_id` and the first line of `goal`, and `study.title` stays empty (6.7).
7. A signer for workflows in `ieeta-pt/biochef-coordinator#6`, and verification of the proposer's signature on a study version. Until both exist the page shows "Proposed by" and no tick.
8. Formats the coordinator's agent API has to settle, assumed until then and listed in `node/verify.py` and `node/federation.py`: what a member's signature covers, what `payload_digest` covers, the payload's shape, and the signature and shape of the node list.
9. Sending what waits in `outgoing_message` is part 8, and so is telling the requester which rules were not verified, if `ieeta-pt/biochef-coordinator#6` gets a field for it. `Partial` signs the statement but not its `decision`, so a withheld result is told apart by its empty `subject`.
10. Member names from the `name` claim (7.3). This depends on `ieeta-pt/biochef-coordinator#6` carrying `binding_token` as it does now, and on the test with the mock AAI: the editor has to request the `profile` scope for the token to carry `name`.

## 14. Runners, and how the agent meets a TRE's task engine

Part 9 gave the agent runners. This section says what each one guarantees to the step it runs, adds a runner that hands steps to a Task Execution Service the TRE already has, adds an API that lets the TRE's own engines hand tasks to the agent, and compares the three ways of installing the agent in a TRE that runs a TES server such as Funnel. It names no TRE: what a TRE's TES server is configured to do is the operator's to say, and the agent never reads it.

### 14.1 What each runner guarantees (part 9c)

A runner says, for every step, three things, each with one of three values.

| Aspect | Question |
|---|---|
| `network` | Can the step reach anything beyond loopback? |
| `filesystem` | Can it see more than its own workspace and image? |
| `environment` | Does it inherit the agent's environment? |

| Value | Meaning |
|---|---|
| `enforced` | The agent applies it to every step, and a step it cannot apply it to fails |
| `attested` | The operator says the backend applies it, and the agent cannot check |
| `none` | Nobody applies it |

| Runner | Network | Filesystem | Environment |
|---|---|---|---|
| `subprocess` | none | none | none |
| `apptainer`, `singularity` | enforced by `--net --network none` in `BIOCHEF_APPTAINER_ARGS` | enforced by `--contain` or `--containall` | enforced by `--cleanenv` or `--containall` |
| `podman` | enforced | enforced | enforced |
| `tes` | attested, if `BIOCHEF_TES_ISOLATION=attested`, else none | the same | enforced |

Rules:

1. **`BIOCHEF_REQUIRE_NETWORK_ISOLATION`** is `true` when unset. On, the agent and the page refuse to start with a runner whose network is not `enforced`. An attested network does not count, because the rule exists for the case where nobody checks. Turning it off needs an explicit `false`, which the API process records at every start as `node.runner.isolation_not_required`, with the runner.
2. **A network check runs at start** for a runner whose network is `enforced`: one step counts the routes and addresses besides loopback, read from `/proc/net` of its own network namespace, and there must be none. With `BIOCHEF_NETWORK_PROBE=host:port`, the agent first connects to it itself, and the step then tries as well, which must fail. The result is audited as `node.runner.network_check` and stored in `node_status`. A failed check stops the agent.
3. **The Node screen** (8.7) shows the three values, the check at start (passed, failed, not yet) and whether isolation is required. This replaces the "Network isolation: off" text of 8.7 once the guarantees are read from the runner.

Done when: a runner that is not `enforced` for the network does not start with the rule on, and a host where the flag is accepted and does nothing is found by the check, in CI, through every runner that claims it.

### 14.2 The TES runner (part 9e)

`BIOCHEF_RUNNER=tes` sends each step to a GA4GH TES server as one task. Snakemake stays on the agent's host. Snakemake has no TES mode, so `tes-step` is the shell its rules run under, as `podman-step` is for podman.

What the agent keeps, because it can check it:

| Kept | How |
|---|---|
| The image | Only `repository@sha256:<64 hex>` is accepted; a tag, a scheme or a short digest stops the agent |
| The task | Built from the command and the image alone: no `env`, no credential, no stdin. The command is one argument of a fixed wrapper, never spliced into it |
| The credential | A bearer token, or a user and a password, in a file only the agent's user can read, sent only in an `Authorization` header and never in the task or the step's environment; no redirect is followed and the agent's proxy settings are not used. The URL is https, or http to this host |
| What the server sees | A copy of the workspace without `Snakefile` and `.snakemake`, in a directory of its own under `BIOCHEF_TES_STAGING`. A link or special file in the workspace stops the step. The task sees it at `/in`, read-only (Funnel mounts every input so), copies it to a scratch directory, runs the step there and writes what changed to `/out`, the one directory a TES server must make writable |
| What comes back | The task hashes the workspace before and after and returns what changed with a manifest of SHA-256. The agent places a file only if every file is plain, the manifest and the files agree exactly, and no path leaves the workspace or names `Snakefile` or `.snakemake`. If one check fails, nothing from the step is placed |
| Ownership | A server running as root returns files owned by root. `tes-step-root`, called through one sudo rule, gives the step's directory back to the agent; it has one command, `finish`, and works only two levels under `STAGING_ROOT` of `/etc/biochef-agent/tes-step.conf` |
| A killed run | The group kill does not reach the server, so when a run ends the agent cancels the run's unfinished tasks by name prefix, waits for them and clears the staging |

What it cannot keep: whether a task has a network, or sees more than its input, is the server's configuration. `BIOCHEF_TES_ISOLATION` (`attested` or `none`, required, no default) is the operator's statement, and the network and filesystem values follow it (14.1). The manifest is written by the same task as the files, so it shows that nothing changed on the way back and not that the step was honest.

Settings, all required unless said, none with a default:

| Variable | Meaning |
|---|---|
| `BIOCHEF_TES_URL` | Base URL of the server |
| `BIOCHEF_TES_IMAGE` | The image, by digest |
| `BIOCHEF_TES_AUTH` | `bearer`, `basic` or `none` |
| `BIOCHEF_TES_TOKEN_FILE` | With `bearer`: absolute path, mode 0600 |
| `BIOCHEF_TES_USER` | With `basic`: the user, with no colon |
| `BIOCHEF_TES_PASSWORD_FILE` | With `basic`: absolute path, mode 0600 |
| `BIOCHEF_TES_CA` | Optional CA bundle |
| `BIOCHEF_TES_STAGING` | A directory of the agent's that the server can read and write, at the same path |
| `BIOCHEF_TES_HELPER` | The installed `tes-step-root` |
| `BIOCHEF_TES_ISOLATION` | `attested` or `none` |

Limits of this runner: each step costs a copy of the workspace by the agent and another by the server; a step's output arrives when its task ends, not as it is written; a file a step deletes is not deleted in the workspace; the image needs `bash`, `find`, `sha256sum`, `sort`, `comm` and `cp`.

Done when: the parity workflow runs through a TES server running as root, the files come back owned by root and are the agent's after the helper, nine kinds of bad arrival are refused with nothing placed, a timed-out run leaves no task running, and no secret of the agent's is in any task posted. CI does this against two servers with one check (`ci/tes_check.py`): a stand-in that speaks the same exchange, which the unit tests also use, and Funnel v0.12.2.

Funnel v0.12.2 has a tag and no release, binary or public image, so the job builds it from the commit the tag names (`d5f0655`) and configures it from its own default config as an operator who attests network isolation would: `docker run` with no network, its own `--read-only`, and `LocalStorage.AllowedDirs` the staging root. What running it showed, and the runner follows:

- A `DIRECTORY` input and output over `file://` work, and so does the `name_prefix` filter: the check cancels a timed-out run through it and lists the run's task as `CANCELED` by it.
- Funnel mounts every input read-only. The first version of the runner wrote in the input directory and failed with `Read-only file system`, which is why the step now runs in a copy.
- The agent checks paths itself and does not rely on the TES server's directory checks.

The Funnel job runs twice, with Funnel's authentication off and with its basic authentication on (`BIOCHEF_TES_AUTH=basic`). With it on, the check also shows that Funnel refuses a request with no credentials and one with the wrong password. Not covered: Funnel's OIDC mode. The bearer path of the runner is proven against the stand-in only (14.6).

### 14.3 Singularity (part 9d)

`BIOCHEF_RUNNER=singularity` is the apptainer runner with SingularityCE: the same flags, the same `BIOCHEF_APPTAINER_ARGS`, the same guarantees (14.1).

- `BIOCHEF_APPTAINER_BINARY`, the absolute path of the binary, is required with `apptainer` and `singularity`. Snakemake has no setting for the binary: it runs `apptainer` if `shutil.which` finds one and `singularity` otherwise, and apptainer's packages install a `singularity` that is apptainer.
- The agent runs the binary with `--version` at start and refuses to start unless it says `apptainer version` or `singularity-ce version`, matching the runner. Old Sylabs Singularity says `singularity version` and is refused.
- Snakemake gets a `PATH` whose first entry is a directory of the agent's own, made once, mode 0700, holding one link named `apptainer` to the configured binary.
- Existing `apptainer` deployments must set `BIOCHEF_APPTAINER_BINARY`; the agent says so at start rather than guessing.

Done when: CI installs SingularityCE from its published package, checked against its digest, and runs the parity workflow and the network check as a user that is not root, once with the setuid starter and once with `allow setuid = no`.

### 14.4 The TES API (part 9f)

`python -m tesapi` is a listener of its own that lets the TRE's workflow engines send the node a TES task. It is not routes on the agent's API: the API's callers are federations and editors, these are the TRE's own tools, and the two share no credential, queue, slot or port. It keeps its tasks and its place as the database's one listener in two tables of the node's database, `tes_task` and `tes_listener`, and opens no other: it reads no study, request, decision, dataset binding or run.

A task is one command in one image, with files in and out. It becomes a Snakefile of one rule and runs through `BIOCHEF_RUNNER`, so it gets that runner's guarantees (14.1) and the same isolation rule and check at start. It runs in a workspace of its own, which is removed.

It refuses first:

| Refusal | Rule |
|---|---|
| No token | Every route needs `Authorization: Bearer`, service-info included. Tokens are SHA-256 in `BIOCHEF_TESAPI_TOKENS` (mode 0600), made with `python -m tesapi token <name>`. There is no way to turn authentication off, and no `/docs` or `openapi.json` |
| Off this host without TLS | `BIOCHEF_TESAPI_TLS_CERT` and `BIOCHEF_TESAPI_TLS_KEY` are required unless it listens on loopback |
| An image not listed | `BIOCHEF_TESAPI_IMAGES`, each by digest; no tag, no second executor. The listener does not start if the runner cannot run a listed image (`subprocess` runs none; rootful podman runs only the image of its configuration) |
| A URL that is not `file://` | Only `file://` on this host |
| A path not in a listed directory | Inputs are read from `BIOCHEF_TESAPI_READ_DIRS` and outputs written to `BIOCHEF_TESAPI_WRITE_DIRS`; paths are normal, absolute and behind no symlink |
| The two lists sharing a directory | The listener does not start if a read directory and a write directory are the same place or one is inside the other, so nothing a task wrote can be read back as an input and nothing it read can be overwritten. `BIOCHEF_TESAPI_DIRS`, the single list, stops it too and says what replaces it |
| The node's own data | The listener does not start if a listed directory, of either list, contains or lies inside `BIOCHEF_LOCAL_ROOT` (where studies' datasets are bound), `BIOCHEF_RUN_ROOT`, the audit log, the database file (the default one too, when `BIOCHEF_DB_URL` is unset), the node's keys, `BIOCHEF_TES_STAGING`, the token and TLS files or the apptainer cache. Each task is checked against the same list again |
| Anything of the caller's in the step | `env`, `stdin`, `stdout`, `stderr`, `volumes`, `resources` and anything not understood are refused, not ignored |
| A full queue | `BIOCHEF_TESAPI_MAX_CONCURRENT` (1) run, `BIOCHEF_TESAPI_MAX_QUEUED` (16) wait, and the next gets 503. Neither is the agent's `BIOCHEF_MAX_CONCURRENT_RUNS` or a request the coordinator sent; the machine's CPU and memory are still shared |
| Too many bytes | `BIOCHEF_TESAPI_MAX_BYTES` (1 GiB) in and out together, and `BIOCHEF_TESAPI_TIMEOUT` (900 s) |
| An unrecordable task | `BIOCHEF_AUDIT_LOG` is required, and a task is refused with 503 if its event cannot be written |

Audit events carry `"source": "tes"`: `tes.start`, `tes.task.created` (image, URLs, a SHA-256 of the command and never the arguments), `tes.task.state` at every state, `tes.task.refused`, `tes.lock.acquired` (and whom it replaced, if a killed listener's place had expired), `tes.lock.lost`, and `tes.auth.refused`, written at most once a second with the count of those not written. A task is written to no `run_summary`, so it has no screen of its own: it is not a request. The Node screen shows the listener and the newest ten tasks in a read-only card (8.7). It is written to `tes_task` (migration 0006) at every state, with the URLs of its inputs and outputs and not the inline content of an input, and the newest 1000 are kept. The listener does not start against a schema older than that, as the page does not. A task that was queued or running when the listener stopped is read back as `SYSTEM_ERROR` with the reason in `system_logs` and a `tes.task.state` event with `error: restarted`, because its workspace, its runner and its place in the queue were in the process that stopped. One listener holds a database at a time, by a lease in `tes_listener` (migration 0007): a second one started against it refuses to start and names the pid and host that hold it, because starting would end the first one's running tasks. The holder renews the lease every 10 seconds and deletes it when it stops cleanly; one that was killed is replaced after 30 seconds without a renewal, and the replacement records `tes.lock.acquired` with whom it replaced. A listener that finds its place taken, or cannot reach the database for most of the lease, stops taking tasks, cancels the ones it has and records `tes.lock.lost`. Listeners on different machines need clocks that agree to a few seconds.

Narrower than TES, and what a caller will see:

- One executor; `command` is a list of arguments.
- Paths are `/work/<file>`, and the command runs in the workspace, so an argument that starts with `/work` is written as the workspace's path and a path inside a shell script is not.
- `exit_code` is Snakemake's, not the tool's: a failure is 1 whatever the tool returned, and the tool's own message is in `stderr`.
- Logs arrive when the task ends. A caller polling a running task sees no `stdout` or `stderr` until it finishes.
- A caller sees only its own tasks.
- A `DIRECTORY` output is merged into the destination, replacing plain files and refusing anything else.

Done when: every refusal above has a test and a deliberate defect that breaks it; CI runs the listener in front of rootless podman and shows the step ran in the image, without the datasets, the agent's environment or a network, that the output is the agent's, that each refusal answers (a read directory used as an output and a write directory used as an input among them), that a listener started again on the same database still has the finished task and ends a task that was running, and that a second listener on the same database is refused while the first runs and the first gives its place up when it stops, and that the trail has the events with source `tes` and no secret.

### 14.5 Installing the agent in a TRE that runs a TES server such as Funnel

The same agent can be installed three ways. Nothing in the agent changes between them but which runner and which listener are on.

| | Beside Funnel | Sending through Funnel | In place of Funnel |
|---|---|---|---|
| What it is | The agent runs its own steps with its own runner; Funnel runs the TRE's other jobs, untouched | The agent runs Snakemake and sends each step to Funnel as a task | The TRE's engines send their tasks to the agent's TES API, and Funnel is not used for them |
| Federated steps run by | `apptainer`, `singularity` or `podman` | Funnel (`BIOCHEF_RUNNER=tes`) | The agent's runner |
| The TRE's own tasks run by | Funnel | Funnel | The agent's runner, through `python -m tesapi` |
| Network, filesystem, environment of a federated step | enforced, enforced, enforced | attested, attested, enforced | enforced, enforced, enforced |
| Same for the TRE's own tasks | Funnel's (not the agent's to say) | Funnel's | enforced, enforced, enforced |
| What the operator must attest | Nothing | That Funnel gives a task no network and only its input; its `file://` access should also be limited to the staging directory | Nothing |
| What the agent still checks | The network, at start | The image digest, an empty task, the digests of everything that comes back, the ownership of the files | The network at start; images, directories, tokens and the node's data on every task |
| Isolation rule | Passes | Needs `BIOCHEF_REQUIRE_NETWORK_ISOLATION=false`, audited at every start | Passes |
| The TRE has to provide | A container runtime the agent's user can use, as in 9 and 14.3 | A directory both can read and write at the same path (the agent checks paths itself and does not rely on the TES server's directory checks); the helper and its sudo rule if Funnel runs as root; an image with the tools of 14.2; a token | A container runtime as in the first column; the engines pointed at the agent's URL with a token; the exchange directories |
| Cost | Two systems share the machine with no shared limits: the agent's slots and step limits do not know Funnel's | The workspace is copied twice per step; output arrives at the end; every federated step waits in Funnel's queue | Whatever the TRE's tasks need beyond the narrow subset of 14.4 cannot run: several executors, `env`, volumes, resources, other storage schemes. One node, one listener per database, and a task a restart interrupted is lost (it is kept as `SYSTEM_ERROR`) |
| Records | The agent's trail and Funnel's, separately | The agent's trail for the run and Funnel's for each task | One trail, with `"source": "tes"` on the TRE's tasks |
| Fits when | The TRE keeps Funnel for its own work and the node must give federated steps the strongest guarantees the agent can check | The TRE wants one scheduler and one place for resource accounting, and accepts an operator's word about its isolation | The TRE's tasks are simple, and it wants them to run under the same guarantees as federated steps |

The first is the one that needs no decision about Funnel and loses no guarantee. The second is the only one where a federated step runs with a guarantee the agent cannot check, which is why it needs the rule turned off in writing. The third is the only one where the TRE's tasks get the agent's guarantees, and it pays for it in what a task may ask for.

### 14.6 Open

Ask `jorgeMFS` before building anything that depends on these.

1. Funnel's OIDC mode. The runner sends a bearer token, which Funnel accepts only in its OIDC mode, and that needs an OIDC provider for the job to talk to. The bearer path is tested against the stand-in only.
2. Funnel v0.12.x has a tag and no published release or image, and its version string still reads `0.12.0-rc.1`. The job builds the commit of the tag. Moving to a published release when there is one removes the build from the job.
3. Client certificates for the TES runner and the TES API. Today the credential is a bearer token over https, or http on loopback.
4. Whether the TES API may run several executors in one task, which needs a runner that can say which executor's output is which.
5. A TES runner that mounts the workspace instead of copying it, for a TES server on the same host. It would save the two copies, and give the server access to the whole workspace.
6. More than one TES API listener on one database, or one listener in several processes. Today a second one is refused. Sharing the queue and the slots between listeners is a design of its own, and the lease does not do it.
