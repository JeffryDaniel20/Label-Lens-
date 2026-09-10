import { expect, test } from "@playwright/test";

/** P6-T2's own acceptance criterion, verbatim: "a user uploads a multi-file
 * label version and sees per-file status," proven with a real browser
 * against a real backend and real MinIO object storage (`docker compose up
 * minio minio-init`) - not mocked. The presigned PUT this exercises goes
 * straight from the browser to MinIO, exactly as it will in production. */

function uniqueEmail(): string {
  return `analyst-${Date.now()}-${Math.floor(Math.random() * 10_000)}@example.com`;
}

// A minimal, structurally valid single-pixel PNG - large enough to exercise
// the real upload/validation pipeline (magic-byte sniff, size checks) rather
// than a placeholder that would just get rejected as unrecognized content.
const PNG_BASE64 =
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=";

// A real, structurally valid single-page PDF (`pypdf.PdfWriter().add_blank_page(200, 200)`,
// matching `backend/tests/integration/test_ingestion_upload_completion.py`'s own fixture) - the
// backend's `validate_pdf` actually parses PDF structure (pypdf) and rasterizes it (pypdfium2), so
// a hand-rolled placeholder byte string would just fail validation rather than exercise the path.
const PDF_BASE64 =
  "JVBERi0xLjMKJeLjz9MKMSAwIG9iago8PAovUHJvZHVjZXIgKHB5cGRmKQo+PgplbmRvYmoKMiAwIG9iago8PAovVHlwZSAvUGFnZXMKL0NvdW50IDEKL0tpZHMgWyA0IDAgUiBdCj4+CmVuZG9iagozIDAgb2JqCjw8Ci9UeXBlIC9DYXRhbG9nCi9QYWdlcyAyIDAgUgo+PgplbmRvYmoKNCAwIG9iago8PAovVHlwZSAvUGFnZQovUmVzb3VyY2VzIDw8Cj4+Ci9NZWRpYUJveCBbIDAuMCAwLjAgMjAwIDIwMCBdCi9QYXJlbnQgMiAwIFIKPj4KZW5kb2JqCnhyZWYKMCA1CjAwMDAwMDAwMDAgNjU1MzUgZiAKMDAwMDAwMDAxNSAwMDAwMCBuIAowMDAwMDAwMDU0IDAwMDAwIG4gCjAwMDAwMDAxMTMgMDAwMDAgbiAKMDAwMDAwMDE2MiAwMDAwMCBuIAp0cmFpbGVyCjw8Ci9TaXplIDUKL1Jvb3QgMyAwIFIKL0luZm8gMSAwIFIKPj4Kc3RhcnR4cmVmCjI1NgolJUVPRgo=";

test("login, create product, create version, and upload a label file", async ({
  page,
  request,
}) => {
  const email = uniqueEmail();
  const password = "CorrectHorse42!";

  const signup = await request.post("http://localhost:8000/v1/auth/signup", {
    data: { organization_name: "Catalog E2E Org", email, password },
  });
  expect(signup.ok()).toBeTruthy();

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("http://localhost:5173/");

  await page.getByRole("link", { name: "Products" }).click();
  await expect(page).toHaveURL(/\/products$/);

  await page.getByRole("button", { name: "New product" }).click();
  await page.getByLabel("Name").fill("Masala Chips");
  await page.getByLabel("Internal SKU").fill(`MC-${Date.now()}`);
  await page.getByLabel("Market codes (comma separated)").fill("IN");
  await page.getByRole("button", { name: "Create product" }).click();

  await page.getByText("Masala Chips").click();
  await expect(page.getByRole("heading", { name: "Masala Chips" })).toBeVisible();

  await page.getByRole("button", { name: "New version" }).click();
  await page.getByLabel("Label (optional)").fill("Diwali 2026 relaunch");
  await page.getByRole("button", { name: "Create version" }).click();

  await page.getByText("v1 — Diwali 2026 relaunch").click();
  await expect(page.getByRole("heading", { name: /v1 — Diwali 2026 relaunch/ })).toBeVisible();

  const fileInput = page.getByLabel("Upload label files").locator("input");
  await fileInput.setInputFiles({
    name: "front-label.png",
    mimeType: "image/png",
    buffer: Buffer.from(PNG_BASE64, "base64"),
  });

  // The upload task goes uploading -> finalizing -> done as it moves through
  // the real presigned PUT to MinIO and then the backend's own completion
  // (validation/AV/rasterization) endpoint.
  await expect(page.getByText("Done")).toBeVisible({ timeout: 15_000 });

  // And the file now shows up in the version's persisted file list (scoped
  // separately from the upload-progress list above, which still shows the
  // same filename), proving the whole round trip - not just the in-memory
  // task state - succeeded.
  const filesList = page.getByRole("list", { name: "Uploaded files" });
  await expect(filesList.getByText("front-label.png")).toBeVisible();
  await expect(filesList.getByText(/image\/png/)).toBeVisible();
  await expect(filesList.getByText("ready")).toBeVisible();

  // The acceptance criterion is multi-file - upload a PDF too (backend's
  // own P2-T3 pipeline treats images and PDFs very differently: PDFs are
  // structurally parsed with pypdf and rasterized per-page with pypdfium2,
  // so this exercises a genuinely distinct code path from the PNG above).
  await fileInput.setInputFiles({
    name: "ingredients.pdf",
    mimeType: "application/pdf",
    buffer: Buffer.from(PDF_BASE64, "base64"),
  });
  await expect(filesList.getByText("ingredients.pdf")).toBeVisible({ timeout: 15_000 });
  await expect(filesList.getByText(/application\/pdf/)).toBeVisible();
  await expect(filesList.getByText(/1 page/)).toBeVisible();

  // A reload re-fetches the file list from the backend, proving both
  // uploads were actually persisted rather than only reflected in local
  // task state.
  await page.reload();
  await expect(filesList.getByText("front-label.png")).toBeVisible();
  await expect(filesList.getByText("ready")).toHaveCount(2);
  await expect(filesList.getByText("ingredients.pdf")).toBeVisible();
});

test("a disallowed file type is rejected with the backend's own error message", async ({
  page,
  request,
}) => {
  const email = uniqueEmail();
  const password = "CorrectHorse42!";

  const signup = await request.post("http://localhost:8000/v1/auth/signup", {
    data: { organization_name: "Catalog Reject E2E Org", email, password },
  });
  expect(signup.ok()).toBeTruthy();

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("http://localhost:5173/");

  await page.getByRole("link", { name: "Products" }).click();
  await page.getByRole("button", { name: "New product" }).click();
  await page.getByLabel("Name").fill("Rejected Product");
  await page.getByLabel("Internal SKU").fill(`RJ-${Date.now()}`);
  await page.getByRole("button", { name: "Create product" }).click();

  await page.getByText("Rejected Product").click();
  await page.getByRole("button", { name: "New version" }).click();
  await page.getByRole("button", { name: "Create version" }).click();

  await page.getByText(/^v1/).click();

  const fileInput = page.getByLabel("Upload label files").locator("input");
  await fileInput.setInputFiles({
    name: "malware.exe",
    mimeType: "application/octet-stream",
    buffer: Buffer.from("not a real executable"),
  });

  await expect(page.getByText(/Allowed types/)).toBeVisible({ timeout: 15_000 });
});
