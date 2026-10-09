#!/usr/bin/env node
// Render the README architecture diagram and the DESIGN.md order state diagram to PNG,
// light and dark, 1600 px wide, using real Mermaid in headless Chromium.
//
// The Mermaid source is read straight from README.md and docs/DESIGN.md (first ```mermaid
// block in each), so the images cannot drift from what the docs say.
//
// Setup (any scratch directory; nothing is added to this repo):
//   npm install --prefix /some/dir mermaid@11.4.1 playwright-core@1.49.1
//   MEDIA_NODE_MODULES=/some/dir node docs/media/tools/render-diagrams.mjs
// Chromium: CHROMIUM_PATH, default /opt/pw-browsers/chromium (the Playwright browser).

import { createRequire } from 'node:module';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const repo = path.resolve(here, '..', '..', '..');
const out = path.resolve(here, '..');

const nodeModules = process.env.MEDIA_NODE_MODULES;
if (!nodeModules) {
  console.error('Set MEDIA_NODE_MODULES to a directory where mermaid and playwright-core are installed.');
  process.exit(2);
}
const require = createRequire(path.join(nodeModules, 'noop.js'));
const { chromium } = require('playwright-core');
const mermaidBundle = path.join(nodeModules, 'node_modules', 'mermaid', 'dist', 'mermaid.min.js');

const WIDTH = 1600; // output width in px
const PAD = 56; // space around the diagram
const MAX_SCALE = 1.6; // never blow a small diagram up more than this
const FONT = '"Liberation Sans", "DejaVu Sans", Arial, sans-serif';

const THEMES = {
  light: {
    background: '#ffffff',
    vars: {
      background: '#ffffff',
      fontFamily: FONT,
      fontSize: '16px',
      primaryColor: '#e8f1ff',
      primaryBorderColor: '#2f6feb',
      primaryTextColor: '#0d1117',
      secondaryColor: '#f1f3f6',
      secondaryBorderColor: '#8c959f',
      secondaryTextColor: '#0d1117',
      tertiaryColor: '#f6f8fa',
      tertiaryBorderColor: '#8c959f',
      tertiaryTextColor: '#0d1117',
      lineColor: '#57606a',
      textColor: '#0d1117',
      mainBkg: '#e8f1ff',
      nodeBorder: '#2f6feb',
      clusterBkg: '#f6f8fa',
      clusterBorder: '#8c959f',
      edgeLabelBackground: '#ffffff',
      titleColor: '#0d1117',
      transitionColor: '#57606a',
      transitionLabelColor: '#0d1117',
      stateLabelColor: '#0d1117',
      stateBkg: '#e8f1ff',
      labelBackgroundColor: '#ffffff',
      compositeBackground: '#f6f8fa',
      altBackground: '#f6f8fa',
      specialStateColor: '#57606a',
    },
  },
  dark: {
    background: '#0d1117',
    vars: {
      background: '#0d1117',
      fontFamily: FONT,
      fontSize: '16px',
      primaryColor: '#12304f',
      primaryBorderColor: '#58a6ff',
      primaryTextColor: '#e6edf3',
      secondaryColor: '#1c2128',
      secondaryBorderColor: '#6e7681',
      secondaryTextColor: '#e6edf3',
      tertiaryColor: '#161b22',
      tertiaryBorderColor: '#6e7681',
      tertiaryTextColor: '#e6edf3',
      lineColor: '#8b949e',
      textColor: '#e6edf3',
      mainBkg: '#12304f',
      nodeBorder: '#58a6ff',
      clusterBkg: '#161b22',
      clusterBorder: '#6e7681',
      edgeLabelBackground: '#0d1117',
      titleColor: '#e6edf3',
      transitionColor: '#8b949e',
      transitionLabelColor: '#e6edf3',
      stateLabelColor: '#e6edf3',
      stateBkg: '#12304f',
      labelBackgroundColor: '#0d1117',
      compositeBackground: '#161b22',
      altBackground: '#161b22',
      specialStateColor: '#8b949e',
    },
  },
};

const DIAGRAMS = [
  { name: 'architecture', file: path.join(repo, 'README.md') },
  { name: 'order-state', file: path.join(repo, 'docs', 'DESIGN.md') },
];

function mermaidSource(file) {
  const match = readFileSync(file, 'utf8').match(/```mermaid\n([\s\S]*?)```/);
  if (!match) throw new Error(`no mermaid block in ${file}`);
  return match[1];
}

async function render(browser, source, theme, target) {
  const page = await browser.newPage({ viewport: { width: WIDTH, height: 800 }, deviceScaleFactor: 1 });
  page.on('pageerror', (err) => console.error('page error:', err.message));
  await page.setContent(
    `<!doctype html><meta charset="utf-8"><body style="margin:0;background:${theme.background}"><div id="stage"></div>`,
  );
  await page.addScriptTag({ path: mermaidBundle });
  const size = await page.evaluate(
    async ({ source, vars, pad, width, maxScale }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: 'strict',
        theme: 'base',
        themeVariables: vars,
        flowchart: { useMaxWidth: false, htmlLabels: true, curve: 'basis', padding: 14, wrappingWidth: 330 },
        state: { useMaxWidth: false },
      });
      const { svg } = await mermaid.render('diagram', source);
      const stage = document.getElementById('stage');
      stage.innerHTML = svg;
      await document.fonts.ready;
      const el = stage.querySelector('svg');
      const vb = el.viewBox.baseVal;
      const scale = Math.min(maxScale, (width - 2 * pad) / vb.width);
      el.removeAttribute('style');
      el.setAttribute('width', String(Math.round(vb.width * scale)));
      el.setAttribute('height', String(Math.round(vb.height * scale)));
      stage.style.cssText = `box-sizing:border-box;width:${width}px;padding:${pad}px;display:flex;justify-content:center`;
      return { scale, natural: [Math.round(vb.width), Math.round(vb.height)], height: stage.offsetHeight };
    },
    { source, vars: theme.vars, pad: PAD, width: WIDTH, maxScale: MAX_SCALE },
  );
  await page.setViewportSize({ width: WIDTH, height: size.height });
  await page.screenshot({ path: target, clip: { x: 0, y: 0, width: WIDTH, height: size.height } });
  await page.close();
  return size;
}

const browser = await chromium.launch({
  executablePath: process.env.CHROMIUM_PATH ?? '/opt/pw-browsers/chromium',
  args: ['--no-sandbox'],
});
try {
  for (const diagram of DIAGRAMS) {
    const source = mermaidSource(diagram.file);
    for (const [themeName, theme] of Object.entries(THEMES)) {
      const target = path.join(out, `${diagram.name}-${themeName}.png`);
      const size = await render(browser, source, theme, target);
      console.log(
        `${path.relative(repo, target)}: ${WIDTH}x${size.height}, diagram ${size.natural.join('x')} scaled x${size.scale.toFixed(2)}`,
      );
    }
  }
} finally {
  await browser.close();
}
