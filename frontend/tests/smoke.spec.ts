import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";

const REPO_URL = "https://github.com/acme/shopkit";
const REPO_ID = "acme__shopkit";

// Serial: each test builds on the repo created by the first one.
test.describe.configure({ mode: "serial" });

test("add a repo and watch it become ready", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
  await page.getByLabel("GitHub URL").fill(REPO_URL);
  await page.getByRole("button", { name: "Add repository" }).click();
  const card = page.getByRole("list", { name: "Indexed repositories" }).getByRole("listitem").filter({ hasText: "acme/shopkit" });
  await expect(card).toBeVisible();
  await expect(card.getByText("ready", { exact: true })).toBeVisible({ timeout: 60_000 });
  await expect(card.getByRole("link", { name: "Ask questions" })).toBeVisible();
});

test("invalid URL shows an error", async ({ page }) => {
  await page.goto("/");
  await page.getByLabel("GitHub URL").fill("file:///etc/passwd");
  await page.getByRole("button", { name: "Add repository" }).click();
  await expect(page.getByTestId("error")).toContainText("github.com");
});

test("ask a question, see live steps, click a citation, see the exact lines", async ({ page }) => {
  await page.goto(`/repos/${REPO_ID}`);
  await page.getByRole("textbox", { name: "Question" }).fill("Where are orders validated?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const run = page.getByTestId("run-indexed_agent");
  await expect(run.getByTestId("agent-steps")).toContainText("Searching code: Where are orders validated?");
  await expect(run.getByTestId("answer")).toContainText("MOCK ANSWER", { timeout: 60_000 });
  await expect(run.getByTestId("verification-badge")).toContainText("verified");

  const chip = run.getByTestId("citation-chip").first();
  const label = (await chip.textContent())!.trim();
  const m = /^(.+):(\d+)(?:-(\d+))?$/.exec(label)!;
  const [path, start, end] = [m[1], Number(m[2]), Number(m[3] ?? m[2])];
  await chip.click();

  const viewer = page.getByTestId("code-viewer");
  await expect(viewer.getByTestId("viewer-path")).toHaveText(path);
  await expect(viewer.getByTestId("viewer-range")).toContainText(`lines ${start}–${end}`);
  const highlighted = viewer.locator(".line.hl");
  await expect(highlighted.first()).toBeVisible();
  const numbers = await highlighted.evaluateAll((els) => els.map((e) => Number(e.getAttribute("data-line"))));
  expect(numbers[0]).toBe(start);
  expect(numbers[numbers.length - 1]).toBe(end);
  expect(numbers).toHaveLength(end - start + 1);
  // The highlighted text is really that file's content.
  const shown = await page.request.get(`http://127.0.0.1:8001/repos/${REPO_ID}/file`, { params: { path, start, end } });
  const expected = (await shown.json()).lines as string[];
  expect((await highlighted.allTextContents()).map((t) => t.replace(/\s+$/, ""))).toEqual(expected.map((t) => t.replace(/\s+$/, "")));
  await page.keyboard.press("Escape");
  await expect(viewer).toBeHidden();
});

test("the answer streams in progressively, then the verified answer replaces it", async ({ page }) => {
  await page.goto(`/repos/${REPO_ID}`);
  await page.getByRole("textbox", { name: "Question" }).fill("Where are orders validated?");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const run = page.getByTestId("run-indexed_agent");
  const draft = run.getByTestId("draft");
  await expect(draft).toBeVisible({ timeout: 60_000 });
  // While streaming there is no verified answer or badge yet.
  await expect(run.getByTestId("verification-badge")).toHaveCount(0);
  const early = ((await draft.textContent()) ?? "").length;
  await expect.poll(async () => ((await draft.textContent().catch(() => null)) ?? "").length, { timeout: 10_000 }).toBeGreaterThan(early);
  // Then the final answer (with citations verified) replaces the draft.
  await expect(run.getByTestId("answer")).toContainText("MOCK ANSWER", { timeout: 30_000 });
  await expect(run.getByTestId("verification-badge")).toBeVisible();
  await expect(draft).toHaveCount(0);
});

test("compare view shows every system with usage", async ({ page }) => {
  await page.goto(`/repos/${REPO_ID}`);
  await page.getByRole("textbox", { name: "Question" }).fill("What does the checkout do?");
  await page.getByLabel("Compare all systems side by side").check();
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  for (const system of ["indexed_agent", "grep_agent", "naive_rag"]) {
    const run = page.getByTestId(`run-${system}`);
    await expect(run.getByTestId("answer")).toBeVisible({ timeout: 60_000 });
    await expect(run.getByTestId("usage")).toContainText("tokens");
  }
});

test("answers never render model-supplied HTML or javascript: links", async ({ page }) => {
  await page.route("**/repos/*/ask", (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body:
        'event: result\ndata: ' +
        JSON.stringify({
          type: "result",
          system: "indexed_agent",
          answer: 'Hello <script>window.__pwned = 1</script><img src=x onerror="window.__pwned=1"> [click](javascript:window.__pwned=1) [ok](https://example.com) [shop/api.py:1-3]',
          citations: [],
          verification: { ok: true, citations_total: 1 },
          usage: { input_tokens: 1, output_tokens: 1, total_tokens: 2, cost_usd: 0, llm_calls: 1, tool_calls: 0, latency_s: 0.1, models: [] },
          retrieved_files: [],
          budget_exhausted: false,
          error: null,
        }) +
        '\n\nevent: done\ndata: {"type":"done"}\n\n',
    }),
  );
  await page.goto(`/repos/${REPO_ID}`);
  await page.getByRole("textbox", { name: "Question" }).fill("xss probe");
  await page.getByRole("button", { name: "Ask", exact: true }).click();
  const answer = page.getByTestId("answer");
  await expect(answer).toContainText("Hello");
  expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
  expect(await answer.locator("script, img").count()).toBe(0);
  expect(await answer.locator('a[href^="javascript:"]').count()).toBe(0);
  await expect(answer.locator('a[href="https://example.com"]')).toHaveAttribute("rel", /noopener/);
});

