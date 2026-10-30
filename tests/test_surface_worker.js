'use strict';
// Run with node --test tests/test_surface_worker.js.
const assert = require('node:assert/strict');
const {spawnSync} = require('node:child_process');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../public/surface-worker.js'), 'utf8');

function harness() {
  const output = [], pending = [];
  let clock = 0;
  const context = {self:{postMessage:message => output.push(message)}, Date, Math, Number, Map, Float32Array, Float64Array,
    performance:{now:() => clock += 8}, setTimeout:callback => pending.push(callback)};
  vm.createContext(context);
  vm.runInContext(source, context);
  return {send:data => context.self.onmessage({data}), output, pending};
}
function sales(price = 2000000) {
  return Array.from({length:8}, (_, i) => ({lat:37.82 + i * .0001, lng:-122.25, price, sqft:2500, beds:3,
    sold_date:'2026-10-15', time_weight:1, address:'Address never needed by worker'}));
}
function packed(rows) {
  return new Float64Array(rows.flatMap(s => [s.lat,s.lng,s.price,s.sqft,s.beds,Date.parse(s.sold_date),s.time_weight]));
}
const metadata = {type:'data',asOf:'2026-10-29',oldestDate:'2024-10-29'};
const render = (id, points = [[37.82,-122.25]]) => ({type:'render',id,points,sqft:2500,beds:3});

test('compact transferred inputs preserve scalar estimates and date filtering', async () => {
  const rows = [...sales(), ...sales(9000000).map(s => ({...s,sold_date:'2024-10-28'}))];
  const legacy = harness(), compact = harness();
  await legacy.send({...metadata,sales:rows});
  await compact.send({...metadata,packed:packed(rows)});
  await legacy.send(render(1));
  await compact.send(render(1));
  assert.deepEqual(Array.from(compact.output[0].values), Array.from(legacy.output[0].values));
  assert.equal(compact.output[0].values[0], 800);
});

test('pan cancellation interrupts a render before all its samples finish', async () => {
  const worker = harness();
  await worker.send({...metadata,packed:packed(sales())});
  const obsolete = worker.send(render(1, Array.from({length:1000}, () => [37.82,-122.25])));
  assert.equal(worker.pending.length, 1, 'Rendering yields during a large view');
  await worker.send({type:'cancel'});
  worker.pending.shift()();
  await obsolete;
  assert.deepEqual(worker.output, [], 'A cancelled view must never be published');
  await worker.send(render(2));
  assert.equal(worker.output[0].id, 2);
});

test('new data invalidates a pending render and takes effect on the next view', async () => {
  const worker = harness();
  await worker.send({...metadata,packed:packed(sales())});
  const obsolete = worker.send(render(1, Array.from({length:1000}, () => [37.82,-122.25])));
  await worker.send({...metadata,packed:packed(sales(2500000))});
  worker.pending.shift()();
  await obsolete;
  await worker.send(render(2));
  assert.equal(worker.output.length, 1);
  assert.equal(worker.output[0].id, 2);
  assert.equal(worker.output[0].values[0], 1000);
});

test('transferred sample coordinates produce the same surface as nested points', async () => {
  const worker = harness();
  await worker.send({...metadata,packed:packed(sales())});
  const points = [[37.82,-122.25],[37.825,-122.25],[38.5,-122.25]];
  await worker.send(render(1, points));
  await worker.send(render(2, new Float64Array(points.flat())));
  assert.deepEqual(Array.from(worker.output[1].values),Array.from(worker.output[0].values));
});

test('cached similarity updates when size or bedrooms change and preserves distance weighting', async () => {
  const rows = Array.from({length:80}, (_, i) => ({
    id:String(i + 1), address:`${i + 1} Example Street`, city:'Oakland',
    lat:37.82 + (i % 10 - 4.5) * .004, lng:-122.25 + (Math.floor(i / 10) - 3.5) * .004,
    price:(620 + (i * 113 % 500)) * (2000 + (i % 5) * 250), sqft:2000 + (i % 5) * 250,
    beds:2 + i % 5, sold_date:'2026-10-15', time_weight:1,
    source_url:`https://www.zillow.com/homedetails/${i + 1}_zpid/`, home_type:'singleFamily'
  }));
  const queries = [[37.82,-122.25,2500,3],[37.843,-122.25,2000,2],[37.845,-122.21,3000,5],[37.82,-122.25,2500,3]];
  const result = spawnSync('python3',['-c',`
import json, sys
from datetime import date
from model import estimate
payload = json.load(sys.stdin)
print(json.dumps([estimate(payload['rows'], lat, lng, sqft=sqft, beds=beds, as_of=date(2026,10,29))['estimate_ppsf'] for lat,lng,sqft,beds in payload['queries']]))
`],{cwd:path.join(__dirname,'..'),input:JSON.stringify({rows,queries}),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  const expected = JSON.parse(result.stdout), worker = harness();
  await worker.send({...metadata,packed:packed(rows)});
  for (const [i,[lat,lng,sqft,beds]] of queries.entries()) {
    await worker.send({...render(i,[[lat,lng]]),sqft,beds});
    assert.ok(Math.abs(worker.output[i].values[0] - expected[i]) < .01, `Query ${i} must match the backend`);
  }
});

test('bedroom selection changes shading and matches Python for raw and packed rows', async () => {
  const rows = [[2,700],[2,750],[3,1000],[3,1050],[4,600],[4,650],[5,800],[8,850]].map(([beds,ppsf], index) => ({
    id:String(index+1), address:`${index+1} Test St`, city:'Oakland', lat:37.82, lng:-122.25,
    price:ppsf*2500, sqft:2500, beds, sold_date:'2026-10-15', time_weight:1,
    source_url:`https://www.zillow.com/homedetails/${index+1}_zpid/`, home_type:'singleFamily'
  }));
  const subjects = [2,3,4,5,10];
  const result = spawnSync('python3', ['-c', `
import json, sys
from datetime import date
from model import estimate
rows = json.load(sys.stdin)
print(json.dumps([estimate(rows, 37.82, -122.25, beds=beds, as_of=date(2026,10,29))["estimate_ppsf"] for beds in [2,3,4,5,10]]))
`], {cwd:path.join(__dirname, '..'), input:JSON.stringify(rows), encoding:'utf8'});
  assert.equal(result.status, 0, result.stderr);
  const backend = JSON.parse(result.stdout);
  for (const usePacked of [false, true]) {
    const worker = harness();
    await worker.send({...metadata, ...(usePacked ? {packed:packed(rows)} : {sales:rows})});
    for (let i = 0; i < subjects.length; i++) {
      await worker.send({...render(i), beds:subjects[i]});
      assert.ok(Math.abs(worker.output[i].values[0] - backend[i]) < .01,
        `Subject ${subjects[i]} bedrooms must match the backend; packed=${usePacked}`);
    }
    assert.notEqual(worker.output[0].values[0], worker.output[1].values[0]);
    assert.ok(worker.output[2].values[0] < worker.output[1].values[0],
      'Selecting more bedrooms need not increase the estimate when matching comps are cheaper');
    assert.equal(worker.output[3].values[0], worker.output[4].values[0], 'Five or more bedrooms share one bucket');
  }
});
