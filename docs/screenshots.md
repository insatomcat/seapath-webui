<!-- SPDX-License-Identifier: CC-BY-4.0 -->
<!-- Copyright (C) 2026, RTE (http://www.rte-france.com) -->

# Recapturing the README screenshots

The images under `img/` are shot against a live deployment with Playwright
driving a real Chrome. This note records the settings so a screenshot can be
redone without rediscovering them, and so a stale image never has to be kept
because nobody remembers how the set was made.

## What the images must match

Every README screenshot uses the same frame, so the set reads as one UI:

- **1500 CSS px wide**, `deviceScaleFactor: 2` (the PNGs are 3000 px wide).
- **Dark theme** (`colorScheme: 'dark'`).
- Cropped to the content, not to a fixed height. The heights differ per image
  on purpose.

## The deployment and its credentials

The reference set is shot against the demo cluster at
`https://<host>/ccvui/`. Reaching it crosses two gates:

1. An nginx basic-auth gate in front of everything (`realm="Accès restreint"`).
2. The application's own login form (`virtu` is the admin demo account).

Neither credential lives in this repository. The maintainer supplies both out
of band; keep them out of git, out of the script, and out of any committed
file. Pass them to the script through the environment (see below).

## The Real time page: five views in one shot each

Real time is an application layout (D28): the shell is the viewport and one
panel is on screen at a time, so a full-page capture would only grab the first
panel. Switch the tab, then clip to the visible card's real bottom. The five
views, in the tab order, which is also the file numbering:

| `data-view`        | file                              |
| ------------------ | --------------------------------- |
| `checks`           | `11-1-realtime-conformance.png`   |
| `pool`             | `11-2-realtime-cpu-pool.png`      |
| `cyclictest`       | `11-3-realtime-latency.png`       |
| `guest_cyclictest` | `11-4-realtime-guest-latency.png` |
| `hwlatdetect`      | `11-5-realtime-firmware.png`      |

When a view is added or renamed, keep the file numbers following the tab order
and update the README captions and this table together.

## The script

A tall viewport lets each view lay out at its natural height; the clip is then
taken to the visible card's bottom. `channel: 'chrome'` uses the installed
Chrome, so no Playwright browser download is needed.

```js
const { chromium } = require('playwright');

const BASE = process.env.BASE;                 // https://<host>/ccvui
const GATE = { username: process.env.GATE_USER, password: process.env.GATE_PASS };
const APP_USER = process.env.APP_USER, APP_PASS = process.env.APP_PASS;

const VIEWS = [
  ['checks', '11-1-realtime-conformance'],
  ['pool', '11-2-realtime-cpu-pool'],
  ['cyclictest', '11-3-realtime-latency'],
  ['guest_cyclictest', '11-4-realtime-guest-latency'],
  ['hwlatdetect', '11-5-realtime-firmware'],
];

(async () => {
  const browser = await chromium.launch({ channel: 'chrome' });
  const ctx = await browser.newContext({
    viewport: { width: 1500, height: 1600 },
    deviceScaleFactor: 2,
    colorScheme: 'dark',
    ignoreHTTPSErrors: true,
    httpCredentials: GATE,
  });
  const page = await ctx.newPage();
  await page.goto(BASE + '/realtime', { waitUntil: 'domcontentloaded' });
  if (await page.locator('form input[type=password]').count()) {
    await page.fill('input[name=username], #username', APP_USER);
    await page.fill('input[type=password]', APP_PASS);
    await page.click('button[type=submit], input[type=submit]');
    await page.waitForLoadState('networkidle');
    await page.goto(BASE + '/realtime', { waitUntil: 'domcontentloaded' });
  }
  await page.waitForSelector('nav#views button.view');
  await page.waitForLoadState('networkidle');
  await page.waitForTimeout(4000);            // let the exporter fan-out settle
  for (const [view, name] of VIEWS) {
    await page.click(`nav#views button.view[data-view="${view}"]`);
    await page.waitForTimeout(1800);
    const bottom = await page.evaluate(() => {
      const card = document.querySelector('.page.paned > .card:not([hidden])');
      return Math.min(window.innerHeight, Math.ceil(card.getBoundingClientRect().bottom + 12));
    });
    await page.screenshot({ path: `img/${name}.png`, clip: { x: 0, y: 0, width: 1500, height: bottom } });
  }
  await browser.close();
})();
```

The footer prints `seapath-webui <version>`. Read it off the shot to prove it
came from the build you meant, since a check turns green only once the fix it
covers has shipped (for example clock synchronisation went green in 0.3.80).

The other pages are the two-pane document layout, not this paned one, so they
are captured full-page and do not need the per-view clip.
