// Optional browser check of the portal. Needs: npm i playwright && npx playwright install chromium
// Usage: URL=http://127.0.0.1:8080 ALICE=<token> CAROL=<token> node tests/ui_smoke.js
// Flow: alice requests an expensive environment -> pending -> carol approves -> alice sees it ACTIVE.
const { chromium } = require("playwright");
const URL = process.env.URL || "http://127.0.0.1:8080";
const assert = (c, m) => { if (!c) { console.error("FAIL:", m); process.exitCode = 1; } else console.log("ok:", m); };

(async () => {
  const browser = await chromium.launch();
  const errors = [];
  const login = async (token) => {
    const page = await (await browser.newContext({ viewport: { width: 1200, height: 800 } })).newPage();
    page.on("pageerror", (e) => errors.push(String(e)));
    page.on("console", (m) => m.type() === "error" && errors.push(m.text()));
    await page.goto(URL);
    await page.fill("input[type=password]", token);
    await page.press("input[type=password]", "Enter");
    await page.waitForSelector("nav");
    return page;
  };
  const name = "ui" + Date.now().toString(36).slice(-6);

  const alice = await login(process.env.ALICE);
  await alice.click("nav >> text=New environment");
  await alice.selectOption("#f-template", "local-demo");
  await alice.fill("#f-name", name);
  await alice.selectOption("select[id$=tier]", "large");
  await alice.fill("#f-ttl", "240");
  await alice.waitForFunction(() => !document.querySelector("#f-submit").disabled);
  assert(/approval/i.test(await alice.innerText("body")), "preview says this request needs approval");
  await alice.click("#f-submit");
  await alice.waitForSelector(`tr:has-text("${name}"):has-text("pending approval")`);
  assert(true, "request shows as pending approval");

  const carol = await login(process.env.CAROL);
  await carol.click("nav >> text=Approvals");
  await carol.waitForSelector(`.card:has-text("${name}")`);
  assert(!/object HTML/.test(await carol.innerText("#tab")), "approvals tab renders real cards");
  await carol.click(`.card:has-text("${name}") >> text=Approve`);

  await alice.waitForSelector(`tr:has-text("${name}"):has-text("active")`, { timeout: 30000 });
  assert(true, "environment became active after approval");
  assert(errors.length === 0, "no browser console errors" + (errors.length ? ": " + errors.join(" | ") : ""));
  await browser.close();
})().catch((e) => { console.error(e); process.exit(1); });
