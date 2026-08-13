/* Topographic map — paints the contour and vector path data the server traced
   out of an elevation model and vector tiles. Everything is pre-projected into
   a 1000x1000 space; the SVG is sliced to fill whatever cell it lands in, so
   the same payload serves any aspect.

   Draw order is the cartographic one: ground, shaded relief, vegetation,
   contours, then water over the contours (a lake surface has no contours on
   it), then rail and roads, then the marginalia. */

/* Palettes. Mono stays on the e-ink 4-level ramp (000/555/aaa/fff) so a grey
   panel maps it 1:1; the colour themes are for panels that have inks. */
const THEMES = {
  mono: {
    ground: '#ffffff', contour: '#555555', index: '#000000', water: '#555555',
    stream: '#aaaaaa', green: null, road: '#000000', rail: '#555555',
    text: '#000000', scale: 1,
  },
  survey: {
    ground: '#f2ead9', contour: '#a9814e', index: '#7d5a2e', water: '#6f93a8',
    stream: '#9fb8c6', green: '#b9c4a0', road: '#3a352c', rail: '#8a8172',
    text: '#3a352c', scale: 1,
  },
  usgs: {
    ground: '#ffffff', contour: '#b07a4a', index: '#8a5a2b', water: '#4a83c4',
    stream: '#87b3de', green: '#c6ddb8', road: '#1a1a1a', rail: '#555555',
    text: '#000000', scale: 1,
  },
  blueprint: {
    ground: '#0d2b4a', contour: '#5f9fd0', index: '#cfe6f7', water: '#16456f',
    stream: '#3d729b', green: null, road: '#ffffff', rail: '#8fb8d8',
    text: '#ffffff', scale: 1.1,
  },
  relief: {
    ground: '#efe9df', contour: '#8d7a63', index: '#5f5040', water: '#7ba0b5',
    stream: '#a8c2d0', green: null, road: '#2b2b2b', rail: '#8a8172',
    text: '#3a352c', scale: 0.85,
  },
  spectra: {
    ground: '#ffffff', contour: '#c81028', index: '#c81028', water: '#1046c8',
    stream: '#1046c8', green: '#12a04a', road: '#000000', rail: '#000000',
    text: '#000000', scale: 1,
  },
  bwry: {
    ground: '#ffffff', contour: '#000000', index: '#d81020', water: '#000000',
    stream: '#000000', green: '#f0c000', road: '#000000', rail: null,
    text: '#000000', scale: 1.1,
  },
};

/* Custom theme — every ink comes off the cell's colour pickers. */
function customTheme(o) {
  return {
    ground: o.c_ground || '#ffffff',
    contour: o.c_contour || '#000000',
    index: o.c_index || o.c_contour || '#000000',
    water: o.c_water || '#000000',
    stream: o.c_water || '#000000',
    green: o.c_green || null,
    road: o.c_road || '#000000',
    rail: o.c_rail || null,
    text: o.c_text || '#000000',
    scale: 1,
  };
}

/* Stroke weights as a share of the cell's short edge, so a sheet and a
   thumbnail keep the same visual weight. */
/* Contours are the subject of the sheet, so nothing else may out-weigh the
   index line. Streams in particular are a hairline: OSM maps one per gully
   and at any honest weight they bury the terrain. */
const WEIGHT = {
  ctr: 0.17, idx: 0.4, river: 0.3, stream: 0.14, rail: 0.26,
  motorway: 0.74, major: 0.55, mid: 0.38, minor: 0.24, track: 0.13,
};
const ROADS = ['track', 'minor', 'mid', 'major', 'motorway'];

const esc = (s) => String(s == null ? '' : s).replace(/[&<>]/g,
  (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c]));

/* Hillshade the coarse height field the server sent. Lambertian, light from
   the north-west at 45 degrees — the convention every printed relief map uses,
   because lighting from any other quarter reads as inverted terrain. */
