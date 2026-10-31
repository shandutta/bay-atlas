'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const {create} = require('../public/water-mask.js');

// Synthetic geometry only. Ring orientation deliberately matches for the
// outside and island: GeoJSON holes must not depend on winding direction.
const rectangle = (west,south,east,north) => [[west,south],[east,south],[east,north],[west,north],[west,south]];
const polygon = (bounds,holes=[]) => ({bounds,rings:[rectangle(...bounds),...holes.map(bounds=>rectangle(...bounds))]});
const payload = polygons => ({version:1,bounds:[-180,-80,180,80],polygons});
const origin = {bounds:[[6,-1],[-1,6]],width:700,height:700};

function context() {
  const saved = [], fills = [];
  let rings = [], current;
  return {
    fills,globalCompositeOperation:'source-over',globalAlpha:.35,fillStyle:'rgba(1,2,3,.5)',
    save() { saved.push({globalCompositeOperation:this.globalCompositeOperation,globalAlpha:this.globalAlpha,fillStyle:this.fillStyle}); },
    restore() { Object.assign(this,saved.pop()); },
    beginPath() { rings=[];current=null; },
    moveTo(x,y) { current=[[x,y]];rings.push(current); },
    lineTo(x,y) { assert.ok(current,'A ring must begin with moveTo');current.push([x,y]); },
    closePath() {},
    fill(rule) { fills.push({rings,rule,composite:this.globalCompositeOperation,alpha:this.globalAlpha,style:this.fillStyle}); }
  };
}

function pixel(lat,lng,view) {
  // Independently compute spherical Mercator y (the half-log sine form).
  const north=view.bounds[0][0],west=view.bounds[0][1],south=view.bounds[1][0],east=view.bounds[1][1];
  const mercator = latitude => {const sin=Math.sin(latitude*Math.PI/180);return .5*Math.log((1+sin)/(1-sin));};
  return [(lng-west)/(east-west)*view.width,(mercator(north)-mercator(lat))/(mercator(north)-mercator(south))*view.height];
}

function insideRing([x,y],ring) {
  let inside=false;
  for(let i=0,j=ring.length-1;i<ring.length;j=i++) {
    const [xi,yi]=ring[i],[xj,yj]=ring[j];
    if((yi>y)!==(yj>y) && x<(xj-xi)*(y-yi)/(yj-yi)+xi) inside=!inside;
  }
  return inside;
}

function erasedAt(ctx,lat,lng,view) {
  const point=pixel(lat,lng,view);
  return ctx.fills.some(fill=>fill.rings.reduce((parity,ring)=>parity!==insideRing(point,ring),false));
}

test('water membership preserves island holes regardless of ring winding', () => {
  const mask=create(payload([polygon([0,0,4,4],[[1,1,3,3]])]));
  assert.equal(mask.isWater(.5,.5),true);
  assert.equal(mask.isWater(2,2),false,'An island inside water must remain land');
  assert.equal(mask.isWater(2,4.5),false);
  assert.equal(mask.isWater(-.5,2),false);
});

test('overlapping water polygons form a union, including an overlapping island hole', () => {
  const mask=create(payload([polygon([0,0,4,4],[[1,1,3,3]]),polygon([2,2,5,5])]));
  const ctx=context();mask.erase(ctx,origin);
  for(const [lat,lng,water] of [[.5,.5,true],[1.5,1.5,false],[2.5,2.5,true],[3.5,3.5,true],[4.5,4.5,true],[5.5,5.5,false]]) {
    assert.equal(mask.isWater(lat,lng),water,`Unexpected membership at ${lat},${lng}`);
    assert.equal(erasedAt(ctx,lat,lng,origin),water,`Unexpected visible mask at ${lat},${lng}`);
  }
  assert.equal(ctx.fills.length,2,'Independent polygon fills prevent overlapping water from cancelling out');
  assert.ok(ctx.fills.every(fill=>fill.rule==='evenodd'),'Islands must use the even-odd fill rule');
});

test('shoreline classification stays precise on both sides of a narrow boundary', () => {
  const mask=create(payload([polygon([0,0,4,4],[[1,1,3,3]])]));
  assert.equal(mask.isWater(2,1-1e-7),true);
  assert.equal(mask.isWater(2,1+1e-7),false);
  assert.equal(mask.isWater(2,4-1e-7),true);
  assert.equal(mask.isWater(2,4+1e-7),false);
});

