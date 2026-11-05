'use strict';
const $ = s => document.querySelector(s);
const KEY = 'alp-chat-history';
const EXAMPLES = ['Find Esri headquarters in Redlands', 'What is at 34.0601, -117.1964?', 'Route from Union Station to LAX', 'Elevation at 36.5785, -118.2923'];
let messages = [];
try { messages = JSON.parse(sessionStorage.getItem(KEY) || '[]'); } catch { messages = []; }
let selected = null, busy = false, viewEl = null, resultLayer = null, selLayer = null, C = null;

const save = () => { try { sessionStorage.setItem(KEY, JSON.stringify(messages.slice(-60))); } catch { /* storage unavailable */ } };
const uid = () => (crypto.randomUUID ? crypto.randomUUID() : Date.now() + '-' + Math.random().toString(16).slice(2)).replace(/[^\w-]/g, '');

function bubble(role, text, cls = '') {
  const d = document.createElement('div');
  d.className = `msg ${role} ${cls}`.trim(); d.textContent = text;
  $('#log').append(d); d.scrollIntoView({ block: 'end' });
  return d;
}
function renderAll() {
  $('#log').replaceChildren();
  messages.forEach(m => bubble(m.role, m.content, m.error ? 'error' : ''));
  $('#examples').hidden = messages.length > 0;
}

function setBusy(on) {
  busy = on;
  ['#input', '#send'].forEach(s => { $(s).disabled = on; });
  $('#form').setAttribute('aria-busy', on);
}

// ---------- map (ArcGIS Maps SDK for JavaScript 5.1, <arcgis-map> component) ----------
const SYM = {
  geocode: { style: 'circle', color: '#3284FF', size: 12 },
  reverse: { style: 'square', color: '#8A4FFF', size: 12 },
  elevation: { style: 'diamond', color: '#1E9E5A', size: 13 },
  'route-start': { style: 'circle', color: '#1E9E5A', size: 13 },
  'route-end': { style: 'circle', color: '#D93025', size: 13 },
  'route-stop': { style: 'circle', color: '#F59E0B', size: 10 }
};
const esc = s => String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
const marker = (o, w = 1.5) => new C.SimpleMarkerSymbol({ style: o.style, color: o.color, size: o.size, outline: { color: '#ffffff', width: w } });

function toGraphics(geojson) {
  return geojson.features.map(f => {
    const p = f.properties || {}, g = f.geometry;
    const rows = Object.entries(p).filter(([k, v]) => !['kind', 'title'].includes(k) && v != null && v !== '').map(([k, v]) => `<b>${esc(k)}</b>: ${esc(v)}`).join('<br>');
    const popupTemplate = new C.PopupTemplate({ title: esc(p.title || p.kind), content: rows || ' ' });
    if (g.type === 'Point') {
      return new C.Graphic({ geometry: new C.Point({ longitude: g.coordinates[0], latitude: g.coordinates[1] }), symbol: marker(SYM[p.kind] || SYM.geocode), attributes: p, popupTemplate });
    }
    return new C.Graphic({
      geometry: new C.Polyline({ paths: [g.coordinates], spatialReference: { wkid: 4326 } }),
      symbol: new C.SimpleLineSymbol({ color: '#4285F4', width: 5, cap: 'round', join: 'round' }), attributes: p, popupTemplate
    });
  });
}

async function drawResult(geojson, append) {
  if (!resultLayer || !geojson || !geojson.features || !geojson.features.length) return;   // text-only: keep map state
  const gs = toGraphics(geojson);
  if (!append) resultLayer.removeAll();
  resultLayer.addMany(gs);
  const all = resultLayer.graphics.toArray();
  try {
    if (all.length === 1 && all[0].geometry.type === 'point') await viewEl.goTo({ target: all[0], zoom: 15 });
    else await viewEl.goTo(all);
  } catch { /* goTo interrupted by another navigation */ }
}

function setSelected(pt) {
  selected = pt;
  $('#sel').hidden = !pt;
  if (pt) $('#selText').textContent = `Selected: ${pt.y.toFixed(5)}, ${pt.x.toFixed(5)}`;
  if (selLayer) {
    selLayer.removeAll();
    if (pt) selLayer.add(new C.Graphic({ geometry: new C.Point({ longitude: pt.x, latitude: pt.y }), symbol: new C.SimpleMarkerSymbol({ style: 'cross', color: '#D93025', size: 16, outline: { color: '#D93025', width: 2 } }) }));
  }
}

function mapMessage(t) { const m = $('#mapMsg'); m.classList.toggle('show', !!t); m.textContent = t || ''; }

async function initMap() {
  if (typeof $arcgis === 'undefined') return mapMessage('The ArcGIS Maps SDK 5.1 could not load. Check your network connection; chat still works.');
  let key;
  try {
    const r = await fetch('/api/arcgis-token'), j = await r.json();
    if (!r.ok) throw new Error(j.error && j.error.message);
    key = j.apiKey;
  } catch (e) { return mapMessage(`Map unavailable: ${e.message || 'no browser map API key'} Chat still works.`); }
  try {
    viewEl = document.querySelector('arcgis-map');
    if (!(window.esriConfig && window.esriConfig.apiKey)) {      // fallback if the key was not set before the SDK loaded
      const config = await $arcgis.import('@arcgis/core/config.js');
      config.apiKey = key;
      viewEl.basemap = (window.ARCGIS_CONFIG && window.ARCGIS_CONFIG.basemap) || 'arcgis/navigation';
    }
    const [Graphic, GraphicsLayer, Point, Polyline, SimpleMarkerSymbol, SimpleLineSymbol, PopupTemplate] = await $arcgis.import([
      '@arcgis/core/Graphic.js', '@arcgis/core/layers/GraphicsLayer.js', '@arcgis/core/geometry/Point.js', '@arcgis/core/geometry/Polyline.js',
      '@arcgis/core/symbols/SimpleMarkerSymbol.js', '@arcgis/core/symbols/SimpleLineSymbol.js', '@arcgis/core/PopupTemplate.js']);
    C = { Graphic, GraphicsLayer, Point, Polyline, SimpleMarkerSymbol, SimpleLineSymbol, PopupTemplate };
    await viewEl.viewOnReady();
    resultLayer = new GraphicsLayer(); selLayer = new GraphicsLayer();
    viewEl.map.addMany([resultLayer, selLayer]);
    viewEl.addEventListener('arcgisViewClick', e => {
      if (busy) return;
      const p = e.detail.mapPoint;
      setSelected({ x: p.longitude, y: p.latitude });
    });
  } catch (e) { mapMessage('The map could not start: ' + (e && e.message || 'check the map API key.')); }
}