function paintRelief(canvas, relief) {
  const { w, h, cells } = relief;
  canvas.width = w;
  canvas.height = h;
  const g = canvas.getContext('2d');
  if (!g) return;
  const img = g.createImageData(w, h);
  const lx = -0.5774, ly = -0.5774, lz = 0.5774;
  const z = 2.2; // vertical exaggeration; flat country needs the help
  for (let j = 0; j < h; j += 1) {
    for (let i = 0; i < w; i += 1) {
      const l = cells[j * w + Math.max(0, i - 1)];
      const r = cells[j * w + Math.min(w - 1, i + 1)];
      const u = cells[Math.max(0, j - 1) * w + i];
      const d = cells[Math.min(h - 1, j + 1) * w + i];
      const dx = (l - r) / 255 * z;
      const dy = (u - d) / 255 * z;
      const len = Math.sqrt(dx * dx + dy * dy + 1);
      let v = (dx * lx + dy * ly + lz) / len;
      v = Math.max(0, Math.min(1, v * 0.5 + 0.5));
      // Keep it pale: the relief is a ground tint, not the subject. The
      // contours have to stay the most legible thing on the sheet.
      const s = Math.round(150 + v * 105);
      const o = (j * w + i) * 4;
      img.data[o] = s;
      img.data[o + 1] = s;
      img.data[o + 2] = s;
      img.data[o + 3] = 255;
    }
  }
  g.putImageData(img, 0, 0);
}

