import { test, expect } from "@playwright/test";

test("landing loads with hero", async ({ page }) => {
  await page.goto("/");
  // Landing headline is the primary hero copy on app/page.tsx.
  // Update this string when the marketing copy changes there.
  await expect(page.getByRole("heading", { level: 1 })).toContainText(
    "Steady when everything else isn"
  );
  await expect(
    page.getByRole("button", { name: /Get Started/i })
  ).toBeVisible();
});

test("today page renders empty state when not connected", async ({ page }) => {
  await page.goto("/today");
  await expect(page.getByRole("heading", { name: /Connect Google/i })).toBeVisible();
});

test("about page mentions hackathon", async ({ page }) => {
  await page.goto("/about");
  await expect(page.getByText(/All Things Agentic/)).toBeVisible();
  await expect(page.getByRole("heading", { name: /About Level/i })).toBeVisible();
});
