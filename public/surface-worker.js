'use strict';
// Continuous scalar estimates. Colors encode $/sqft, never listing density.
let bins = new Map(), weights = [1, .8, .55, .3, .15], generation = 0, subjectKey = '';
const cell = .025, radians = Math.PI / 180;
const maxLatDistance = 3 / 6371 / radians;
function key(lat, lng) { return `${Math.floor(lat / cell)},${Math.floor(lng / cell)}`; }
function quantile(items, q) {
  const sorted = items.slice().sort((a, b) => a.value - b.value);
  const target = sorted.reduce((s, a) => s + a.weight, 0) * q;
  let sum = 0;
  for (const item of sorted) { sum += item.weight; if (sum >= target) return item.value; }
  return sorted.length ? sorted[sorted.length - 1].value : 0;
}
function bedroomWeight(compBeds, subjectBeds) {
  // Soft comp matching, not a price premium. Keep in sync with model.py.
  if (subjectBeds >= 3 && compBeds === 2) return .75;
  return .9 ** Math.abs(Math.min(5, compBeds) - Math.min(5, subjectBeds));
}
function prepareSubject(sqft, beds) {
  const next = `${sqft},${beds}`;
  if (next === subjectKey) return;
  subjectKey = next;
  for (const bucket of bins.values()) for (const sale of bucket) {
    // Size and bedroom similarity depend on the subject, not the map pixel.
    sale.subjectWeight = sale.timeWeight * Math.exp(-.5 * (Math.log(sale.sqft / sqft) / .25) ** 2) * bedroomWeight(sale.beds, beds);
  }
}
function estimate(lat, lng, sqft, beds) {
  const nearby = [], bx = Math.floor(lat / cell), by = Math.floor(lng / cell), cosLat = Math.cos(lat * radians);
  const counts = [0, 0, 0], radii = [.75, 1.5, 2.25, 3];
  for (let i = bx - 2; i <= bx + 2; i++) for (let j = by - 2; j <= by + 2; j++) {
    const bucket = bins.get(`${i},${j}`); if (!bucket) continue;
    for (const sale of bucket) {
      // Latitude alone gives a conservative rejection before costly trig calls.
      if (Math.abs(sale.lat - lat) > maxLatDistance) continue;
      const dlat = (sale.lat - lat) * radians, dlng = (sale.lng - lng) * radians;
      const a = Math.sin(dlat / 2) ** 2 + cosLat * sale.cosLat * Math.sin(dlng / 2) ** 2;
      const distance = 6371 * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
      if (distance <= 3) {
        nearby.push({sale, distance});
        for (let r = 0; r < counts.length; r++) if (distance <= radii[r]) counts[r]++;
      }
    }
  }
  if (nearby.length < 3) return null;
  let radius = 3;
  for (let r = 0; r < counts.length; r++) if (counts[r] >= 8) { radius = radii[r]; break; }
  const items = [];
  for (const {sale, distance} of nearby) {
    if (distance > radius) continue;
    const weight = sale.subjectWeight * Math.exp(-.5 * (distance / (radius / 2)) ** 2);
    if (weight > 0) items.push({value: sale.logPrice, weight});
  }
  const total = items.reduce((s, a) => s + a.weight, 0), sumSquares = items.reduce((s, a) => s + a.weight ** 2, 0);
  const effective = total ** 2 / sumSquares;
  if (items.length < 3 || effective < 2) return null;
  const median = quantile(items, .5), mad = quantile(items.map(a => ({value: Math.abs(a.value - median), weight:a.weight})), .5);
  const clamp = Math.max(.18, 3 * 1.4826 * mad);
  const value = Math.exp(items.reduce((s, a) => s + Math.max(median - clamp, Math.min(median + clamp, a.value)) * a.weight, 0) / total);
  // Opacity shows support continuously while hue carries the scalar price.
  return [value, Math.min(1, .24 + (effective - 2) / 10)];
}
self.onmessage = async ({data}) => {
  const current = ++generation;
  if (data.type === 'cancel') return;
  if (data.type === 'data') {
    const asOf = Date.parse(data.asOf || new Date().toISOString().slice(0,10));
    const cutoffDate = new Date(asOf); cutoffDate.setUTCFullYear(cutoffDate.getUTCFullYear() - 2);
    const cutoff = data.oldestDate ? Date.parse(data.oldestDate) : +cutoffDate;
    weights = data.weights || weights; bins = new Map(); subjectKey = '';
    const packed = data.packed;
    const count = packed ? Math.floor(packed.length / 7) : (data.sales || []).length;
    for (let i = 0; i < count; i++) {
      const raw = packed ? null : data.sales[i], offset = i * 7;
      const s = packed ? {lat:packed[offset],lng:packed[offset+1],price:packed[offset+2],sqft:packed[offset+3],beds:packed[offset+4]} : {lat:Number(raw.lat),lng:Number(raw.lng),price:Number(raw.price),sqft:Number(raw.sqft),beds:Number(raw.beds)};
      const soldDate = packed ? packed[offset+5] : Date.parse(raw.sold_date), age = Math.floor((asOf - soldDate) / 86400000);
      if (!Number.isFinite(age) || age < 0 || soldDate < cutoff || !Number.isFinite(s.lat) || !Number.isFinite(s.lng) || !(s.price > 0) || !(s.sqft >= 2000 && s.sqft <= 3000) || !(s.beds >= 2)) continue;
      s.timeWeight = Number(packed ? packed[offset+6] : raw.time_weight) || weights[age <= 30 ? 0 : age <= 90 ? 1 : age <= 183 ? 2 : age <= 365 ? 3 : 4];
      s.cosLat = Math.cos(s.lat * radians); s.logPrice = Math.log(s.price / s.sqft);
      const k = key(s.lat, s.lng); if (!bins.has(k)) bins.set(k, []); bins.get(k).push(s);
    }
    return;
  }
  if (data.type === 'render') {
    prepareSubject(data.sqft, data.beds);
    const packedPoints = data.points instanceof Float64Array;
    const count = packedPoints ? data.points.length / 2 : data.points.length;
    const values = new Float32Array(count * 2);
    let chunkStart = performance.now();
    for (let i = 0; i < count; i++) {
      if (current !== generation) return;
      const result = packedPoints ? estimate(data.points[i * 2], data.points[i * 2 + 1], data.sqft, data.beds)
        : estimate(data.points[i][0], data.points[i][1], data.sqft, data.beds);
      if (result) { values[i * 2] = result[0]; values[i * 2 + 1] = result[1]; }
      // Yield so a pan, zoom or data refresh can cancel obsolete calculations.
      if ((i + 1) % 64 === 0 && performance.now() - chunkStart >= 8) {
        await new Promise(resolve => setTimeout(resolve, 0));
        chunkStart = performance.now();
      }
    }
    if (current !== generation) return;
    self.postMessage({id:data.id, values, width:data.width, height:data.height, origin:data.origin}, [values.buffer]);
  }
};