export default function render(shadow, ctx) {
  const d = (ctx && ctx.data) || {};
  const fragment = (ctx && ctx.cell && ctx.cell.fragment) || null;
  const opts = (ctx && ctx.cell && ctx.cell.options) || {};
  const themeName = d.theme || opts.theme || 'mono';
  const t = themeName === 'custom'
    ? customTheme(opts)
    : (THEMES[themeName] || THEMES.mono);

  if (d.error || !d.contours) {
    shadow.innerHTML = `<link rel="stylesheet" href="/static/style/spectra-widgets.css">
      <div class="w-body tp-msg"><span class="u-muted">${esc(d.error || 'No sheet yet.')}</span></div>
      <style>.tp-msg{display:flex;align-items:center;justify-content:center;height:100%;
        padding:6cqmin;text-align:center;line-height:1.4}</style>`;
    return;
  }

  const P = d.paths || {};
  const C = d.contours || {};
  const size = d.size || 1000;
  const weight = Number(opts.weight) > 0 ? Number(opts.weight) : 1;
  const sc = (t.scale || 1) * weight;
  const compact = fragment === 'map';

  const layer = (dd, cls, fill, stroke) => {
    if (!dd || (!fill && !stroke)) return '';
    const a = [`class="tp-${cls}"`, `d="${dd}"`, fill ? `fill="${fill}"` : 'fill="none"'];
    if (stroke) {
      a.push(`stroke="${stroke}"`, 'stroke-linecap="round"',
        'stroke-linejoin="round"', 'vector-effect="non-scaling-stroke"');
    }
    return `<path ${a.join(' ')}/>`;
  };

  const roads = ROADS.map((k) => layer(P[k], k, null, t.road)).join('');

  /* Index-contour heights, set along the line. The ground-coloured stroke
     under the glyphs knocks out the contour behind them, which is what a
     printed sheet does by breaking the line for the number. */
  const wantLabels = String(opts.show_labels ?? 'true') !== 'false';
  const labels = (wantLabels ? (d.labels || []) : []).map((l) => `
    <text class="tp-lbl" x="${l.x}" y="${l.y}"
          transform="rotate(${l.angle} ${l.x} ${l.y})"
          text-anchor="middle" dominant-baseline="central"
          fill="${t.index}" stroke="${t.ground}" paint-order="stroke"
          >${esc(l.text)}</text>`).join('');

  /* Triangulation mark on the high point, with its spot height. */
  const pk = d.peak;
  const wantPeak = String(opts.show_peak ?? 'true') !== 'false' && pk;
  const peak = wantPeak ? `
    <path class="tp-pk" d="M${pk.x} ${pk.y - 11}L${pk.x + 10} ${pk.y + 7}L${pk.x - 10} ${pk.y + 7}Z"
          fill="none" stroke="${t.text}" vector-effect="non-scaling-stroke"/>
    <text class="tp-pkt" x="${pk.x + 15}" y="${pk.y + 7}" fill="${t.text}"
          stroke="${t.ground}" paint-order="stroke"
          >${esc(pk.elev)}${esc(d.unit || 'm')}</text>` : '';

  const showBand = !compact && String(opts.show_label ?? 'true') !== 'false';
  const band = showBand ? `
    <div class="tp-band">
      <div class="tp-name">${esc(d.label)}</div>
      <div class="tp-sub">${esc(d.sub)}</div>
      <div class="tp-legend">${esc(d.legend)}</div>
    </div>` : '';

  const strokeRules = Object.keys(WEIGHT).map(
    (k) => `.tp-${k}{stroke-width:${(WEIGHT[k] * sc).toFixed(2)}cqmin}`).join('\n      ');

  shadow.innerHTML = `
    <link rel="stylesheet" href="/static/style/spectra-widgets.css">
    <div class="tp-wrap">
      ${d.relief ? '<canvas class="tp-relief"></canvas>' : ''}
      <svg class="tp-svg" viewBox="0 0 ${size} ${size}"
           preserveAspectRatio="xMidYMid slice" shape-rendering="geometricPrecision">
        ${layer(P.green, 'green', t.green, null)}
        ${layer(C.minor, 'ctr', null, t.contour)}
        ${layer(C.index, 'idx', null, t.index)}
        ${layer(P.water, 'water', t.water, null)}
        ${layer(P.stream, 'stream', null, t.stream || t.water)}
        ${layer(P.river, 'river', null, t.water)}
        ${layer(P.rail, 'rail', null, t.rail)}
        ${roads}
        ${labels}
        ${peak}
      </svg>
      ${band}
    </div>
    <style>
      :host { display: block; height: 100%; }
      .tp-wrap {
        position: relative; height: 100%; width: 100%;
        container-type: size; overflow: hidden;
        background: ${t.ground};
      }
      .tp-relief, .tp-svg {
        position: absolute; inset: 0; width: 100%; height: 100%; display: block;
      }
      /* The height field is deliberately coarse; letting the browser
         interpolate it is what turns it into smooth shading. */
      .tp-relief { object-fit: cover; opacity: 0.85; mix-blend-mode: multiply; }
      ${strokeRules}
      .tp-lbl {
        font-size: 2.5cqmin; font-weight: var(--fw-bold, 700);
        letter-spacing: 0.02em; stroke-width: 1.1cqmin; stroke-linejoin: round;
      }
      .tp-pk { stroke-width: ${(0.42 * sc).toFixed(2)}cqmin; }
      .tp-pkt {
        font-size: 2.9cqmin; font-weight: var(--fw-black, 800);
        stroke-width: 1.2cqmin; stroke-linejoin: round;
      }
      .tp-band {
        position: absolute; left: 0; right: 0; bottom: 0;
        padding: 3cqmin 4cqmin 3.2cqmin;
        background: ${t.ground}; border-top: 0.5cqmin solid ${t.text};
        text-align: center; color: ${t.text};
      }
      .tp-name {
        font-size: 7cqmin; line-height: 1.05;
        font-weight: var(--fw-black, 800); letter-spacing: 0.16em;
        text-transform: uppercase; white-space: nowrap;
        overflow: hidden; text-overflow: clip;
      }
      .tp-sub {
        margin-top: 1.2cqmin; font-size: 2.5cqmin; line-height: 1.1;
        letter-spacing: 0.12em; opacity: 0.85; white-space: nowrap;
      }
      .tp-legend {
        margin-top: 0.8cqmin; font-size: 2.2cqmin; line-height: 1.1;
        letter-spacing: 0.16em; opacity: 0.6; white-space: nowrap;
      }
      @container (max-width: 220px) {
        .tp-name { font-size: 9cqmin; letter-spacing: 0.1em; }
        .tp-sub, .tp-legend { display: none; }
      }
    </style>`;

  if (d.relief) {
    const canvas = shadow.querySelector('.tp-relief');
    if (canvas) paintRelief(canvas, d.relief);
  }
}
