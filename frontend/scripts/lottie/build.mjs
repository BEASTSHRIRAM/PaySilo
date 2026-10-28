// Generates PaySilo's Lottie animations into lib/lottie/*.json.
//   node scripts/lottie/build.mjs
//
// Silo is a squirrel: squirrels keep every stash separate — exactly what
// PaySilo does with each employer's money.

import { mkdirSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  anim, composition, cycle, ellipse, fill, group, path, rect, shapeLayer, smooth, star, stroke, trim,
} from "./lib.mjs";

const OUT = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "lib", "lottie");

// Palette: #244855 ink · #E64833 coral · #874F41 clay · #90AEAD sage · #FBE9D0 sand
const C = {
  violet: "#874F41", // body (clay)
  violetDeep: "#6E3F34", // feet
  violetArm: "#7A463A", // arms
  cream: "#FBE9D0", // belly + muzzle (sand)
  ink: "#0C1D23",
  pinkEar: "#F2917F",
  cheek: "#F2917F",
  tail: "#E64833", // coral
  tailLight: "#F2917F",
  nut: "#D9A066",
  cap: "#563129",
  stem: "#3E231D",
  shadow: "#061015",
  gold: "#F3D7B0",
  white: "#FFFFFF",
};

const mirror = (pts) => pts.map(([x, y]) => [512 - x, y]).reverse();

// ---------------------------------------------------------------- Silo parts
function tail(op, wag = [-5, 6]) {
  const outer = [[292, 420], [352, 405], [410, 350], [432, 270], [418, 190], [372, 140], [318, 138], [292, 172],
    [310, 206], [350, 214], [372, 250], [368, 310], [340, 360], [300, 380]];
  const inner = [[330, 380], [378, 330], [396, 262], [384, 205], [352, 182], [338, 196], [356, 234], [352, 300],
    [322, 350]];
  return group("tail", [
    group("tail-light", [smooth(inner), fill(C.tailLight)]),
    group("tail-main", [smooth(outer), fill(C.tail)]),
  ], { pivot: [300, 410], rotation: cycle(op, [wag[0], wag[1]]) });
}

function body() {
  return group("body", [
    group("belly", [ellipse(256, 398, 128, 118), fill(C.cream)]),
    group("torso", [ellipse(256, 385, 200, 175), fill(C.violet)]),
  ]);
}

function feet() {
  return group("feet", [
    group("foot-l", [ellipse(212, 462, 66, 28), fill(C.violetDeep)]),
    group("foot-r", [ellipse(300, 462, 66, 28), fill(C.violetDeep)]),
  ]);
}

function eye(cx, cy, blinkKeys) {
  return group(`eye-${cx}`, [
    group("shine2", [ellipse(cx - 6, cy + 12, 6, 6), fill(C.white)]),
    group("shine", [ellipse(cx + 8, cy - 12, 13, 13), fill(C.white)]),
    group("pupil", [ellipse(cx, cy, 36, 44), fill(C.ink)]),
  ], { pivot: [cx, cy], scale: blinkKeys });
}

function happyEyes() {
  const arc = (cx) => path([[cx - 16, 252], [cx + 16, 252]], [[0, 0], [8, -14]], [[8, -14], [0, 0]]);
  return group("happy-eyes", [arc(210), arc(302), stroke(C.ink, 7)]);
}

function blink(op, at) {
  if (at === null) return undefined;
  return anim([[0, [100, 100]], [at, [100, 100]], [at + 5, [100, 10]], [at + 10, [100, 100]], [op, [100, 100]]]);
}

function face({ op, blinkAt, expression = "calm", cheekOpacity = 80 }) {
  const items = [];
  if (expression === "cheer") {
    items.push(group("mouth-open", [
      group("tongue", [ellipse(256, 306, 16, 9), fill(C.cheek)]),
      group("mouth", [ellipse(256, 300, 30, 24), fill(C.ink)]),
    ]));
  } else {
    items.push(group("mouth", [
      path([[238, 292], [256, 296], [274, 292]], [[0, 0], [-6, 6], [-2, 9]], [[2, 9], [6, 6], [0, 0]]),
      stroke(C.ink, 5),
    ]));
    items.push(group("teeth", [rect(256, 304, 16, 12, 3), fill(C.white)]));
  }
  items.push(group("nose", [ellipse(256, 276, 26, 16), fill(C.ink)]));
  items.push(group("cheeks", [ellipse(172, 288, 38, 22), ellipse(340, 288, 38, 22), fill(C.cheek, cheekOpacity)]));
  if (expression === "cheer") items.push(happyEyes());
  else items.push(eye(210, 246, blink(op, blinkAt)), eye(302, 246, blink(op, blinkAt)));
  return items;
}

