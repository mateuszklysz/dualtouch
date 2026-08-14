// Render the DualTouch app icon with Vercel Satori.
//
// Design (two Steam-Deck-style trackpads, left + right, GRUVBOX DARK):
//   - Rounded-rectangle body (10% corner radius), filling ~88% of the canvas.
//   - Body is dark Gruvbox bg1 -> bg0 with a subtle top highlight and a soft
//     drop shadow; a faint gray rim keeps the silhouette visible on both
//     light and dark taskbars.
//   - Two trackpads as simple rounded rectangles, angled outward like a real
//     deck (left -10deg, right +10deg), with a CLEAR ASYMMETRIC vertical
//     offset (left pad sits well above the right pad).
//   - LEFT pad is the lit one (fg2); RIGHT pad is darker (gray).
//   - The pads sit slightly ABOVE the body (a dark, near-black rim + soft
//     under-shadow lifts them off the icon — iOS-style Z-depth).
//   - The RIGHT pad carries faint concentric rings, pushed toward the
//     lower-right corner — a finger-tap landing point.
//
// Pipeline: Satori composes pure HTML/CSS -> SVG, resvg-js rasterizes it.
//
// Usage:  node render.mjs   (writes render.svg + icon-1024.png)

import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import satori from "satori";
import { Resvg } from "@resvg/resvg-js";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const OUT_SVG = path.join(__dirname, "render.svg");
const OUT_PNG = path.join(__dirname, "icon-1024.png");
const SIZE = 1024;

// Gruvbox dark palette.
const GB = {
  bg0: "#282828",
  bg1: "#3c3836",
  bg2: "#504945",
  bg4: "#7c6f64",
  gray: "#928374",
  fg4: "#a89984",
  fg2: "#d5c4a1",
  fg1: "#ebdbb2",
};

// Near-black rim: makes the pad read as raised above the body (Z depth).
const RIM_DARK = "rgba(20, 22, 22, 0.9)";

// Satori children live in props.children; a div with children must be flex.
const h = (tag, props, ...children) => ({
  type: tag,
  props: { ...(props ?? {}), children: children.flat() },
});

async function rasterizePng(svgString) {
  const r = new Resvg(svgString, {
    fitTo: { mode: "width", value: SIZE },
    background: "rgba(0,0,0,0)",
  }).render();
  return r.asPng();
}

// A rectangular trackpad raised off the body: dark near-black rim (the pad's
// side wall) + soft under-shadow for the iOS-style Z lift. `rise` is the
// vertical offset in px (negative = higher). Applied via marginTop so it
// stays independent of the rotation.
function pad({ size, face, angle, rise, children }) {
  return h(
    "div",
    {
      style: {
        display: "flex",
        width: size,
        height: size,
        borderRadius: Math.round(size * 0.12),
        alignItems: "center",
        justifyContent: "center",
        backgroundImage: face,
        border: `${Math.round(size * 0.022)}px solid ${RIM_DARK}`,
        boxShadow:
          "0 22px 40px rgba(0,0,0,0.7), inset 0 2px 6px rgba(255,255,255,0.12)",
        transform: `rotate(${angle}deg)`,
        marginTop: rise,
      },
    },
    children,
  );
}

// A faint concentric ring, used for the "finger tap" ripple on the right pad.
// `size` is the wrapper box; each ring is centered inside it.
function ripple({ diameter, thickness, size }) {
  const off = Math.round((size - diameter) / 2);
  return h("div", {
    style: {
      display: "flex",
      position: "absolute",
      left: off,
      top: off,
      width: diameter,
      height: diameter,
      borderRadius: "50%",
      border: `${thickness}px solid rgba(40, 40, 40, 0.6)`,
    },
  });
}

const PAD = Math.round(SIZE * 0.38); // each pad is 38% of the canvas (was 33%)
// Ripple wrapper size (largest ring) and its lower-right nudge (20% total).
const RIP_W = Math.round(PAD * 0.48);
const RIP_DX = Math.round(PAD * 0.20);
const RIP_DY = Math.round(PAD * 0.11);

// 2-stop gradients only (3 stops flatten to noise at 16px).
const LEFT_FACE = `linear-gradient(135deg, ${GB.fg1} 0%, ${GB.fg2} 100%)`;
const RIGHT_FACE = `linear-gradient(135deg, ${GB.fg4} 0%, ${GB.gray} 100%)`;

const jsx = h(
  "div",
  {
    style: {
      width: SIZE,
      height: SIZE,
      display: "flex",
      alignItems: "center",
      justifyContent: "center",
    },
  },
  h(
    "div",
    {
      style: {
        display: "flex",
        width: "94%",
        height: "94%",
        borderRadius: Math.round(SIZE * 0.10),
        position: "relative",
        alignItems: "center",
        justifyContent: "center",
        gap: Math.round(SIZE * 0.06),
        backgroundImage: `linear-gradient(165deg, ${GB.bg1} 0%, ${GB.bg0} 100%)`,
        // External drop shadow behind the whole body + a hairline rim so the
        // silhouette lifts off whatever tray/wallpaper is behind it.
        boxShadow: `0 28px 56px rgba(0,0,0,0.6), 0 0 0 2px rgba(20, 22, 22, 0.6)`,
        overflow: "hidden",
      },
    },
    // Inner depth: subtle top highlight ("lit from above") + soft inner
    // vignette at the bottom — contained inside the body, never clipped.
    h("div", {
      style: {
        display: "flex",
        position: "absolute",
        inset: 0,
        borderRadius: Math.round(SIZE * 0.10),
        backgroundImage:
          "linear-gradient(180deg, rgba(235,219,178,0.09) 0%, rgba(235,219,178,0) 30%), linear-gradient(0deg, rgba(0,0,0,0.35) 0%, rgba(0,0,0,0) 22%)",
      },
    }),
    // Left pad — the lit pad, clearly HIGHER, angled +10deg (to the right).
    pad({
      size: PAD,
      face: LEFT_FACE,
      angle: 10,
      rise: -Math.round(SIZE * 0.09),
    }),
    // Right pad — darker, clearly LOWER, angled -10deg (to the left),
    // with the finger-tap ripple pushed toward the lower-right corner.
    pad({
      size: PAD,
      face: RIGHT_FACE,
      angle: -10,
      rise: Math.round(SIZE * 0.09),
      children: [
        h(
          "div",
          {
            style: {
              display: "flex",
              position: "relative",
              width: RIP_W,
              height: RIP_W,
              marginLeft: RIP_DX,
              marginTop: RIP_DY,
            },
          },
          ripple({
            diameter: RIP_W,
            thickness: Math.max(3, Math.round(PAD * 0.022)),
            size: RIP_W,
          }),
          ripple({
            diameter: Math.round(PAD * 0.36),
            thickness: Math.max(3, Math.round(PAD * 0.018)),
            size: RIP_W,
          }),
          ripple({
            diameter: Math.round(PAD * 0.26),
            thickness: Math.max(2, Math.round(PAD * 0.014)),
            size: RIP_W,
          }),
        ),
      ],
    }),
  ),
);

const svg = await satori(jsx, { width: SIZE, height: SIZE, fonts: [] });
fs.writeFileSync(OUT_SVG, svg);
console.log(`wrote ${OUT_SVG}`);

const png = await rasterizePng(svg);
fs.writeFileSync(OUT_PNG, png);
console.log(`wrote ${OUT_PNG} (${png.length} bytes)`);
