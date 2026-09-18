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

## The Real time and Cluster pages: one shot per view

Real time and Cluster are application layouts (D28): the shell is the viewport
and one panel is on screen at a time, so a full-page capture would only grab
the first panel. Switch the tab, then clip to the visible card's real bottom.
Cluster's three views, `members`, `resources` and `storage`, are
`8-cluster-membership.png`, `9-cluster-resources.png` and
`10-cluster-storage.png`. The five views of Real time, in the tab order, which
is also the file numbering:

| `data-view`        | file                              |
| ------------------ | --------------------------------- |
| `checks`           | `12-1-realtime-conformance.png`   |
| `pool`             | `12-2-realtime-cpu-pool.png`      |
| `cyclictest`       | `12-3-realtime-latency.png`       |
| `guest_cyclictest` | `12-4-realtime-guest-latency.png` |
| `hwlatdetect`      | `12-5-realtime-firmware.png`      |

When a view or a page is added or renamed, keep the file numbers following the
tab order, and update the README captions and this table together. The pages
follow the top bar: Node 1, Inventory 2, Deployment 3 to 5, VMs 6, Containers
7, Cluster 8 to 10, Backup 11, Real time 12, Runs 13.

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
  ['checks', '12-1-realtime-conformance'],
  ['pool', '12-2-realtime-cpu-pool'],
  ['cyclictest', '12-3-realtime-latency'],
  ['guest_cyclictest', '12-4-realtime-guest-latency'],
  ['hwlatdetect', '12-5-realtime-firmware'],
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

The other pages are the document layout, so they are captured full-page and do
not need the per-view clip. Clip them to the bottom of `footer.footer` plus
12 px, so the empty ground a tall viewport leaves under a short page is not in
the image. Select it by its class: the Node page has a `<footer>` of its own
inside a card. The Node page is served at the root, `BASE + '/'`.

`13-runs.png` and `13-1-runs-results.png` show the `backup_full` run, opened
with `runs?run=<id>`; the second has the per host table unfolded by a click on
`#hosts-summary`.

## Windows over a page

These images show a window rather than a page. Use a viewport tall enough for
the window to fit (2600 px does), open it, and clip to the full 1500 px width
from 32 px above the `.modal-body` to 32 px below it, so the dimmed page
shows at both sides.

| file                           | how the window is opened                                                                                                                |
| ------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------- |
| `4-deployment-reaching.png`    | Deployment, `#reach-open`. Close with Close. |
| `5-deployment-code.png`        | Deployment, `#collection-open`. Close with Close. |
| `5-1-deployment-machines.png`  | Deployment, the first **Choose machines**, with `node1` and `node2` checked, then the list scrolled back to its top. Close with Cancel. |
| `6-1-add-vm.png`               | VMs, **Add a VM**: name `rtu-bay3`, a held qcow2 and `guest.xml.j2` picked, bridge `br0`, address `192.168.110.43/24`, gateway and resolver `192.168.110.1`, packages `qemu-guest-agent, rt-tests`. Never press Add and deploy. |
| `6-2-vm-run.png`               | VMs, then `RunWatch.open("<id>")` from `page.evaluate`, with the id of a finished `deploy_vms_cluster` run from `GET api/v1/runs`, and 8 s for the stream to replay. |
| `7-1-add-container.png`        | Containers, `#add`, empty. Close with Cancel. |
| `7-2-container-quadlet.png`    | Containers, the first `.quadlet-open` link. |
| `11-1-backup-list.png`         | Backup, **Show the backups**, once `#catalogue-table` is shown. A read over SSH, no run. Never press Restore. |
| `13-2-runs-time.png`           | Runs, `runs?run=<id>` of the same finished `deploy_vms_cluster` run, then **Where the time went**. |

The run window replays a run that already happened, so no playbook is launched
to take this picture: these are live substation hypervisors.