function head({ op, blinkAt, expression, tilt, cheekOpacity, earTwitchAt }) {
  const earL = [[178, 206], [160, 150], [168, 112], [198, 120], [228, 170]];
  const innerL = [[186, 190], [176, 152], [182, 130], [198, 138], [214, 174]];
  const twitch = earTwitchAt == null ? undefined
    : anim([[0, 0], [earTwitchAt, 0], [earTwitchAt + 6, 12], [earTwitchAt + 12, -4], [earTwitchAt + 18, 0], [op, 0]]);
  return group("head", [
    ...face({ op, blinkAt, expression, cheekOpacity }),
    group("muzzle", [ellipse(256, 292, 120, 80), fill(C.cream)]),
    group("skull", [ellipse(256, 248, 220, 200), fill(C.violet)]),
    group("ear-r", [
      group("inner", [smooth(mirror(innerL)), fill(C.pinkEar)]),
      group("outer", [smooth(mirror(earL)), fill(C.violet)]),
    ], { pivot: [320, 185], rotation: twitch }),
    group("ear-l", [
      group("inner", [smooth(innerL), fill(C.pinkEar)]),
      group("outer", [smooth(earL), fill(C.violet)]),
    ]),
  ], { pivot: [256, 340], rotation: tilt });
}

function acorn(x, y, opts = {}) {
  return group("acorn", [
    group("shine", [ellipse(x - 10, y + 2, 10, 16), fill(C.white, 35)]),
    group("cap", [ellipse(x, y - 18, 56, 26), fill(C.cap)]),
    group("stem", [rect(x, y - 34, 6, 12, 3), fill(C.stem)]),
    group("nut", [ellipse(x, y + 4, 46, 48), fill(C.nut)]),
  ], { pivot: [x, y], ...opts });
}

/** An arm drawn straight up from its shoulder, rotated into place. */
function arm(name, shoulder, length, rotation) {
  const [sx, sy] = shoulder;
  return group(name, [
    group("paw", [ellipse(sx, sy - length, 40, 38), fill(C.violetArm)]),
    group("limb", [rect(sx, sy - length / 2, 32, length, 16), fill(C.violetArm)]),
  ], { pivot: shoulder, rotation });
}

function shadowLayer(op, scaleKeys) {
  return shapeLayer("shadow", [group("shadow", [ellipse(256, 470, 210, 28), fill(C.shadow, 30)])], op,
    { pivot: [256, 470], scale: scaleKeys });
}

// ---------------------------------------------------------------- Silo poses
function siloIdle() {
  const op = 240;
  const bob = anim([[0, [256, 470]], [60, [256, 460]], [120, [256, 470]], [180, [256, 460]], [240, [256, 470]]]);
  const squash = anim([[0, [103, 97]], [60, [99, 101]], [120, [103, 97]], [180, [99, 101]], [240, [103, 97]]]);
  const silo = shapeLayer("silo", [
    group("arm-r", [ellipse(290, 352, 44, 34), fill(C.violetArm)], { pivot: [290, 352], rotation: -25 }),
    group("arm-l", [ellipse(222, 352, 44, 34), fill(C.violetArm)], { pivot: [222, 352], rotation: 25 }),
    acorn(256, 352),
    head({ op, blinkAt: 150, tilt: cycle(op, [0, -2, 0, 2]), earTwitchAt: 70 }),
    feet(),
    body(),
    tail(op),
  ], op, { pivot: [256, 470], position: bob, scale: squash });
  const shadow = shadowLayer(op, anim([[0, [100, 100]], [60, [88, 88]], [120, [100, 100]], [180, [88, 88]], [240, [100, 100]]]));
  return composition("silo-idle", { w: 512, h: 512, op, layers: [shadow, silo] });
}

function siloWave() {
  const op = 120;
  const wave = anim([[0, 52], [20, 26], [40, 52], [60, 26], [80, 52], [120, 52]]);
  const silo = shapeLayer("silo", [
    arm("arm-wave", [334, 352], 100, wave),
    group("arm-l", [ellipse(226, 352, 44, 34), fill(C.violetArm)], { pivot: [226, 352], rotation: 25 }),
    acorn(250, 354),
    head({ op, blinkAt: 96, tilt: anim([[0, 0], [30, -5], [90, -5], [120, 0]]), earTwitchAt: 20 }),
    feet(),
    body(),
    tail(op, [-8, 8]),
  ], op, { pivot: [256, 470], position: anim([[0, [256, 470]], [60, [256, 464]], [120, [256, 470]]]) });
  return composition("silo-wave", { w: 512, h: 512, op, layers: [shadowLayer(op), silo] });
}