// Web Mercator extent -> WGS84 degrees for the server's searchExtent
const mercToLon = x => x / 6378137 * 180 / Math.PI;
const mercToLat = y => (2 * Math.atan(Math.exp(y / 6378137)) - Math.PI / 2) * 180 / Math.PI;
function mapContext() {
  const ctx = { selectedPoint: selected };
  try {
    const e = viewEl && viewEl.extent;
    if (e) {
      const merc = e.spatialReference && (e.spatialReference.isWebMercator || e.spatialReference.wkid === 3857);
      ctx.mapExtent = merc
        ? { xmin: mercToLon(e.xmin), ymin: mercToLat(e.ymin), xmax: mercToLon(e.xmax), ymax: mercToLat(e.ymax) }
        : { xmin: e.xmin, ymin: e.ymin, xmax: e.xmax, ymax: e.ymax };
    }
  } catch { /* extent is optional context */ }
  return ctx;
}

// ---------- chat ----------
const TOOL_STATUS = { find_address_candidates: 'Searching for places…', reverse_geocode: 'Looking up the address…', solve_route: 'Solving the route…', elevation_at_locations: 'Checking elevation…' };

async function send(text) {
  text = text.trim();
  if (!text || busy) return;
  const history = messages.filter(m => !m.error).map(({ role, content, point }) => ({ role, content, point }));
  messages.push({ role: 'user', content: text }); save();
  bubble('user', text); $('#examples').hidden = true; $('#input').value = ''; autosize();
  setBusy(true);
  const pending = bubble('assistant', 'Thinking…', 'pending');
  const requestId = uid();
  let es;
  try {
    es = new EventSource(`/api/chat/stream?requestId=${encodeURIComponent(requestId)}`);
    es.addEventListener('status', e => { pending.textContent = JSON.parse(e.data).text; });
    es.addEventListener('tool_call', e => { pending.textContent = TOOL_STATUS[JSON.parse(e.data).name] || 'Working…'; });
    es.addEventListener('done', () => es.close());
    es.onerror = () => es.close();
  } catch { /* status stream is optional */ }
  try {
    const r = await fetch('/api/chat', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ message: text, history: history.slice(-40), context: mapContext(), requestId }) });
    const j = await r.json().catch(() => ({ error: { message: 'The server sent an unexpected response.' } }));
    if (!r.ok || j.error) throw new Error((j.error && j.error.message) || 'Request failed.');
    pending.remove();
    const first = j.geojson && j.geojson.features[0];
    const point = first && first.geometry.type === 'Point' ? { x: first.geometry.coordinates[0], y: first.geometry.coordinates[1] } : null;
    messages.push({ role: 'assistant', content: j.text, point }); save();
    const b = bubble('assistant', j.text);
    addAlternatives(b, j.meta);
    await drawResult(j.geojson, j.meta && j.meta.append);
  } catch (e) {
    pending.remove();
    messages.push({ role: 'assistant', content: e.message, error: true }); save();
    bubble('assistant', e.message, 'error');
  } finally { if (es) es.close(); setBusy(false); $('#input').focus(); }
}

// Explain the geocode choice: list other candidates; picking one redraws the map from backend-provided coordinates.
function addAlternatives(b, meta) {
  const g = meta && meta.geocode;
  if (!g || !g.candidates || g.candidates.length < 2) return;
  const d = document.createElement('details'); d.className = 'alts';
  const s = document.createElement('summary'); s.textContent = `Chose the top of ${g.count} matches. Other options`; d.append(s);
  g.candidates.slice(1).forEach(c => {
    const x = document.createElement('button'); x.type = 'button'; x.textContent = c.address || 'Unnamed match';
    x.onclick = () => drawResult({ type: 'FeatureCollection', features: [{ type: 'Feature', geometry: { type: 'Point', coordinates: [c.x, c.y] }, properties: { kind: 'geocode', title: c.address, address: c.address, score: c.score } }] }, false);
    d.append(x);
  });
  b.append(d);
}

function autosize() { const t = $('#input'); t.style.height = 'auto'; t.style.height = Math.min(t.scrollHeight, 140) + 'px'; }

$('#form').addEventListener('submit', e => { e.preventDefault(); send($('#input').value); });
$('#input').addEventListener('input', autosize);
$('#input').addEventListener('keydown', e => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send($('#input').value); } });
$('#selClear').addEventListener('click', () => setSelected(null));
$('#clear').addEventListener('click', () => { if (busy) return; messages = []; save(); renderAll(); if (resultLayer) resultLayer.removeAll(); setSelected(null); });
EXAMPLES.forEach(t => { const b = document.createElement('button'); b.type = 'button'; b.textContent = t; b.onclick = () => send(t); $('#examples').append(b); });
renderAll();
initMap();