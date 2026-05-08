const { test, expect } = require('playwright/test');

test('class cards expand and reveal actions on hover', async ({ page }) => {
  const logs = [];
  page.on('console', message => {
    if (['error', 'warning'].includes(message.type())) logs.push(`${message.type()}: ${message.text()}`);
  });
  page.on('pageerror', error => logs.push(`pageerror: ${error.message}`));

  await page.goto('http://localhost:8083', { waitUntil: 'domcontentloaded' });
  await expect(page.locator('.cc').first()).toBeVisible({ timeout: 30000 });

  const first = page.locator('.cc').first();
  const before = await first.boundingBox();
  await first.hover();
  await page.waitForTimeout(350);
  const after = await first.boundingBox();

  const tools = await page.locator('.cc-hover-tools').first().evaluate(el => {
    const style = getComputedStyle(el);
    return {
      opacity: Number(style.opacity),
      maxHeight: style.maxHeight,
      pointerEvents: style.pointerEvents,
      display: style.display,
      text: el.textContent.trim().replace(/\s+/g, ' '),
    };
  });

  expect(after.height).toBeGreaterThan(before.height + 80);
  expect(tools.opacity).toBeGreaterThan(0.9);
  expect(tools.pointerEvents).toBe('auto');
  expect(tools.text).toContain('Similar');
  expect(tools.text).toContain('Trainer');
  expect(logs.filter(line => !line.includes('favicon'))).toEqual([]);
});