function siloCheer() {
  const op = 90;
  const jump = anim([[0, [256, 470]], [10, [256, 474]], [36, [256, 420]], [60, [256, 470]], [70, [256, 474]], [90, [256, 470]]]);
  const squash = anim([[0, [100, 100]], [10, [108, 92]], [22, [94, 106]], [48, [100, 100]], [62, [108, 92]], [76, [100, 100]], [90, [100, 100]]]);
  const sparkles = [[150, 150, 18, 30], [372, 120, 14, 40], [120, 300, 12, 50], [400, 300, 16, 20]].map(([x, y, r, t0]) =>
    group(`sparkle-${x}`, [star(x, y, r), fill(C.gold)], {
      pivot: [x, y], scale: anim([[0, [0, 0]], [t0, [0, 0]], [t0 + 12, [110, 110]], [t0 + 26, [0, 0]], [op, [0, 0]]]),
      rotation: anim([[0, 0], [op, 90]]),
    }));
  const silo = shapeLayer("silo", [
    arm("arm-r", [300, 350], 92, cycle(op, [28, 40])),
    arm("arm-l", [212, 350], 92, cycle(op, [-28, -40])),
    head({ op, blinkAt: null, expression: "cheer", cheekOpacity: 100 }),
    feet(),
    body(),
    tail(op, [-12, 12]),
  ], op, { pivot: [256, 470], position: jump, scale: squash });
  const toss = shapeLayer("acorn", [acorn(256, 120, { rotation: anim([[0, 0], [op, 360]]) })], op, {
    pivot: [256, 120],
    position: anim([[0, [256, 92]], [36, [256, 26]], [60, [256, 92]], [90, [256, 92]]]),
  });
  const fx = shapeLayer("sparkles", sparkles, op);
  const shadow = shadowLayer(op, anim([[0, [100, 100]], [36, [70, 70]], [60, [100, 100]], [90, [100, 100]]]));
  return composition("silo-cheer", { w: 512, h: 512, op, layers: [shadow, silo, toss, fx] });
}

function siloShy() {
  const op = 120;
  const sway = cycle(op, [-3, 3]);
  const cover = (name, shoulder, eyeAt) => {
    const [sx, sy] = shoulder, [ex, ey] = eyeAt;
    return group(name, [
      group("paw", [ellipse(ex, ey, 58, 50), fill(C.violetArm)]),
      group("limb", [path([[sx, sy], [ex, ey + 14]], [[0, 0], [(sx - ex) * 1.4, 50]], [[(sx - ex) * 0.9, -40], [0, 0]]),
        stroke(C.violetArm, 30)]),
    ]);
  };
  const silo = shapeLayer("silo", [
    group("hands", [cover("paw-l", [190, 360], [212, 248]), cover("paw-r", [322, 360], [300, 248])],
      { pivot: [256, 340], rotation: sway }),
    head({ op, blinkAt: null, tilt: sway, cheekOpacity: anim([[0, 80], [60, 100], [120, 80]]) }),
    feet(),
    body(),
    tail(op, [-3, 3]),
  ], op, { pivot: [256, 470] });
  return composition("silo-shy", { w: 512, h: 512, op, layers: [shadowLayer(op), silo] });
}

// ---------------------------------------------------------------- scenes
function silos() {
  const op = 180, W = 560, H = 400;
  const tones = [["#90AEAD", "#6F8F8E"], ["#874F41", "#6E3F34"], ["#E64833", "#A42F1F"]];
  const xs = [110, 280, 450];
  const layers = [];
  xs.forEach((x, idx) => {
    const [main, dark] = tones[idx];
    const land = 40 + idx * 50;
    const bump = anim([[0, [100, 100]], [land, [100, 100]], [land + 6, [106, 94]], [land + 16, [100, 100]], [op, [100, 100]]]);
    layers.push(shapeLayer(`silo-${idx}`, [
      group("lock", [
        group("shackle", [path([[x - 15, 290], [x + 15, 290]], [[0, 0], [0, -28]], [[0, -28], [0, 0]]), stroke("#FBE9D0", 7, 95)]),
        group("lock-body", [rect(x, 306, 44, 34, 9), fill("#FBE9D0", 95)]),
      ], { pivot: [x, 300], scale: anim([[0, [100, 100]], [land + 6, [100, 100]], [land + 14, [118, 118]], [land + 24, [100, 100]], [op, [100, 100]]]) }),
      group("top", [ellipse(x, 150, 142, 40), fill(dark)]),
      group("rim", [ellipse(x, 155, 152, 48), fill(main)]),
      group("band", [rect(x, 230, 152, 12, 0), fill(dark, 60)]),
      group("body", [rect(x, 268, 152, 236, 36), fill(main)]),
    ], op, { pivot: [x, 386], scale: bump }));
    // Coin: drops into ITS OWN silo only.
    const t0 = land - 30;
    layers.push(shapeLayer(`coin-${idx}`, [
      group("coin", [
        group("mark", [rect(x, 0, 6, 16, 2), fill("#874F41")]),
        group("ring", [ellipse(x, 0, 26, 26), stroke("#C08A7C", 4)]),
        group("face", [ellipse(x, 0, 38, 38), fill("#F3D7B0")]),
      ], { pivot: [x, 0], scale: anim([[0, [100, 100]], [t0 + 20, [100, 100]], [t0 + 30, [100, 30]], [op, [100, 30]]]) }),
    ], op, {
      pivot: [x, 0],
      position: anim([[0, [x, -40]], [t0, [x, -40]], [t0 + 30, [x, 155]], [op, [x, 155]]], [0.55, 0.9]),
      opacity: anim([[0, 0], [t0, 0], [t0 + 4, 100], [t0 + 28, 100], [t0 + 31, 0], [op, 0]]),
    }));
  });
  return composition("silos", { w: W, h: H, op, layers });
}