test('water is erased in full-resolution Mercator pixels from the completed raster bounds', () => {
  const mask=create(payload([polygon([2,20,8,40])]));
  const view={bounds:[[60,0],[0,10]],width:1000,height:600},ctx=context();
  mask.erase(ctx,view);
  const points=ctx.fills.flatMap(fill=>fill.rings.flat());
  for(const [lng,lat] of rectangle(2,20,8,40)) {
    const [x,y]=pixel(lat,lng,view);
    assert.ok(points.some(point=>Math.abs(point[0]-x)<1e-6 && Math.abs(point[1]-y)<1e-6),`Missing projected shoreline vertex ${lng},${lat}`);
  }
  assert.equal(erasedAt(ctx,30,5,view),true);
  assert.equal(erasedAt(ctx,45,5,view),false);
  assert.ok(Math.abs(pixel(40,2,view)[1]-200)>50,'This view must distinguish Mercator from linear latitude interpolation');
});

test('a retained raster uses its own origin across later map views and sizes', () => {
  const mask=create(payload([polygon([2,20,8,40])]));
  const oldView={bounds:[[60,0],[0,10]],width:1000,height:600};
  const movedView={bounds:[[50,1],[10,11]],width:600,height:1200};
  const oldContext=context(),movedContext=context(),repeatedContext=context();
  mask.erase(oldContext,oldView);
  mask.erase(movedContext,movedView);
  mask.erase(repeatedContext,oldView);
  assert.deepEqual(repeatedContext.fills,oldContext.fills,'Drawing a new view must not corrupt coordinates for the retained view');
  assert.notDeepEqual(movedContext.fills[0].rings,oldContext.fills[0].rings);
  assert.equal(erasedAt(movedContext,30,5,movedView),true);
});

test('offscreen polygons are culled while polygons enclosing the view still erase it', () => {
  const mask=create(payload([polygon([0,0,4,4]),polygon([20,20,30,30])]));
  const view={bounds:[[3,1],[1,3]],width:120,height:200},ctx=context();
  mask.erase(ctx,view);
  assert.equal(ctx.fills.length,1);
  assert.equal(erasedAt(ctx,2,2,view),true,'No visible vertices does not mean the water polygon is offscreen');
  const empty=context();mask.erase(empty,{bounds:[[15,10],[10,15]],width:100,height:100});
  assert.equal(empty.fills.length,0);
});

test('erasing removes all water opacity and restores the caller drawing state', () => {
  const mask=create(payload([polygon([0,0,4,4])])),ctx=context();
  mask.erase(ctx,origin);
  assert.equal(ctx.fills.length,1);
  assert.equal(ctx.fills[0].composite,'destination-out');
  assert.equal(ctx.fills[0].alpha,1,'Inherited low opacity must not leave color over water');
  assert.equal(ctx.globalCompositeOperation,'source-over');
  assert.equal(ctx.globalAlpha,.35);
  assert.equal(ctx.fillStyle,'rgba(1,2,3,.5)');
});

test('corrupt boundary data is rejected before any partial mask can be used', () => {
  const good=polygon([0,0,4,4]);
  for(const data of [null,{...payload([good]),version:2},payload([]),payload([good,{bounds:[5,5,6,6],rings:[]}]),
    payload([good,{bounds:[5,5,6,6],rings:[[[5,5],[6,5],[6,NaN],[5,5]]]}])]) {
    assert.throws(()=>create(data));
  }
  const mask=create(payload([good]));
  for(const [lat,lng] of [[NaN,2],[2,Infinity],[90,2],[undefined,2]]) assert.equal(mask.isWater(lat,lng),false);
});

test('a drawing error still restores the reusable canvas context', () => {
  const mask=create(payload([polygon([0,0,4,4])])),ctx=context();
  ctx.fill=()=>{throw new Error('Synthetic drawing failure');};
  assert.throws(()=>mask.erase(ctx,origin),/Synthetic drawing failure/);
  assert.equal(ctx.globalCompositeOperation,'source-over');
  assert.equal(ctx.globalAlpha,.35);
  assert.equal(ctx.fillStyle,'rgba(1,2,3,.5)');
});

test('bundled boundaries cover Bay, Pacific and lake water while preserving island interiors', () => {
  const data=JSON.parse(fs.readFileSync(path.join(__dirname,'../public/geo/bay-water.json'),'utf8'));
  const mask=create(data);
  // Public geographic landmarks; these are not home locations.
  for(const [label,lat,lng,water] of [
    ['Bay interior',37.8,-122.35,true],['Pacific near Ocean Beach',37.77,-122.525,true],
    ['Lake Merritt',37.8015,-122.259,true],['Berkeley waterfront',37.865,-122.325,true],
    ['Alameda island interior',37.765,-122.25,false],['Treasure Island interior',37.824,-122.37,false],
    ['Angel Island interior',37.86,-122.432,false],['San Francisco interior',37.7749,-122.4194,false],
    ['Oakland interior',37.8044,-122.2711,false]
  ]) assert.equal(mask.isWater(lat,lng),water,label);
});
