# LabelLens frontend

React 18 + TypeScript + Vite + TanStack Query + Tailwind CSS. See
[IMPLEMENTATION.md](../IMPLEMENTATION.md) for the architecture and
[docs/runbooks/local-development.md](../docs/runbooks/local-development.md) for the full dev
workflow.

## Scripts

| Command                           | What it does                                                                                                                                 |
| --------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------- |
| `npm run dev`                     | Vite dev server on `:5173`, proxying `/v1/*` to a backend on `:8000`.                                                                        |
| `npm run build`                   | Type-checks (`tsc -b`) then produces a production bundle in `dist/`.                                                                         |
| `npm run test`                    | Vitest component tests (jsdom).                                                                                                              |
| `npm run e2e`                     | Playwright, a real browser against a real (freshly migrated) backend.                                                                        |
| `npm run lint` / `npm run format` | ESLint / Prettier check.                                                                                                                     |
| `npm run typecheck`               | `tsc -b --noEmit`.                                                                                                                           |
| `npm run generate:api-types`      | Regenerates `src/api/schema.ts` from the backend's own OpenAPI schema - re-run whenever a backend endpoint's request/response shape changes. |

## Structure

```
src/
├─ api/        # generated OpenAPI types (schema.ts) + the fetch wrapper (client.ts)
├─ features/   # feature-scoped hooks (auth session, catalog: products/versions/uploads, ...)
├─ routes/     # route components + the app shell (RootLayout, ProtectedRoute)
├─ components/ # shared UI primitives (e.g. UploadDropzone)
├─ lib/        # cross-cutting utilities (toast)
└─ styles/     # Tailwind entry point
```

## Catalog and uploads (P6-T2)

Products, label versions, and file uploads live in `src/features/catalog/` and
`src/routes/{Products,ProductDetail,VersionDetail}Page.tsx`. Uploading a label file is a
three-step flow driven entirely by the real backend, never a second, invented upload mechanism:
`POST /v1/product-versions/{id}/uploads` mints a presigned PUT URL, the browser `PUT`s the raw
bytes straight to object storage (never through this app's own backend), then
`POST /v1/product-versions/{id}/files` tells the backend to validate and persist it (magic-byte
sniff, size limits, AV scan, dedup). `useFileUploads` (`src/features/catalog/uploads.ts`) drives
all three steps per file with `XMLHttpRequest` (for real upload-progress events) and tracks
per-file status for the drag-drop UI. Running `npm run e2e`'s catalog suite locally needs real
MinIO up (`docker compose -f ../infra/docker-compose.yml up -d minio minio-init`) - see
[docs/runbooks/local-development.md](../docs/runbooks/local-development.md).

## Auth model

The backend authenticates via an httpOnly session cookie - it is never readable from JS. The one
piece of session state the frontend does hold in memory is the CSRF token every mutating request
must send back as `X-CSRF-Token`: `login`/`signup` return it once, and `GET /v1/me` returns it again
on every call so a page reload can recover it without forcing a re-login (see
`app.identity.schemas.MeResponse.csrf_token` on the backend). Never persist it to `localStorage` -
that would hand any successful XSS a standing way to forge requests indefinitely instead of only
for the current tab's lifetime.
