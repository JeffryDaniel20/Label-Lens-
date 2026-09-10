import { expect, test } from "@playwright/test";

/** P6-T1's own acceptance criterion, verbatim: "unauthenticated access
 * redirects; role-gated nav reflects capabilities," proven with a real
 * browser against a real (freshly migrated) backend - not mocked. */

function uniqueEmail(): string {
  return `owner-${Date.now()}-${Math.floor(Math.random() * 10_000)}@example.com`;
}

test("unauthenticated visitors are redirected to /login", async ({ page }) => {
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});

test("a real signup, login, session-reload, and logout all work end to end", async ({
  page,
  request,
}) => {
  const email = uniqueEmail();
  const password = "CorrectHorse42!";

  // Seed the org+owner directly against the real backend the same way a
  // signup form eventually will (P6-T2+); this test's own job is the
  // frontend's session lifecycle, not re-proving signup validation the
  // backend's own suite already covers exhaustively.
  const signup = await request.post("http://localhost:8000/v1/auth/signup", {
    data: { organization_name: "E2E Org", email, password },
  });
  expect(signup.ok()).toBeTruthy();

  await page.goto("/login");
  await page.getByLabel("Email").fill(email);
  await page.getByLabel("Password").fill(password);
  await page.getByRole("button", { name: "Sign in" }).click();

  await expect(page).toHaveURL("http://localhost:5173/");
  await expect(page.getByText("You are signed in as")).toBeVisible();
  await expect(page.getByRole("link", { name: "Members" })).toBeVisible();

  // A reload has only the session cookie, not the CSRF token React state
  // held in memory - `GET /v1/me` must hand it back (see
  // `app.identity.schemas.MeResponse.csrf_token`) so the session survives.
  await page.reload();
  await expect(page.getByText("You are signed in as")).toBeVisible();

  await page.getByRole("button", { name: "Sign out" }).click();
  await expect(page).toHaveURL(/\/login$/);

  // And a signed-out session really can't reach a protected route anymore.
  await page.goto("/");
  await expect(page).toHaveURL(/\/login$/);
});