test("tour page renders with working citations", async ({ page }) => {
  await page.goto(`/repos/${REPO_ID}/tour`);
  await expect(page.getByRole("heading", { name: "Onboarding tour" })).toBeVisible();
  await expect(page.getByTestId("reading-order")).toBeVisible({ timeout: 90_000 });
  await expect(page.getByRole("heading", { name: "Entry points" })).toBeVisible();
  await page.getByTestId("citation-chip").first().click();
  await expect(page.getByTestId("code-viewer")).toBeVisible();
  await expect(page.locator(".line.hl").first()).toBeVisible();
});

test("results page shows the empty state when nothing is published", async ({ page }) => {
  await page.route("http://127.0.0.1:8001/results", (route) =>
    route.fulfill({ status: 404, contentType: "application/json", body: JSON.stringify({ error: { code: "no_results", message: "none" } }) }),
  );
  await page.goto("/results");
  await expect(page.getByText("No evaluation results have been published yet.")).toBeVisible();
});

test("results page renders tables and loads the charts", async ({ page }) => {
  await page.goto("/results");
  await expect(page.getByRole("heading", { name: "Evaluation results" })).toBeVisible();
  await expect(page.getByTestId("results-table")).toContainText("full");
  await expect(page.getByText("MOCK runs")).toBeVisible();
  const img = page.getByRole("img", { name: /Correctness vs cost/ });
  await expect(img).toBeVisible();
  await expect.poll(() => img.evaluate((el: HTMLImageElement) => el.complete && el.naturalWidth)).toBeGreaterThan(0);
});

test("unknown repo shows an error state", async ({ page }) => {
  await page.goto("/repos/does-not-exist");
  await expect(page.getByTestId("error")).toContainText("Unknown repository");
});

test("dark mode toggle applies and persists", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: /Switch to dark theme/ }).click();
  await expect(page.locator("html")).toHaveClass(/dark/);
  await page.reload();
  await expect(page.locator("html")).toHaveClass(/dark/);
});

for (const theme of ["light", "dark"]) {
  test(`no serious accessibility violations on the home page (${theme})`, async ({ page }) => {
    await page.addInitScript((t) => localStorage.setItem("theme", t), theme);
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
    const results = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa"]).analyze();
    const serious = results.violations.filter((v) => v.impact === "serious" || v.impact === "critical");
    expect(serious, JSON.stringify(serious.map((v) => [v.id, v.nodes.map((n) => n.target)]))).toEqual([]);
  });
}

test("mobile layout has no horizontal scroll @mobile", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Repositories" })).toBeVisible();
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(0);
  await page.request.post("http://127.0.0.1:8001/repos", { data: { url: REPO_URL } }); // 409 if it already exists
  await expect.poll(async () => (await (await page.request.get(`http://127.0.0.1:8001/repos/${REPO_ID}`)).json()).status, { timeout: 60_000 }).toBe("ready");
  await page.goto(`/repos/${REPO_ID}`);
  await expect(page.getByRole("textbox", { name: "Question" })).toBeVisible();
  expect(await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth)).toBeLessThanOrEqual(0);
});
