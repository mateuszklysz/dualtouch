# DualTouch app icon (Vercel Satori)

Regenerates the app icon from a Satori (HTML/CSS → SVG) source.

## Files

- `render.mjs` — the Satori renderer. Composes the icon as HTML/CSS
  (flexbox layers, `border-radius: 50%` touchpads, gradients, drop shadow)
  and rasterizes to `icon-1024.png` with resvg-js.
- `package.json` / `package-lock.json` — Node deps (`satori`,
  `@resvg/resvg-js`). `node_modules/` is gitignored; run `npm install` once.
- `icon-1024.png` — generated master (build artifact, gitignored).

## Design

Two Steam-Controller-style touchpads (left + right) on a rounded-square body:

- The body fills ~92% of the canvas with uniform margins, so the icon reads
  large at tray size (16px) and nothing is clipped at the top.
- **Left pad** is a dark steel gradient — always visible against the white
  body and on light tray backgrounds (the old all-white pad was invisible).
- **Right pad** is white with a distinct rim and inner "indent" ring,
  mirroring the real controller's right touchpad.
- Each pad carries the signature centered indent ring.

## Usage

```bat
cd windows
python tools/rebuild_icon.py
```

This runs the Satori render and downscales the master to
`data/images/app_icon.ico` (7 sizes) and `data/images/icon.png`
(500×500 Steam grid art). Requires Python + Pillow and Node.js
(`npm install` in this directory once).
