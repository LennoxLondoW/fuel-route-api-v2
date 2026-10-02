// Demo client for the Fuel Route API.
// All the planning happens on the server; this file only calls the API and draws the JSON it returns.

const $ = id => document.getElementById(id);

// Leaflet map with OpenStreetMap tiles. The canvas renderer stays fast with 30k-point routes.
const map = L.map('map', { preferCanvas: true }).setView([39.5, -98.35], 4);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', { attribution: '© OpenStreetMap' }).addTo(map);
const layer = L.layerGroup().addTo(map);

// Escape text before putting it into HTML (station names come from data, never trust them).
const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const usd = n => n == null ? '—' : '$' + n.toFixed(2);

// Decode a Google encoded polyline into [[lat, lon], ...].
// The API returns the route this way by default: a coast-to-coast line is ~0.13 MB encoded
// against ~0.81 MB as a point list, and decoding 35k points here takes a few milliseconds.
function decodePolyline(str, precision = 5) {
  const factor = 10 ** precision, n = str.length, points = [];
  let i = 0, lat = 0, lon = 0;
  while (i < n) {
    let shift = 0, result = 0, b;
    do { b = str.charCodeAt(i++) - 63; result |= (b & 31) << shift; shift += 5; } while (b >= 32);
    lat += (result & 1) ? ~(result >> 1) : result >> 1;
    shift = 0; result = 0;
    do { b = str.charCodeAt(i++) - 63; result |= (b & 31) << shift; shift += 5; } while (b >= 32);
    lon += (result & 1) ? ~(result >> 1) : result >> 1;
    points.push([lat / factor, lon / factor]);
  }
  return points;
}

// The route line, whichever geometry format the API sent.
const routeLine = route => route.points || decodePolyline(route.polyline, route.precision);

// Call the API. Returns the parsed JSON, or throws an Error with the API's error message.
async function planRoute(key, from, to) {
  const url = '/api/v1/route/?' + new URLSearchParams({ from, to });
  const res = await fetch(url, { headers: { 'X-API-Key': key, 'Accept': 'application/json' } });
  const body = await res.json();
  if (!res.ok) {
    const e = body.error || {};
    const fieldErrors = e.details ? ' ' + Object.entries(e.details).map(([k, v]) => `${k}: ${v}`).join('; ') : '';
    throw new Error((e.message || `HTTP ${res.status}`) + fieldErrors);
  }
  return body;
}

// Draw the route line and every marker from the response.
function draw(data) {
  layer.clearLayers();
  L.polyline(routeLine(data.route), { color: '#0b6bcb', weight: 5, smoothFactor: 2 }).addTo(layer);

  const stopsBySeq = new Map(data.stops.map(s => [s.sequence, s]));
  for (const m of data.markers) {
    if (m.type === 'fuel_stop') {
      const s = stopsBySeq.get(m.sequence);
      const icon = L.divIcon({ className: '', html: `<div class="pin">${m.sequence}</div>`, iconSize: [24, 24] });
      L.marker([m.lat, m.lon], { icon }).addTo(layer).bindPopup(
        `<b>${esc(s.name)}</b><br>${esc(s.city)}, ${esc(s.state)}<br>` +
        `$${s.price_per_gallon.toFixed(3)}/gal · buy ${s.gallons.toFixed(1)} gal = ${usd(s.cost_usd)}<br>Mile ${Math.round(s.mile)}`);
    } else {
      L.marker([m.lat, m.lon]).addTo(layer).bindPopup((m.type === 'start' ? 'Start: ' : 'Finish: ') + esc(m.label));
    }
  }
  if (data.route.bounds) map.fitBounds(data.route.bounds, { padding: [30, 30], animate: false });
}

// Render the summary card and the stops table.
function summarize(data) {
  const s = data.summary, t = data.meta.timings_ms;
  const rows = data.stops.map(r =>
    `<tr><td>${r.sequence}</td><td>${esc(r.name)}<br><small>${esc(r.city)}, ${esc(r.state)}</small></td>` +
    `<td>${Math.round(r.mile)}</td><td>${r.price_per_gallon.toFixed(3)}</td><td>${r.gallons.toFixed(1)}</td><td>${usd(r.cost_usd)}</td></tr>`).join('');
  return `
    <div class="card"><div class="big">${usd(s.total_cost_usd)}</div>total fuel cost
      <div class="row"><span>Distance</span><b>${Math.round(s.distance_miles)} mi</b></div>
      ${s.detour_miles > 0 ? `<div class="row"><span>Detours to pumps</span><b>+${Math.round(s.detour_miles)} mi</b></div>` : ''}
      <div class="row"><span>Fuel used</span><b>${s.fuel_used_gallons.toFixed(1)} gal</b></div>
      <div class="row"><span>Fuel-ups</span><b>${s.fuel_stops}</b></div>
      <div class="row"><span>Starting tank (${s.starting_tank.gallons.toFixed(1)} gal)</span><b>${usd(s.starting_tank.cost_usd)}</b></div>
      <div class="row"><span>Server: geocode / route / plan</span><b>${t.geocode} / ${t.route} / ${t.plan} ms</b></div>
    </div>
    ${rows ? `<div class="card"><table><tr><th>#</th><th>Station</th><th>Mile</th><th>$/gal</th><th>Gal</th><th>Cost</th></tr>${rows}</table></div>`
           : '<div class="card">No fuel stop needed — the trip fits in one tank.</div>'}`;
}

// Form submit: call the API, then draw and summarize (or show the error).
$('form').addEventListener('submit', async ev => {
  ev.preventDefault();
  const btn = $('go'), out = $('out');
  btn.disabled = true;
  out.innerHTML = '<div class="card">Planning…</div>';
  try {
    const data = await planRoute($('key').value.trim(), $('from').value, $('to').value);
    draw(data);
    out.innerHTML = summarize(data);
  } catch (e) {
    out.innerHTML = `<div class="card err">${esc(e.message)}</div>`;
  }
  btn.disabled = false;
});