function shield() {
  const op = 120;
  const shieldPath = path(
    [[150, 40], [240, 74], [232, 158], [150, 262], [68, 158], [60, 74]],
    [[-35, 12], [-25, -6], [4, -40], [52, -34], [12, 48], [0, 0]],
    [[35, 12], [0, 0], [-12, 48], [-52, -34], [-4, -40], [25, -6]], true);
  const layer = shapeLayer("shield", [
    group("check", [
      path([[110, 150], [140, 180], [196, 118]], [[0, 0], [0, 0], [0, 0]], [[0, 0], [0, 0], [0, 0]]),
      trim(0, anim([[0, 0], [18, 0], [48, 100], [op, 100]])),
      stroke("#FBE9D0", 16),
    ]),
    group("shield-inner", [shieldPath, fill("#244855")], { pivot: [150, 150], scale: [86, 86] }),
    group("shield", [shieldPath, fill("#90AEAD")]),
  ], op, { pivot: [150, 150], scale: anim([[0, [92, 92]], [14, [104, 104]], [26, [100, 100]], [op, [100, 100]]]) });
  const ring = shapeLayer("ring", [group("ring", [ellipse(150, 150, 220, 220), stroke("#E64833", 6)])], op, {
    pivot: [150, 150],
    scale: anim([[0, [70, 70]], [48, [70, 70]], [96, [130, 130]], [op, [130, 130]]]),
    opacity: anim([[0, 0], [48, 0], [52, 90], [96, 0], [op, 0]]),
  });
  return composition("shield", { w: 300, h: 300, op, layers: [ring, layer] });
}

function burst() {
  const op = 90, cx = 200, cy = 200;
  const colors = ["#E64833", "#FBE9D0", "#90AEAD", "#874F41", "#F2917F", "#F3D7B0"];
  const pieces = Array.from({ length: 22 }, (_, idx) => {
    const angle = (Math.PI * 2 * idx) / 22 + (idx % 3) * 0.12;
    const dist = 120 + (idx % 4) * 22;
    const tx = cx + Math.cos(angle) * dist, ty = cy + Math.sin(angle) * dist + 30;
    const color = colors[idx % colors.length];
    const shape = idx % 2 === 0 ? rect(0, 0, 14, 8, 2) : ellipse(0, 0, 11, 11);
    return shapeLayer(`piece-${idx}`, [group("p", [shape, fill(color)], { rotation: anim([[0, 0], [op, 360 * (idx % 2 ? 1 : -1)]]) })], op, {
      position: anim([[0, [cx, cy]], [40, [tx, ty - 20]], [op, [tx, ty + 30]]], [0.1, 0.6]),
      opacity: anim([[0, 100], [60, 100], [op, 0]]),
      scale: anim([[0, [30, 30]], [12, [120, 120]], [op, [80, 80]]]),
    });
  });
  return composition("burst", { w: 400, h: 400, op, layers: pieces });
}

const outputs = {
  "silo-idle.json": siloIdle(),
  "silo-wave.json": siloWave(),
  "silo-cheer.json": siloCheer(),
  "silo-shy.json": siloShy(),
  "silos.json": silos(),
  "shield.json": shield(),
  "burst.json": burst(),
};

mkdirSync(OUT, { recursive: true });
for (const [file, data] of Object.entries(outputs)) {
  writeFileSync(join(OUT, file), JSON.stringify(data));
  console.log(`wrote lib/lottie/${file} (${data.layers.length} layers, ${(data.op / data.fr).toFixed(1)}s)`);
}
