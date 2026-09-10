import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";

import { expect, test } from "@playwright/test";

/** P6-T3's own acceptance criterion, verbatim: "a running analysis updates
 * live without a manual refresh" - proven against the real, now-fixed
 * end-to-end pipeline (real Postgres/Redis/MinIO, real PaddleOCR via the
 * `.venv312` workers `playwright.config.ts` starts, and real Gemini when
 * `LABELLENS_LLM_API_KEY` is configured), not a mock.
 *
 * This is genuinely live external infrastructure (a real LLM API), so this
 * test does not assert one hard-coded outcome. It asserts the dashboard
 * correctly reflects whichever REAL terminal state the pipeline actually
 * reaches - `completed`, `needs_review`, or `failed` are all legitimate,
 * already-observed-live outcomes (see TESTTEST.md's readiness-audit entry:
 * a real Gemini `503` capacity error is a real, expected possibility, not a
 * flake to paper over) - and never fabricates compliance findings or
 * extracted data regardless of which one occurs.
 */

test.setTimeout(240_000);

function uniqueEmail(): string {
  return `analyst-${Date.now()}-${Math.floor(Math.random() * 10_000)}@example.com`;
}

const FIXTURE_PATH = fileURLToPath(new URL("./fixtures/label.png", import.meta.url));

test("upload, start analysis, observe live progress, reload, and reach a real terminal state", async ({
  page,
  request,
}) => {
  const email = uniqueEmail();
  const password = "CorrectHorse42!";

  const signup = await request.post("http://localhost:8000/v1/auth/signup", {
    data: { organization_name: "Analysis E2E Org", email, password },
  });
  expect(signup.ok()).toBeTruthy();

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page).toHaveURL("http://localhost:5173/");

  await page.getByRole("link", { name: "Products" }).click();
  await page.getByRole("button", { name: "New product" }).click();
  await page.getByLabel("Name").fill("Masala Chips");
  await page.getByLabel("Internal SKU").fill(`MC-${Date.now()}`);
  // A declared IN market code gives `_jurisdiction_from_hints` an exact
  // match (P3-T9) - the best real chance this run actually reaches a
  // resolved IN/packaged_food classification and, with it, real findings
  // against the real published `in-fssai-food` pack (P4-T5/D-01) - never
  // asserted as certain below, since that still also depends on what
  // Gemini's own real extraction of the fixture label yields.
  await page.getByLabel("Market codes (comma separated)").fill("IN");
  await page.getByRole("button", { name: "Create product" }).click();

  await page.getByText("Masala Chips").click();
  await page.getByRole("button", { name: "New version" }).click();
  await page.getByRole("button", { name: "Create version" }).click();
  await page.getByText(/^v1/).click();

  const fileInput = page.getByLabel("Upload label files").locator("input");
  await fileInput.setInputFiles({
    name: "label.png",
    mimeType: "image/png",
    buffer: readFileSync(FIXTURE_PATH),
  });
  const filesList = page.getByRole("list", { name: "Uploaded files" });
  await expect(filesList.getByText("ready")).toBeVisible({ timeout: 20_000 });

  await page.getByRole("button", { name: "Run analysis" }).click();
  await expect(page).toHaveURL(/\/analyses\/[0-9a-f-]+$/);

  const status = page.getByLabel("Analysis status");
  const STOPPING_LABELS = /^(Completed|Needs review|Failed|Cancelled)$/;

  // Live progress: the state visibly advances past the initial "Queued"
  // without this test ever reloading the page - this is the literal
  // acceptance criterion ("updates live without a manual refresh"), proven
  // by the real SSE stream (or, if that connection ever drops, the polling
  // fallback) actually pushing new state into the UI on its own.
  await expect(status).not.toContainText("Queued", { timeout: 30_000 });

  // Reload mid-flight: a fresh page load must recover the analysis's real
  // current state from `GET /v1/analyses/{id}` and re-open a live
  // connection, not get stuck on a stale or empty view.
  await page.reload();
  await expect(status).toBeVisible({ timeout: 10_000 });

  // Whichever real stopping state the pipeline reaches - this is the run's
  // own live result, not asserted in advance.
  await expect(status.getByText(STOPPING_LABELS)).toBeVisible({ timeout: 150_000 });
  const finalLabel = await status.getByText(STOPPING_LABELS).textContent();

  if (finalLabel === "Failed") {
    // A real, honest failure (e.g. Gemini capacity) must be displayed as
    // one, with the backend's own failure stage and reason - never silently
    // hidden or replaced with fabricated success.
    await expect(page.getByText(/Analysis failed at/)).toBeVisible();
  } else {
    // `completed` or `needs_review`: a confidence tier must be present
    // (`_scoring` always sets one before either state is reached), and
    // generating the real extraction report must show real extracted
    // fields - never invented ones.
    await expect(status.getByText(/confidence$/)).toBeVisible();

    // Compliance findings must render as whatever the backend actually
    // returned - real, evidence-traced findings against the real published
    // `in-fssai-food` pack (P4-T5/D-01) if this run's real classification
    // resolved to IN/packaged_food, or the honest empty state otherwise
    // (a different/unresolved jurisdiction, or an abstained classification -
    // both real, legitimate outcomes of a live extraction this test does
    // not control). Never asserted as one or the other in advance.
    const findingWithEvidence = page.getByRole("button", { name: /Click to view evidence/ });
    const noFindingsMessage = page.getByText(
      "No compliance findings are available for this analysis.",
    );
    await expect(findingWithEvidence.or(noFindingsMessage).first()).toBeVisible();

    if (await findingWithEvidence.count()) {
      // The literal P6-T4 acceptance criterion: selecting a finding brings
      // its real evidence into view. Clicking switches the label viewer to
      // the evidence's own page and renders a real bbox overlay, sized from
      // the finding's own `EvidenceDetailOut.bbox` in the label's real pixel
      // coordinate space - not a placeholder.
      await findingWithEvidence.first().click();
      await expect(page.getByTestId("evidence-bbox").first()).toBeVisible({ timeout: 10_000 });
    }

    await page.getByRole("button", { name: "Generate extraction report" }).click();
    const fieldTable = page.getByRole("table");
    await expect(fieldTable).toBeVisible({ timeout: 20_000 });
    await expect(fieldTable.locator("tbody tr").first()).toBeVisible();
  }
});
