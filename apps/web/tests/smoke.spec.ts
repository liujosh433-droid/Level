import { test, expect } from "@playwright/test";

test("landing loads with hero", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { level: 1 })).toContainText("calm second set of hands");
  await expect(page.getByRole("link", { name: "Open the dashboard" })).toBeVisible();
});

test("today page renders empty state when not connected", async ({ page }) => {
  await page.goto("/today");
  await expect(page.getByRole("heading", { name: /Connect Google/i })).toBeVisible();
});

test("about page mentions hackathon", async ({ page }) => {
  await page.goto("/about");
  await expect(page.getByText(/All Things Agentic/)).toBeVisible();
});
