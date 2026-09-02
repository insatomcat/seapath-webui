<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Vendored assets

There is no Node build step here, in production or anywhere else, so the one
library this UI does not write itself is committed as the file the browser
loads. A node in a substation has no route to a CDN, and a terminal emulator is
not something to reimplement: xterm.js is what the console panel draws in.

| File | Package | Version | Licence |
|---|---|---|---|
| `xterm.js` | `@xterm/xterm` | 5.5.0 | MIT, in `LICENSE.xterm` |
| `xterm.css` | `@xterm/xterm` | 5.5.0 | MIT, in `LICENSE.xterm` |
| `addon-fit.js` | `@xterm/addon-fit` | 0.10.0 | MIT, in `LICENSE.xterm` |

The files are the published builds, byte for byte:

```
1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495  xterm.js
ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6  xterm.css
bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089  addon-fit.js
```

Upgrading means replacing the files, the versions and the hashes above, and
saying so in the commit message. Nothing else in this UI loads from here.
