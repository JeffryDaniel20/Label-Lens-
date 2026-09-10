import { defineConfig, devices } from "@playwright/test";

/** Real browser E2E for P6-T1 ("component + E2E login/logout", see
 * `e2e/auth.spec.ts`), P6-T2 ("a user uploads a multi-file label version and
 * sees per-file status", see `e2e/catalog.spec.ts`), and P6-T3 ("a running
 * analysis updates live without a manual refresh", see
 * `e2e/analysis.spec.ts`). The Vite dev server and a real backend (migrated
 * to head) are started fresh for the run - P6-T3's analysis pipeline needs
 * more than a disposable SQLite file can give it (RLS tenant isolation, a
 * real Arq queue), so this now runs against real Postgres/Redis/MinIO
 * instead. Before `npm run e2e`, bring those up:
 *   docker compose -f ../infra/docker-compose.yml up -d postgres redis minio minio-init
 * `analysis.spec.ts` additionally needs a real `LABELLENS_LLM_API_KEY` (see
 * `backend/.env`) for its Gemini extraction call to succeed - without one,
 * `extracting` fails immediately with `ProviderNotConfigured`, which the
 * test tolerates as one of several honest real outcomes (see that spec's
 * own comments) but a real key exercises the actual live pipeline. */
export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  retries: 0,
  reporter: "list",
  use: {
    baseURL: "http://localhost:5173",
    trace: "retain-on-failure",
  },
  webServer: [
    {
      command:
        "..\\backend\\.venv\\Scripts\\python.exe -m alembic upgrade head && " +
        "..\\backend\\.venv\\Scripts\\python.exe -m uvicorn app.main:create_app --factory --port 8000",
      // If `.venv` fails to import `app.main` with a native-DLL
      // "Application Control policy" error, that's a machine-level
      // Windows security policy blocking a specific venv's copy of
      // `pillow_heif`'s compiled extension, not a code or dependency
      // problem - see docs/runbooks/local-development.md's "Common
      // problems" section (found live, P6-T1, 2026-09-09). Every session
      // since has actually verified this suite with `.venv312` substituted
      // for `.venv` here as a workaround; switch back to `.venv312` for
      // local verification until that policy block is resolved on this
      // machine. `.venv312` is also the only option for the two worker
      // entries below, since it's the one environment on this machine with
      // a working PaddleOCR install (see `app/vision/ocr/paddle.py`).
      cwd: "../backend",
      url: "http://localhost:8000/healthz",
      env: {
        LABELLENS_ENVIRONMENT: "local",
        LABELLENS_SECRET_KEY: "e2e-test-secret-key-that-is-long-enough-123456",
        LABELLENS_DATABASE_URL: "postgresql+psycopg://labellens:labellens@localhost:5432/labellens",
        LABELLENS_COOKIE_SECURE: "false",
        LABELLENS_REDIS_URL: "redis://localhost:6379/0",
        // Real MinIO (via `docker compose up minio minio-init`), reachable
        // from both this backend process and the browser's own direct PUT
        // to the presigned URL - see P6-T2's upload flow.
        LABELLENS_STORAGE_ENDPOINT_URL: "http://localhost:9000",
        LABELLENS_STORAGE_BUCKET: "labellens-uploads",
        LABELLENS_STORAGE_ACCESS_KEY: "labellens",
        LABELLENS_STORAGE_SECRET_KEY: "labellens-dev-secret",
        // Real Gemini (D-06) - see `backend/.env`. `process.env` is already
        // merged in by Playwright itself, so this only needs to name the
        // vars, not hard-code them; empty/missing just means `extracting`
        // fails fast with `ProviderNotConfigured`, one of the honest
        // outcomes `analysis.spec.ts` itself tolerates.
        LABELLENS_LLM_PROVIDER: process.env.LABELLENS_LLM_PROVIDER ?? "gemini",
        LABELLENS_LLM_API_KEY: process.env.LABELLENS_LLM_API_KEY ?? "",
        LABELLENS_LLM_MODEL: process.env.LABELLENS_LLM_MODEL ?? "gemini-3.8-flash",
        LABELLENS_LLM_ESCALATION_MODEL:
          process.env.LABELLENS_LLM_ESCALATION_MODEL ?? "gemini-3.8-flash",
      },
      reuseExistingServer: false,
      timeout: 60_000,
    },
    // Nothing consumes a queued analysis without these - see
    // `app/analysis/worker.py`'s own module docstring and
    // `infra/docker-compose.yml`'s `worker-*` services, which this mirrors
    // for a host-run (non-containerized) backend so the browser's presigned
    // MinIO PUT can use `localhost` throughout instead of a container-only
    // hostname. `LABELLENS_WORKER_QUEUE` is what makes a single `arq
    // app.analysis.worker.WorkerSettings` invocation watch anything other
    // than `default` - no separate script/module needed per queue.
    {
      command:
        "..\\backend\\.venv312\\Scripts\\python.exe -m arq app.analysis.worker.WorkerSettings",
      cwd: "../backend",
      env: {
        LABELLENS_SECRET_KEY: "e2e-test-secret-key-that-is-long-enough-123456",
        LABELLENS_DATABASE_URL: "postgresql+psycopg://labellens:labellens@localhost:5432/labellens",
        LABELLENS_REDIS_URL: "redis://localhost:6379/0",
        LABELLENS_STORAGE_ENDPOINT_URL: "http://localhost:9000",
        LABELLENS_STORAGE_BUCKET: "labellens-uploads",
        LABELLENS_STORAGE_ACCESS_KEY: "labellens",
        LABELLENS_STORAGE_SECRET_KEY: "labellens-dev-secret",
        LABELLENS_LLM_PROVIDER: process.env.LABELLENS_LLM_PROVIDER ?? "gemini",
        LABELLENS_LLM_API_KEY: process.env.LABELLENS_LLM_API_KEY ?? "",
        LABELLENS_LLM_MODEL: process.env.LABELLENS_LLM_MODEL ?? "gemini-3.8-flash",
        LABELLENS_LLM_ESCALATION_MODEL:
          process.env.LABELLENS_LLM_ESCALATION_MODEL ?? "gemini-3.8-flash",
        LABELLENS_WORKER_QUEUE: "default",
      },
      reuseExistingServer: false,
    },
    {
      command:
        "..\\backend\\.venv312\\Scripts\\python.exe -m arq app.analysis.worker.WorkerSettings",
      cwd: "../backend",
      env: {
        LABELLENS_SECRET_KEY: "e2e-test-secret-key-that-is-long-enough-123456",
        LABELLENS_DATABASE_URL: "postgresql+psycopg://labellens:labellens@localhost:5432/labellens",
        LABELLENS_REDIS_URL: "redis://localhost:6379/0",
        LABELLENS_STORAGE_ENDPOINT_URL: "http://localhost:9000",
        LABELLENS_STORAGE_BUCKET: "labellens-uploads",
        LABELLENS_STORAGE_ACCESS_KEY: "labellens",
        LABELLENS_STORAGE_SECRET_KEY: "labellens-dev-secret",
        LABELLENS_LLM_PROVIDER: process.env.LABELLENS_LLM_PROVIDER ?? "gemini",
        LABELLENS_LLM_API_KEY: process.env.LABELLENS_LLM_API_KEY ?? "",
        LABELLENS_LLM_MODEL: process.env.LABELLENS_LLM_MODEL ?? "gemini-3.8-flash",
        LABELLENS_LLM_ESCALATION_MODEL:
          process.env.LABELLENS_LLM_ESCALATION_MODEL ?? "gemini-3.8-flash",
        LABELLENS_WORKER_QUEUE: "ocr",
      },
      reuseExistingServer: false,
    },
    {
      command:
        "..\\backend\\.venv312\\Scripts\\python.exe -m arq app.analysis.worker.WorkerSettings",
      cwd: "../backend",
      env: {
        LABELLENS_SECRET_KEY: "e2e-test-secret-key-that-is-long-enough-123456",
        LABELLENS_DATABASE_URL: "postgresql+psycopg://labellens:labellens@localhost:5432/labellens",
        LABELLENS_REDIS_URL: "redis://localhost:6379/0",
        LABELLENS_STORAGE_ENDPOINT_URL: "http://localhost:9000",
        LABELLENS_STORAGE_BUCKET: "labellens-uploads",
        LABELLENS_STORAGE_ACCESS_KEY: "labellens",
        LABELLENS_STORAGE_SECRET_KEY: "labellens-dev-secret",
        LABELLENS_LLM_PROVIDER: process.env.LABELLENS_LLM_PROVIDER ?? "gemini",
        LABELLENS_LLM_API_KEY: process.env.LABELLENS_LLM_API_KEY ?? "",
        LABELLENS_LLM_MODEL: process.env.LABELLENS_LLM_MODEL ?? "gemini-3.8-flash",
        LABELLENS_LLM_ESCALATION_MODEL:
          process.env.LABELLENS_LLM_ESCALATION_MODEL ?? "gemini-3.8-flash",
        LABELLENS_WORKER_QUEUE: "llm",
      },
      reuseExistingServer: false,
    },
    {
      command: "npm run dev",
      url: "http://localhost:5173",
      reuseExistingServer: false,
      timeout: 30_000,
    },
  ],
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
});
