'use strict';
// Optional, deterministic synthetic benchmark; no real home records are read.
// node tests/benchmark_surface.js [path/to/baseline-worker.js]
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {performance} = require('node:perf_hooks');

let seed = 42;
const random = () => ((seed = (Math.imul(seed, 1664525) + 1013904223) >>> 0) / 4294967296);
const centers = [[37.82,-122.25],[37.94,-122.10],[38.03,-122.53],[38.30,-122.29],[37.77,-122.43],
  [37.55,-122.30],[37.33,-121.90],[38.25,-122.04],[38.44,-122.71]];
const sales = Array.from({length:21050},(_,i) => {
  const center = centers[i % centers.length], sqft = 2000 + Math.floor(random() * 1001);
  return {lat:center[0] + (random() - .5) * .16,lng:center[1] + (random() - .5) * .20,
    price:sqft * (450 + random() * 1000),sqft,beds:2 + Math.floor(random() * 4),
    sold_date:'2026-10-15',time_weight:1};
});
const points = Array.from({length:1800},(_,i) => [37.74 + Math.floor(i / 60) * .006,-122.42 + (i % 60) * .006]);

async function benchmark(source) {
  const output = [];
  const context = {self:{postMessage:data=>output.push(data)},Date,Math,Number,Map,Float32Array,Float64Array,performance,setTimeout};
  vm.createContext(context); vm.runInContext(source,context);
  await context.self.onmessage({data:{type:'data',sales,asOf:'2026-10-29',oldestDate:'2024-10-29'}});
  const times = [];
  for (let i = 0; i < 4; i++) {
    const start = performance.now();
    await context.self.onmessage({data:{type:'render',id:i,points,sqft:2500,beds:3}});
    if (i) times.push(performance.now() - start);
  }
  return {median_ms:Math.round(times.sort((a,b)=>a-b)[1]),values:output.at(-1).values};
}
(async () => {
  const current = await benchmark(fs.readFileSync(path.join(__dirname,'../public/surface-worker.js'),'utf8'));
  const summary = {synthetic_sales:sales.length,surface_samples:points.length,measured_runs:3,current_median_ms:current.median_ms};
  if (process.argv[2]) {
    const baseline = await benchmark(fs.readFileSync(process.argv[2],'utf8'));
    assert.equal(current.values.length,baseline.values.length);
    let maxDifference = 0;
    for (let i=0;i<current.values.length;i++) maxDifference=Math.max(maxDifference,Math.abs(current.values[i]-baseline.values[i]));
    assert.ok(maxDifference < .001,'Optimization must preserve scalar estimates');
    Object.assign(summary,{baseline_median_ms:baseline.median_ms,speedup:Number((baseline.median_ms/current.median_ms).toFixed(2)),max_scalar_difference:maxDifference});
  }
  console.log(JSON.stringify(summary,null,2));
})().catch(error=>{console.error(error.message);process.exitCode=1;});
