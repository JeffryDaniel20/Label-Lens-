# LabelLens — Build Status

Companion to [IMPLEMENTATION.md](IMPLEMENTATION.md). Every planned component appears here once.
**Updated: 2026-08-30** — Phase 0 and Phase 1 fully implemented and verified against a live
PostgreSQL container (including RLS enforcement and the append-only audit trigger); Phase 2 is
fully implemented (catalog, object storage, upload completion/validation/AV-scan, and
rasterization/page normalization), verified against live MinIO, ClamAV, and PostgreSQL containers.
P2-T1's cross-phase acceptance criterion ("editing a version referenced by an analysis is
rejected") still completes only once P5-T1 (analyses) exists — see its row for detail. **Phase 3
is underway:** P3-T1 (preprocessing with coordinate transforms), P3-T2 (OCR adapter interface +
PaddleOCR), P3-T4 (fact schema), and P3-T7 (normalization library) are all implemented and tested.
P3-T2 was additionally verified against a **real PaddleOCR engine** running in a second,
purpose-installed Python 3.12 virtualenv (see below — PaddlePaddle, PaddleOCR's inference engine,
has no wheel for this project's primary Python 3.14 interpreter). P3-T3 (cloud OCR fallback) and
P3-T5 (LLM extraction) are both **blocked** on external prerequisites this session cannot supply —
D-03 (vendor choice) plus real cloud credentials for the former, an `ANTHROPIC_API_KEY` for the
latter. Per standing instructions, Phase 4 and D-01 remain untouched.

## Legend

| Mark | Meaning |
|---|---|
| `[x]` | Done — concrete code exists and tests prove it. Evidence column names the files and tests. |
| `[ ]` | Not started, in progress, or partially done. Partial work is spelled out in Notes. |
| ~~struck~~ | Explicitly cut from the MVP — do not build. |

Rules: never mark `[x]` without code **and** test evidence. Every `[ ]` that has partial work must
say what exists and what is missing. Blocked items name the blocker.

## Verification snapshot

Run from `backend/` with the project's primary virtualenv (Python 3.14), with `LABELLENS_TEST_PG_URL`,
`LABELLENS_TEST_S3_ENDPOINT`, and `LABELLENS_TEST_CLAMD_HOST` pointed at a live PostgreSQL 16, MinIO,
and ClamAV container respectively, last executed 2026-08-30:

| Check | Command | Result |
|---|---|---|
| Tests | `pytest -q` | **421 passed, 8 skipped** (the 8 are `paddleocr`-marked — see the separate PaddleOCR verification below) |
| Coverage | `pytest --cov=app` | **94%** overall; identity 93–100%, catalog 100%, storage 98–100%, ingestion 80–100%, vision (preprocess/transform/base/service) 93–100%, extraction `facts.py`/`normalize/*` 94–100%, `vision/ocr/paddle.py` 0% in *this* venv only (paddlepaddle has no wheel for Python 3.14 — see below), rules n/a (not built) |
| Lint | `ruff check .` | clean |
| Types | `mypy` | clean, 57 source files (`app.identity.*` under strict) |
| Dependency audit | `pip-audit --strict` | no known vulnerabilities (incl. new `pypdfium2`, `pillow`, `pillow-heif`, `opencv-python-headless`, `numpy`, `py3langid`) |
| Full stack | `docker compose -f infra/docker-compose.yml up --build` | postgres, redis, minio, api all healthy; verified with live HTTP requests (see P0-T2) |

**Real PaddleOCR verification, in a second virtualenv (`backend/.venv312`, Python 3.12.10 — see P3-T2
for why):** `pytest -m paddleocr -q` against the same live services → **8 passed, 0 skipped, 0 failed**.
This is the evidence that P3-T2's PaddleOCR adapter genuinely works, not just that it's wired up
correctly against a fake. Running the *entire* suite from that second venv also surfaced one
unrelated, pre-existing, environment-specific flake: `test_absolute_expiry_wins_over_idle_refresh`
(session absolute-expiry test) fails consistently under Python 3.12.10 on this machine but passes
consistently under the primary Python 3.14.6 venv, on unmodified code — a wall-clock-resolution
edge case in a test asserting strict `>` against a zero-second TTL boundary, not a regression from
any change made this session (nothing in `app/identity/` was touched). Documented here rather than
silently ignored; not fixed, since it's outside this task's scope.

Every `postgres`-, `object_storage`-, and `clamav`-marked test now runs and passes; nothing is
skipped anywhere in the primary-venv suite except the `paddleocr`-marked tests (verified separately,
above). Of the 421: 4 exercise PostgreSQL-only guarantees (RLS enablement, RLS enforcement across
all nine tenant tables including `files`, `file_pages`, `ocr_results` and `ocr_tokens`, the
superuser-bypass documentation test, the audit-log append-only trigger), roughly 30 exercise a live
MinIO endpoint (direct-to-storage upload/download round-trips, expired-URL rejection, real
adversarial-file rejection with confirmed object deletion, dedup, rasterized-page storage and
retrieval), and 3 exercise a live ClamAV daemon (clean-file verdict, a real `FOUND` verdict for the
EICAR test string, and end-to-end 400 handling of an infected verdict).

**Bug found and fixed while re-verifying against a freshly-provisioned PostgreSQL container (not a
regression from this session's feature work, but only surfaced by it):** the `unprivileged_role`
fixture in `tests/integration/test_migrations_and_rls.py` built its non-superuser connection URL
with a hard-coded `PG_URL.replace("://labellens:labellens@", ...)`, matching only the
`infra/docker-compose.yml` dev credential convention. Against a test container provisioned with
different credentials (e.g. `postgres:labellens`), `.replace()` silently no-ops, and the "unprivileged"
connection silently falls back to the original superuser credentials — defeating the entire RLS test
without ever failing loudly (the test still passed, but for the wrong reason: it was exercising the
superuser-bypass path, not the policy). Fixed by building the URL with SQLAlchemy's
`make_url(PG_URL).set(username=role, password=password)`, which is correct regardless of the
source URL's own credentials. Re-verified: with the fix, the same test against the same container
now genuinely exercises RLS as the non-superuser role and still passes.

---

## Phase 0 — Foundation

| ID | Task | Done | Notes / Evidence |
|---|---|---|---|
| P0-T1 | Repository scaffold and tooling | `[x]` | `backend/pyproject.toml` (ruff, mypy strict on identity, pytest markers), `Makefile`, `.gitignore`. Proven by the whole suite running from a clean checkout. |
| P0-T2 | Docker Compose dev environment | `[x]` | Executed 2026-08-29: `docker compose -f infra/docker-compose.yml up -d --build` brought up postgres, redis, minio (+ `minio-init` bucket bootstrap), and api, all reaching `healthy`/running. Fixed a real bug found in the process: `backend/Dockerfile` copied only `pyproject.toml` before `pip install ".[dev]"`, but setuptools needs the `app/` package directory present to resolve the local package, so the install failed with `package directory 'app' does not exist`. Fixed by reordering to `COPY . .` before install. After the fix: `alembic upgrade head` ran automatically on container start against real Postgres; `curl /healthz` returned `{"status":"ok"}`; `curl /readyz` returned `{"status":"ready","checks":{"database":"ok"}}`; a real signup POST returned `201` with a session cookie, backed by real Redis (not the in-memory fallback). MinIO logs confirm `Bucket created successfully local/labellens-uploads` set to private. Torn down with `docker compose down -v`, no leftover containers or volumes. |
| P0-T3 | Config, logging, errors, correlation IDs | `[x]` | `app/platform/{config,logging,errors,middleware}.py`. Tests: `tests/unit/test_platform.py::TestConfig` (fail-fast on missing/placeholder/short secret), `::TestRedaction`, `::TestCorrelationId`, `tests/integration/test_auth_and_ops.py::TestOps` (problem+json shape, correlation id echo, security headers, 413 body cap). |
| P0-T4 | DB engine, session, migrations baseline, health | `[x]` | `app/db/{base,session,models}.py`, `migrations/`, `/healthz` + `/readyz` in `app/main.py`. Tests: `tests/integration/test_migrations_and_rls.py::test_migration_upgrade_and_downgrade` (up then `downgrade base` leaves no tables), `::test_migrated_schema_matches_the_models`, `TestOps::test_readyz_reports_each_dependency`. |
| P0-T5 | CI pipeline | `[ ]` | `.github/workflows/ci.yml` authored and each of its steps individually reproduced locally with matching results (lint clean, mypy clean, 247/247 tests passing, 95% coverage, `pip-audit --strict` finds nothing today). The dependency-audit step's `|| true` escape hatch has been removed, so a future vulnerable dependency now fails the build instead of being silently ignored. **Still not executed as an actual GitHub Actions run** — this repository has no remote configured, so the workflow itself has never fired; pushing to a remote is outside this session's scope. |

---

## Phase 1 — Tenancy & Identity

| ID | Task | Done | Notes / Evidence |
|---|---|---|---|
| P1-T1 | Org/user/membership schema + migration | `[x]` | `app/identity/models.py`, `migrations/versions/0001_identity_catalog_audit.py`. Unique `(organization_id, user_id)` and unique email enforced; enum columns round-trip via `enum_column()`. Tests: migration up/down, `TestMembers::test_duplicate_membership_conflicts`. |
| P1-T2 | Password auth, sessions, login/logout, lockout | `[x]` | `app/identity/{passwords,sessions,service,router}.py`. Argon2id, server-side sessions with idle + absolute expiry, httpOnly/SameSite cookies, lockout. Tests: `TestSignupAndLogin` (5 tests incl. lockout at the configured limit and counter reset on success), `tests/unit/test_platform.py::TestSessions` (revocation, absolute expiry), `TestPasswords`. |
| P1-T3 | RBAC capability matrix + dependency | `[x]` | `app/identity/rbac.py`, `require()` in `app/identity/deps.py`. Tests: `tests/unit/test_rbac_and_passwords.py` parametrises **every role × every capability** (100 assertions) against an independently written expected matrix, plus monotonicity by role rank; `tests/security/…::TestAuthorizationSurface` proves 403s at the HTTP layer. |
| P1-T4 | Tenant scoping + RLS + isolation suite | `[x]` | **Application layer:** `tenant_scoped()` refuses non-tenant models; `tests/security/test_tenant_isolation.py` (21 tests) asserts 404-not-403 on products, versions, memberships and API keys across two orgs. **RLS, verified against a live PostgreSQL 16 container on 2026-08-29:** `test_rls_is_enabled_on_every_tenant_table` and `test_rls_blocks_rows_from_another_tenant` pass. Verifying the latter surfaced a real gap in the original test: the Postgres bootstrap user is a **superuser**, and superusers bypass RLS unconditionally regardless of `FORCE ROW LEVEL SECURITY` — so the original assertion (connecting as that bootstrap user) would have passed even if the policies enforced nothing at all. Fixed by provisioning a throwaway `NOSUPERUSER` role for the assertion, and added `test_superuser_bypasses_rls_a_known_postgres_limitation`, which documents the limitation by proving it directly (asserts the bootstrap user *is* superuser and *does* see both orgs' rows). Written up in the `docs/adr/0002-tenant-isolation.md` addendum, with an action item that the production DB role must be a dedicated non-superuser (tracked under P7-T7). All 4 `postgres`-marked tests now pass; 0 skipped anywhere in the suite. |
| P1-T5 | Append-only audit log | `[x]` | `app/audit/{models,service}.py`, `GET /v1/audit-logs` (Admin-only, tenant-scoped). Failure-path events survive rollback via `record_out_of_band()` (see `docs/adr/0003`). Tests: `TestAuditTrail` (5 tests). **Immutability trigger verified against live PostgreSQL on 2026-08-29:** `test_audit_log_rejects_update_and_delete` passes — both `UPDATE` and `DELETE` against `audit_logs` raise `DatabaseError` from the append-only trigger. Unlike RLS, this needed no role change: triggers fire for superusers too, so the original test was valid as written. |
| P1-T6 | TOTP MFA + API keys | `[x]` | `service.start_mfa_enrolment/confirm_mfa_enrolment/create_api_key/authenticate_api_key`. Tests: `TestMfa` (enrol → login challenged → login with TOTP; recovery code works exactly once; codes stored hashed) and `TestApiKeys` (create/use/revoke takes effect immediately, Owner/Admin roles refused for keys, malformed and unknown keys rejected). |

---

## Phase 2 — Catalog & Ingestion

| ID | Task | Done | Notes / Evidence |
|---|---|---|---|
| P2-T1 | Products and immutable product versions | `[ ]` | **Otherwise complete:** `app/catalog/{models,service,router}.py` — tenant-scoped CRUD, auto-incrementing versions, previous version superseded, `lock_version()` + `StateInvalid` (409) on editing a locked version. Tests: `TestProducts`, `TestProductVersions` (4 tests) at 100% module coverage. **Left `[ ]` solely because:** locking is currently triggered manually since analyses do not exist yet; the specific acceptance criterion "editing a version referenced by an analysis is rejected" needs a real analysis to lock a version against, so it completes only once P5-T1 exists. Everything else P2-T1 calls for is done and tested. |
| P2-T2 | Object storage adapter + presigned uploads | `[x]` | `app/storage/{client,keys,service,router}.py` — S3-compatible adapter (boto3, works against MinIO/R2/B2 through one client), a key scheme namespaced `org/{org_id}/pv/{version_id}/{uuid}.{ext}` so ownership is checkable from the key string alone, and `POST /v1/product-versions/{id}/uploads` / `GET /v1/files/download-url` endpoints. Tests: `tests/unit/test_storage.py` (18 tests — key scheme, extension allowlist, org ownership, a fake-client service test proving a foreign product version or key is rejected before any network call), `tests/integration/test_storage_and_uploads.py` (auth/capability/tenancy on the router, plus an `object_storage`-marked suite executed 2026-08-29 against a live MinIO container: a real HTTP `PUT` through a presigned URL followed by a real `GET` recovers identical bytes, `head_object`/`delete_object` round-trip, and both an expired upload URL and an expired download URL are rejected by MinIO itself after sleeping past a 1-second TTL). This directly proves the acceptance criterion "a file uploads directly to storage without passing through the API" — the API process never saw the payload bytes. |
| P2-T3 | Upload completion, validation, and AV scan | `[x]` | `app/catalog/models.py` (`File`/`FileStatus`/`AvStatus`), `migrations/versions/0002_files.py` (table + RLS policy), `app/ingestion/{magic_bytes,pdf_checks,av,service,router}.py`. Hand-rolled magic-byte sniffing (no libmagic dependency) for jpg/png/webp/heic/tiff/pdf with a claimed-extension cross-check; `pypdf`-based structural validation rejecting encrypted PDFs, embedded JavaScript/files, and page counts over a configurable cap; a real `clamd` adapter (`ClamdAvScanner`) with an explicit `NullAvScanner` fallback that reports `av_status=skipped` rather than ever fabricating "clean"; dedup by `(org_id, sha256)` that deletes the duplicate object and reuses the existing storage key. Tests: `tests/unit/test_ingestion.py` (26 tests — magic-byte signatures including SVG-as-PNG and an EXE header both correctly unrecognized, extension cross-check, PDF encryption/page-cap/malformed rejection, AV factory and unreachable-daemon handling) and `tests/integration/test_ingestion_upload_completion.py`, run 2026-08-29 against live MinIO **and** a live ClamAV daemon: full happy-path upload→ready with a real PDF page count, five adversarial rejections (SVG-as-PNG, oversized-page PDF, malformed PDF, oversized file, disallowed extension) each confirmed to delete the object from real storage, three tenant-isolation checks (foreign version, foreign key, uploading-without-completing), and dedup verified to produce two `File` rows sharing one `storage_key` with the duplicate's own object confirmed deleted. The live-AV class proves three things separately and honestly: (1) a clean real image is marked `clean`, (2) the `ClamdAvScanner` really talks to a live daemon and gets a real `FOUND` verdict for the plain EICAR test string, and (3) the service layer correctly turns an `INFECTED` verdict into a 400 response via a stub scanner — split this way after empirically finding that this ClamAV build's container-aware scanning does not flag EICAR once it's embedded inside a structurally-recognized JPEG/PDF (a property of the daemon's engine, not of this integration; documented inline in the test file). **Deferred, scoped out of this task on purpose:** true gigabyte-scale zip-bomb PDF fixtures (the page-cap and malformed-parse guards are proven; an actual multi-GB decompression bomb is not manufactured for the test suite) and EXIF-specific payload attacks (EXIF stripping is Phase 3 preprocessing, not ingestion). |
| P2-T4 | Rasterization and page normalization | `[x]` | `app/catalog/models.py` (`FilePage`), `migrations/versions/0003_file_pages.py` (table + RLS policy), `app/ingestion/rasterize.py`, `app/storage/keys.py` (`build_render_key`, content-addressed by sha256 so files that dedup to identical content also share rendered pages instead of re-rasterizing), `app/storage/client.py` (`put_object`), `app/ingestion/service.py` (`_get_or_render_pages`), `GET /v1/files/{file_id}/pages`. PDFs are rasterized page-by-page via `pypdfium2` at 300 DPI (`scale = 300/72`); every other allowed format (jpg/png/webp/heic/tiff) is normalized to one page via Pillow + `pillow-heif`, with `ImageOps.exif_transpose` baking in EXIF orientation and re-encoding to a fresh PNG with no `exif=` payload stripping all other metadata. A decode failure (valid magic bytes, undecodable body) is treated as further evidence of a bad upload and rejected the same way as a failed AV/content check — object deleted, `file.rejected` audited, 400 returned. Tests: `tests/unit/test_rasterize.py` (11 tests, no live services — multi/single-page PDF dimensions at the correct 300/72 DPI scale factor, malformed-PDF rejection, plain/rotated JPEG orientation normalization with width/height swap verified, PNG passthrough, a genuinely encoded-then-decoded HEIC image via `pillow_heif.from_pillow(...)`, and a corrupt-JPEG-body rejection) and `tests/integration/test_ingestion_upload_completion.py::TestRasterizationAndPageNormalization` (4 tests, run 2026-08-30 against live MinIO — a real 3-page PDF yields 3 `file_pages` rows at ~833×833px, a real rotated JPEG's rendered PNG is fetched back through the existing `/v1/files/download-url` endpoint and confirmed both dimension-swapped and EXIF-free, two files with identical content are confirmed to share one `render_key` while still getting their own `FilePage` row each, and a file with correct JPEG magic bytes but an undecodable body is rejected end-to-end with no file left in the version's file list). The existing happy-path/dedup tests were updated to use a genuinely decodable JPEG fixture (built via Pillow) rather than bare magic-byte bytes, since a valid upload must now also survive rasterization. **Deferred, scoped out on purpose:** re-rasterizing already-rendered pages when a corrupted/missing render object is later discovered (no backfill path — acceptable since this is a fresh system with no legacy data). |

---

## Phase 3 — Vision & Extraction

| ID | Task | Done | Notes / Evidence |
|---|---|---|---|
| P3-T1 | Preprocessing with invertible coordinate transforms | `[x]` | `app/vision/transform.py` (`AffineTransform` — a 3x3 homogeneous matrix stored as a plain nested tuple so dataclass equality never hits numpy's ambiguous-truth-value trap; `.apply`, `.inverse`, `.then` for composition), `app/vision/preprocess.py` (`rotate`, `deskew`, `denoise`, `enhance_contrast`, `binarize`, each returning `(image, AffineTransform)`, plus `preprocess()` composing all four into one pipeline and `map_bbox_to_original()`). `rotate()` expands the canvas so nothing is cropped and returns the exact OpenCV matrix it applied; `deskew()` estimates a correction angle via Otsu threshold + `minAreaRect` on foreground pixels (folded into `(-45°, 45°]` since a rectangle's angle is only defined modulo 90°) and calls `rotate()` with it, or returns the identity transform below a 0.1° noise floor or on a blank page. `denoise`/`enhance_contrast`/`binarize` (fastNlMeansDenoising, CLAHE, Otsu binarization) only touch pixel values, never geometry, so each returns the identity transform. No live services needed — this is pure numpy/OpenCV math with no DB/storage/network dependency. Tests (28, all in `tests/unit/`): `test_vision_transform.py` (12 — identity, translation/rotation `apply`, forward-then-inverse recovers the original point across 5 angles including 90° and 177°, composition order, a composed transform's own round trip); `test_vision_preprocess.py` (16 — the core property test parametrized over 6 rotation angles confirms a known point maps forward and back within 1px through `rotate()` alone, a second parametrized test over 4 skew angles confirms the same round-trip property holds through the **full composed pipeline** on a deliberately skewed synthetic fixture, a separate looser test confirms `deskew()`'s estimated correction angle is within 3° of the true value on two skew angles — a sanity check that it isn't a no-op stub, not a precision claim — a blank-page no-op case, identity-transform + shape checks for each pixel-only step, and `binarize` producing strictly `{0, 255}` output). This directly proves the acceptance criterion: any bbox produced on a preprocessed image can be expressed in original coordinates via `to_original`, the composed inverse of every geometric step's transform. **Not yet wired into the ingestion pipeline or persisted anywhere** — P3-T1 is a pure, tested library; P3-T2 (OCR) is what will actually call `preprocess()` against a real `file_pages` render and store results. |
| P3-T2 | OCR adapter interface + PaddleOCR | `[x]` | `app/vision/ocr/base.py` (`OcrEngine` protocol, `OcrToken` — text, confidence, bbox, line_no, language), `app/vision/ocr/paddle.py` (`PaddleOcrEngine`, PP-OCRv6 via a local PaddleOCR pipeline), `app/vision/models.py` (`OcrResult`, `OcrTokenRow`), `migrations/versions/0004_ocr.py`, `app/vision/ocr/service.py` (`run_ocr()` — downloads a page's render, runs `preprocess()`, runs the engine, maps every token's bbox back to *original* page coordinates via `map_bbox_to_original()`, persists both tables; `tokens_overlapping()` — a genuine spatial query). Fixed a real correctness bug found while wiring this up: P3-T1's `map_bbox_to_original()` mapped only two corners of a bbox, which silently drops two corners of the quadrilateral a rotation produces and can flip min/max ordering — fixed to map all 4 corners and return their axis-aligned bounding box, with `tests/unit/test_vision_preprocess.py` extended with a dedicated rotation case proving the new behavior (the old test was renamed to reflect what it actually tests: the axis-aligned/near-identity case). On PostgreSQL, `ocr_tokens` additionally gets a **generated `box` column + GiST index** (`bbox_box box GENERATED ALWAYS AS (box(point(x1,y1), point(x2,y2))) STORED`), verified live 2026-08-30 by inserting tokens directly and running `SELECT ... WHERE bbox_box && box(point(0,0), point(100,100))` — it returns exactly the token inside that region, and `EXPLAIN` confirms Postgres's planner actually uses `Index Scan using ix_ocr_tokens_bbox_gist`, not a sequential scan — directly proving the DB schema's "GIN/GiST for spatial lookup" requirement, not just declaring it. `analysis_id` is deliberately absent from both tables (schema calls for it, but `analyses` doesn't exist until P5-T1); scoped to `file_page_id` only for now, exactly like P2-T1's manual locking. **PaddlePaddle (PaddleOCR's inference engine) publishes no wheel past cp313**, so it cannot install into this project's primary Python 3.14 venv; a second Python 3.12.10 virtualenv (`backend/.venv312`, installed via `winget install Python.Python.3.12`) was created solely to install and genuinely run it — see the Verification snapshot above. A real, reproducible bug was hit and fixed along the way: PaddlePaddle 3.3.1's oneDNN/PIR combination on CPU raises `NotImplementedError: ConvertPirAttribute2RuntimeAttribute ...` on `.predict()`; setting `FLAGS_use_mkldnn=0` and `enable_mkldnn=False` avoids the broken code path entirely (a paddle-side issue, not this integration's). Tests: `tests/unit/test_vision_ocr_service.py` (6, fake-engine — result/token persistence, the engine receives the *preprocessed* not raw image, a token's bbox is recorded in original- not preprocessed-image coordinates after a real 20° rotation, cross-org access is 404, zero tokens gives `avg_confidence=0.0` not a crash, spatial query filters correctly) and `tests/integration/test_vision_ocr_paddle.py` (8, marked `paddleocr`, run for real against `.venv312` 2026-08-30 — golden-crop WER ≤ 0.15 per line / ≤ 0.10 average against a clean synthetic 3-line label using the real PP-OCRv6 pipeline, confidence ≥ 0.90 on clean text, bbox well-formedness and top-to-bottom line ordering, engine-version determinism across repeated queries and separate instances, and — the key acceptance-criterion proof — a rotated fixture run through the real engine with its tokens' bboxes mapped back to the original (still-rotated) image and **visually/quantitatively confirmed to contain actual text ink**, not just *some* coordinates, plus a full `run_ocr()`-to-`tokens_overlapping()` pipeline test using the real engine end-to-end). **Deferred, scoped out on purpose:** word-level token splitting (`return_word_box=True`) — PaddleOCR's default configuration recognizes whole *lines*, so each `OcrToken` here is one line, which still satisfies the protocol's text/confidence/bbox/line_no/language contract; per-token language detection (this pipeline reports language at the engine-configuration level, not per line). |
| P3-T3 | Cloud OCR fallback + escalation policy | `[ ]` | **Blocked, not attempted:** this task's own acceptance criteria require a real cloud OCR vendor call (IMPLEMENTATION.md specifies Google Cloud Vision), but **D-03 (cloud OCR fallback vendor: Google Vision vs Azure Read) is still an open decision**, and either vendor would additionally require real cloud credentials (a GCP or Azure account with billing enabled) that are not available in this environment and cannot be provisioned by this session. Nothing else in Phase 3 depends on P3-T3 (`P3-T5` depends only on `P3-T2, P3-T4`), so it is skipped in dependency order rather than blocking the rest of the phase, matching the same honest-blocker pattern as P2-T1/P5-T1. |
| P3-T4 | Fact schema (contract that unblocks Phase 4) | `[x]` | `app/extraction/facts.py` — `Fact[T]` (a generic Pydantic wrapper enforcing the one invariant this schema exists for: exactly one of `value` / `not_found_reason` is ever set, so a field can never be silently missing — it must be a real value or an explicit, reasoned absence), and `LabelFacts` composing all eight categories IMPLEMENTATION.md names: `IngredientsFacts` (declared text + parsed `IngredientItem` list with position/percentage), `AllergensFacts`, `NutritionFacts` (`NutritionRow` per nutrient, per-100g/per-serving), `QuantityFacts`, `DatesFacts` (manufacture/expiry-or-best-before/batch), `ClaimsFacts`, `AddressesFacts` (role-typed: manufacturer/packer/marketer/importer), `LanguagesFacts`. `schema_version` is a `Literal["1.0.0"]` field backed by the `SCHEMA_VERSION` module constant — a payload claiming any other version is rejected at parse time, which is what lets a future analysis be re-read against exactly the schema version it was produced under. Deliberately excludes per-field confidence/evidence (those belong to `extracted_fields`/`evidence_spans` in P3-T5/P3-T6, keyed by the same dotted path the rule DSL examples already use, e.g. `ingredients.items`) — this file only defines the shape of the data. Both `Fact` and `LabelFacts` are frozen (`ConfigDict(frozen=True)`), so a constructed fact set can't be mutated after the fact (pun acknowledged) — analyses must always create a new one, never edit in place. No live services needed — pure Pydantic. Tests: `tests/unit/test_extraction_facts.py` (15 — the value-xor-reason invariant in all four combinations plus the `found`/`missing` constructors, frozen-instance mutation rejected on both `Fact` and `LabelFacts`, a fully-populated label round-trips through `model_dump_json`/`model_validate_json` byte-for-byte, the "fabrication bait" case — **every single fact `not_found`** — also constructs and round-trips validly rather than erroring, an incompatible `schema_version` in a payload is rejected, `model_json_schema()` documents exactly the eight fact categories plus `schema_version`, and field-level checks — `Address.role` rejects an unlisted role, `IngredientItem.percentage` is genuinely optional). This directly satisfies the acceptance criterion's testable half now (documented, versioned, round-trips, required/optional semantics enforced); "imported by both `extraction` and `rules` fixtures" completes once those modules exist (P3-T5 onward, and Phase 4). |
| P3-T5 | LLM extraction with untrusted-data framing | `[ ]` | **Blocked, not attempted:** needs a live LLM provider call (IMPLEMENTATION.md specifies Claude/Anthropic) against a real `ANTHROPIC_API_KEY`, which is not configured in this environment and cannot be provisioned by this session — the same class of blocker as P3-T3, just a different provider. Its other dependency, P3-T4, is done. |
| P3-T6 | Evidence verification gate (anti-hallucination) | `[ ]` | Not started — depends on P3-T5. |
| P3-T7 | Normalization library | `[x]` | `app/extraction/normalize/` — `NORMALIZER_VERSION` ("1.0.0", the single version string for this whole library), `numbers.py` (`parse_locale_number` — resolves `.`/`,` as decimal point or thousands separator: both present → whichever comes *last* wins; comma-only → a single comma with exactly 1–2 trailing digits reads as a decimal comma, else thousands; dot-only → decimal unless there is more than one dot), `units.py` (`parse_quantity` converts mass to grams / volume to millilitres via a lookup table; `IU` is recognized but deliberately never converted, since its mass ratio is substance-specific and converting it would fabricate a number; `parse_nutrition_basis` classifies a nutrition-table header as `per_100`/`per_serving`/`unknown`), `dates.py` (`normalize_label_date` — day-first for numeric dates, matching FSSAI/EU convention rather than assuming US month-first; a month/year-only declaration like "DEC 2027" normalizes to the partial ISO form `"2027-12"` instead of raising, since that is a real, common "best before" convention), `allergens.py` (the EU's 14 legally-declarable allergens + FSSAI's major-allergen list, with a synonym dictionary — `canonicalize_allergen`/`is_allergen`), `ingredients.py` (`split_top_level` — a paren-depth-aware comma splitter, `parse_ingredients` — returns `app.extraction.facts.IngredientItem` directly, extracting a trailing QUID percentage *only* when a parenthetical is nothing but a percentage, leaving a sub-ingredient list's own parenthetical untouched), `language.py` (`detect_language` via `py3langid`, restricted to a curated language set relevant to FSSAI/EU rather than its full ~97-language default — unrestricted, "Contains: Milk, Soy" misclassifies as Swedish; restricted, it's correctly English — and returns `None` below a 12-character floor rather than a low-confidence guess). `py3langid` was chosen over the more common `langdetect` specifically because `langdetect`'s classifier is not deterministic run-to-run (it samples from a PRNG unless the caller remembers to seed it), which would have violated this task's own "deterministic" requirement; `py3langid` has no randomness. Every function is pure (no clock, no OS locale, no network, no randomness) — no live services needed. Tests (111, all in `tests/unit/`, table-driven): `test_normalize_numbers.py` (20 — locale decimals, thousands separators, both-separators-present in both US and EU order, negative/signed, malformed rejection), `test_normalize_units.py` (26 — mass/volume conversions incl. a locale-decimal quantity, IU passthrough, unrecognized-unit rejection, and the required **per-100g vs per-serving** classification table), `test_normalize_dates.py` (18 — full day-first dates incl. a case that would parse to the wrong date under US month-first convention, 2-digit years, month-name and ISO forms, **month/year-only declarations**, invalid-date and unparsable rejection), `test_normalize_allergens.py` (19 — every synonym → canonical mapping, non-allergens correctly unrecognized, and a dictionary-integrity check that every synonym points at a real canonical key and every canonical key has at least one synonym), `test_normalize_ingredients.py` (11 — plain lists, the required **nested-parentheses** case proving a sub-ingredient list's internal commas don't fragment the top-level split even when doubly nested, bare vs. locale-decimal QUID percentage extraction, and confirming a sub-ingredient parenthetical without a percentage is correctly left untouched), `test_normalize_language.py` (9 — four real-language blocks incl. German and French correctly detected only after restricting the candidate set, short text returning `None` rather than a guess, and 20 repeated calls on the same input producing identical results), `test_normalize_version.py` (3 — the version constant is stable, and normalizer output is directly usable to construct real `IngredientsFacts`/`NutritionFacts` instances from P3-T4, proving the two tasks actually compose). **Acceptance's other half** — "normalizer version is recorded in the model manifest" — completes once `model_manifests` exists (Phase 5+, per the DB schema in IMPLEMENTATION.md §6); `NORMALIZER_VERSION` is exported now so that wiring is a one-line addition, not a redesign. |
| P3-T8 | Confidence model and tier routing | `[ ]` | Not started — depends on P3-T6 (blocked on P3-T5's LLM credential) and P3-T7 (done). |
| P3-T9 | Category & jurisdiction classification | `[ ]` | Not started — unblocked (P3-T7 is done); next in dependency order once picked up. |

---

## Phases 4–7 — Not started

No code exists for any row below.

| ID | Task | Done |
|---|---|---|
| P4-T1 | Rule DSL schema and loader | `[ ]` |
| P4-T2 | Predicate library | `[ ]` |
| P4-T3 | Evaluator, applicability, effective dates | `[ ]` |
| P4-T4 | Ruleset versioning, publishing, pinning | `[ ]` |
| P4-T5 | First jurisdiction pack + fixtures | `[ ]` |
| P4-T6 | Rule authoring docs + scaffolding CLI | `[ ]` |
| P5-T1 | Analysis entity, state machine, idempotency | `[ ]` |
| P5-T2 | Queue infrastructure and stage jobs | `[ ]` |
| P5-T3 | Retries, timeouts, DLQ, janitor | `[ ]` |
| P5-T4 | Findings + evidence persistence | `[ ]` |
| P5-T5 | SSE progress + cost accounting | `[ ]` |
| P6-T1 | Frontend shell, auth, API client | `[ ]` |
| P6-T2 | Catalog and upload UI | `[ ]` |
| P6-T3 | Analysis dashboard + live progress | `[ ]` |
| P6-T4 | Label viewer canvas with evidence overlay | `[ ]` |
| P6-T5 | Findings panel + review actions | `[ ]` |
| P6-T6 | Review queue and sign-off | `[ ]` |
| P6-T7 | Version comparison view | `[ ]` |
| P7-T1 | Report snapshot assembler | `[ ]` |
| P7-T2 | PDF rendering and signed delivery | `[ ]` |
| P7-T3 | Golden dataset + eval harness | `[ ]` |
| P7-T4 | Adversarial suite | `[ ]` |
| P7-T5 | Observability stack + alerts | `[ ]` |
| P7-T6 | Backups, restore drill, runbooks | `[ ]` |
| P7-T7 | Production deploy pipeline + rollback | `[ ]` |
| P7-T8 | Retention, deletion, DSR | `[ ]` |
| P7-T9 | Security review pass | `[ ]` |

---

## Cross-cutting components

| ID | Component | Done | Notes / Evidence |
|---|---|---|---|
| X-01 | Module boundary enforcement (import lint) | `[ ]` | Boundaries are respected by hand today; no automated import-lint rule yet. |
| X-02 | `rules` engine purity (no clock, no I/O) | `[ ]` | Module does not exist yet. |
| X-03 | RLS on every tenant table | `[x]` | Policies verified enabled on all nine tenant tables (`memberships`, `api_keys`, `products`, `product_versions`, `files`, `file_pages`, `ocr_results`, `ocr_tokens`, `audit_logs`) against a live PostgreSQL container, and verified to actually block cross-tenant rows when queried as a non-superuser role (see P1-T4 for the superuser-bypass finding and fix, and the verification-snapshot note above for a fixture credential bug found and fixed in an earlier session). Each new table was added and its policy confirmed by the same generic `test_rls_is_enabled_on_every_tenant_table` loop, without any test-file changes needed beyond appending to `TENANT_TABLES`. **Still missing:** an automated guard rail that fails CI when a *new* tenant table is added without a policy — this remains a manual discipline for now. |
| X-04 | Cross-tenant access returns 404, never 403 | `[x]` | `catalog/service.get_product/get_version`, `deps._load_membership`. Tests: `TestCrossTenantResourceAccess` — 12 assertions across products, versions, memberships, API keys. |
| X-05 | Immutability triggers | `[ ]` | **Verified for `audit_logs`** against live PostgreSQL (see P1-T5) — the only append-only table that exists yet. **Not applicable to analyses, findings, rulesets, or reports**, because none of those tables exist yet — they arrive in Phases 3–5 and 7, and each will need the same trigger plus its own verification test before it can be marked done. |
| X-06 | Model manifest pinning per analysis | `[ ]` | Not started. |
| X-07 | Historical reproducibility of analyses | `[ ]` | Not started. |
| X-08 | `insufficient_data` never resolves to `pass` | `[ ]` | Not started (rule engine). |
| X-09 | Evidence chain completeness | `[ ]` | Not started. |
| X-10 | Bounding boxes in original image coordinates | `[ ]` | Not started. |
| X-11 | Confidence tier routing | `[ ]` | Not started. |
| X-12 | Prompt-injection defenses + corpus | `[ ]` | Not started — no model calls exist yet. |
| X-13 | Egress allowlist; never fetch URLs from labels | `[ ]` | Not started. |
| X-14 | Secrets management | `[x]` | No secrets in the repo; `.env.example` only; `Settings` has no defaults for `secret_key`/`database_url` and rejects placeholders, so startup fails loudly. Tests: `TestConfig` (3 tests). Logs redact secrets (`TestRedaction`). |
| X-15 | Cost controls | `[ ]` | Not started. |
| X-16 | Correlation ID propagation | `[ ]` | **HTTP layer done:** generated or sanitised per request, returned in `X-Correlation-Id`, bound into logs and written onto every audit row (`test_audit_rows_carry_the_request_correlation_id`). Propagation into jobs and provider calls awaits Phase 5. |
| X-17 | Local-only mode (cloud AI disabled per org) | `[ ]` | `organizations.cloud_ai_enabled` column exists; nothing reads it yet. |
| X-18 | Documentation set | `[ ]` | Present: `README.md`, three ADRs (`docs/adr/0001–0003`), `docs/runbooks/local-development.md`. Missing: API reference, rule-authoring guide, incident/restore/key-rotation runbooks. |

---

## Security suite inventory

The gate that must never regress. All currently passing — the SQLite-backed tests on every run,
and the PostgreSQL-only row confirmed against a live container on 2026-08-29.

| Area | Tests | Done |
|---|---|---|
| Cross-tenant resource access (products, versions, members, API keys) | `TestCrossTenantResourceAccess` (7) | `[x]` |
| Application scoping helper refuses non-tenant models | `TestApplicationScopingHelper` (2) | `[x]` |
| Every endpoint requires authentication | `TestAuthorizationSurface::test_every_endpoint_requires_authentication` (8 params) | `[x]` |
| Role-based denial at the HTTP layer | `TestAuthorizationSurface` (2) | `[x]` |
| CSRF on state-changing requests | `TestSessionSecurity` (2) | `[x]` |
| Cookie flags, forged cookie, session invalidation on membership removal | `TestSessionSecurity` (3) | `[x]` |
| Account enumeration (identical failure responses) | `TestEnumeration` (1) | `[x]` |
| RLS backstop under PostgreSQL, incl. the superuser-bypass regression guard | `TestPostgresGuarantees` (4) | `[x]` — passing against a live PostgreSQL 16 container |
| Prompt injection corpus | — | `[ ]` — nothing to inject into yet |

---

## Open decisions

| ID | Decision | Needed by | Status |
|---|---|---|---|
| D-01 | First jurisdiction + category (proposed: FSSAI packaged food, India) | P4-T5 | OPEN |
| D-02 | Object storage vendor (R2 vs B2) | P2-T2 | OPEN — deferred without cost: the adapter (`app/storage/client.py`) speaks plain S3 through boto3 and is proven against MinIO; switching to R2 or B2 in production is a config change (`storage_endpoint_url`, credentials), not a code change |
| D-03 | Cloud OCR fallback vendor (Google Vision vs Azure Read) | P3-T3 | OPEN — actively blocking: P3-T3 cannot start without this **and** real cloud credentials (a GCP or Azure account with billing), neither available in this environment |
| D-04 | Whether a local LLM fallback is in scope at all | P3-T5 | OPEN — P3-T5 (LLM extraction) additionally needs a live LLM provider credential (e.g. `ANTHROPIC_API_KEY`) not available in this environment, independent of this decision |
| D-05 | Hosting target (single VPS vs Fly/Render) | P7-T7 | OPEN |

---

## Next actions

1. ~~Start Docker and run the full stack, then `pytest -m postgres`, to close P0-T2, P1-T4, P1-T5, X-03, X-05.~~ Done 2026-08-29 — see evidence above, including the superuser-RLS-bypass fix.
2. Push this repository to a remote so the CI workflow actually executes as a real GitHub Actions run (P0-T5 — its steps are reproduced locally with matching results, but never yet run by Actions itself).
3. Add the import-lint rule and an automated "new tenant table must have an RLS policy" guard rail (X-01, X-03).
4. ~~P2-T2 (object storage adapter + presigned uploads).~~ Done 2026-08-29 — see evidence above, verified against a live MinIO container.
5. ~~P2-T3 (upload completion — `files` table, magic-byte/size validation, ClamAV, dedup by sha256).~~ Done 2026-08-29 — see evidence above, verified against live MinIO and ClamAV containers.
6. ~~P2-T4 (rasterization and page normalization).~~ Done 2026-08-30 — see evidence above, verified against a live MinIO container. Phase 2 is now fully implemented; the one open item (P2-T1's analysis-locking acceptance criterion) is correctly left `[ ]` pending P5-T1 rather than forced early.
7. ~~P3-T1 (preprocessing with invertible coordinate transforms).~~ Done 2026-08-30 — see evidence above; pure unit-tested library code, no live services needed.
8. ~~P3-T2 (OCR adapter interface + PaddleOCR implementation).~~ Done 2026-08-30 — see evidence above, verified against a **real** PaddleOCR engine in a second Python 3.12 virtualenv (`.venv312`) created specifically because PaddlePaddle has no Python 3.14 wheel.
9. P3-T3 (cloud OCR fallback) is **blocked** on D-03 (vendor choice) plus real cloud credentials this session doesn't have — skipped in dependency order since nothing else in Phase 3 requires it (see its row).
10. ~~P3-T4 (fact schema).~~ Done 2026-08-30 — see evidence above; pure Pydantic, no live services needed. This is the task that unblocks Phase 4 once frozen, though Phase 4 itself stays on hold for D-01 per standing instructions.
11. P3-T5 (LLM extraction) is **blocked** on a real LLM provider credential (`ANTHROPIC_API_KEY`) this session doesn't have, same class of blocker as P3-T3 — skipped in dependency order. P3-T6 sits downstream of P3-T5 and is blocked transitively.
12. ~~P3-T7 (normalization library).~~ Done 2026-08-30 — see evidence above; pure Python, no live services or external credentials needed.
13. Next: P3-T9 (category & jurisdiction classification) — unblocked now that P3-T7 is done. P3-T8 (confidence model and tier routing) still needs P3-T6, which stays blocked behind P3-T5's `ANTHROPIC_API_KEY` requirement.
