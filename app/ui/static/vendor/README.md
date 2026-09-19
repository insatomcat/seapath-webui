<!--
Copyright (C) 2026, RTE (http://www.rte-france.com)
SPDX-License-Identifier: CC-BY-4.0
-->

# Vendored assets

There is no Node build step here, in production or anywhere else, so the two
libraries this UI does not write itself are committed as the files the browser
loads. A node in a substation has no route to a CDN, and neither a terminal
emulator nor a VNC client is something to reimplement: xterm.js is what the
console panel draws in, and noVNC is what shows a guest's screen.

| File | Package | Version | Licence |
|---|---|---|---|
| `xterm.js` | `@xterm/xterm` | 5.5.0 | MIT, in `LICENSE.xterm` |
| `xterm.css` | `@xterm/xterm` | 5.5.0 | MIT, in `LICENSE.xterm` |
| `addon-fit.js` | `@xterm/addon-fit` | 0.10.0 | MIT, in `LICENSE.xterm` |
| `novnc/core/` | noVNC | 1.7.0 | MPL-2.0, in `novnc/LICENSE.txt` |
| `novnc/vendor/pako/` | pako, as noVNC 1.7.0 carries it | | MIT, in `novnc/vendor/pako/LICENSE` |

The files are the published builds, byte for byte:

```
1f991ac3b4b283ebf96e60ae23a00a52765dd3a2e46fa6fdda9f1aab032f7495  xterm.js
ba8e6985669488981ccf40c0cefe3aba80722cb6c92de7ad628b0bd717faf2b6  xterm.css
bdaefa370b1bfc42ee88d46fe6072400902a4d4b2d45cd93438dda9b23c97089  addon-fit.js
```

noVNC is the `core/` and `vendor/pako/` folders of its v1.7.0 tag, byte for
byte and nothing else: they are ES modules the browser loads as they are, and
`graphic.js` imports `core/rfb.js` the first time a graphic console opens, so a
page that opens none fetches none of them. The tree is checked with one hash,
taken from inside `novnc/`:

```
$ find core vendor -type f | LC_ALL=C sort | xargs shasum -a 256 | shasum -a 256
0e61b932064ec6ecffa0a4e4443fc2f447e7f1d1452ec2070f35c029f60bc334  -
```

Upgrading means replacing the files, the versions and the hashes above, and
saying so in the commit message. Nothing else in this UI loads from here.
