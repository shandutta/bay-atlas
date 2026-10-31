'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,'../public/app.js'),'utf8');
function point(x,y) {
  if (Array.isArray(x)) [x,y] = x;
  return {x,y,add(other) { const p = Array.isArray(other) ? point(other) : other; return point(x+p.x,y+p.y); },
    multiplyBy(value) { return point(x*value,y*value); },divideBy(value) { return point(x/value,y/value); },
    ceil() { return point(Math.ceil(x),Math.ceil(y)); }};
}
function harness() {
  const timers = new Map(), handlers = new Map(), layers = new Set(), messages = [], paints = [];
  const elements = {'map-loading':{hidden:true},'map-notice':{hidden:true},'map-notice-text':{},'sqft':{value:'2500'},'beds':{value:'3'},'toggle-surface':{disabled:false,setAttribute(key,value) {this[key]=value;}}};
  let nextTimer = 0;
  const drawing = {createImageData:(width,height)=>({data:new Uint8ClampedArray(width*height*4)}),
    putImageData() {},clearRect() {},drawImage(...args) { paints.push(args); }};
  const canvas = {style:{},classes:new Set(),width:0,height:0,getContext:()=>drawing};
  const map = {offset:point(0,0),getSize:()=>point(100,60),distance:()=>100,
    containerPointToLatLng(value) { const p = Array.isArray(value) ? point(value) : value; return {lat:40-(p.y+this.offset.y)/1000,lng:-120+(p.x+this.offset.x)/1000}; },
    hasLayer:layer=>layers.has(layer),on(events,callback) { for (const event of events.split(' ')) { if (!handlers.has(event)) handlers.set(event,[]); handlers.get(event).push(callback); } },
    emit(event) { for (const callback of handlers.get(event) || []) callback(); }};
  // The harness exercises the application's scheduling/raster handoff. Leaflet's
  // actual pan/zoom transforms are covered by the browser interaction check.
  class ImageOverlay {
    constructor(url,bounds,options) { this.bounds=bounds;this.options=options;this._zoomAnimated=true; }
    setBounds(bounds) { this.bounds=bounds;return this; }
    addTo() { this._initImage();layers.add(this);return this; }
    static extend(properties) { class Overlay extends ImageOverlay {} Object.assign(Overlay.prototype,properties);return Overlay; }
  }
  const worker = {postMessage:message=>messages.push(message)};
  const context = {state:{sales:[{}],surface:true,surfaceFailed:false,renderId:0},surfaceMoving:false,renderTimer:null,
    waterMask:{erase:(ctx,origin)=>paints.push({masked:true,origin})},canvas,surfaceContext:drawing,worker,map,boundsEast:[[40,-120],[39,-119]],
    L:{ImageOverlay,DomUtil:{addClass:(element,value)=>element.classes.add(value)}},
    window:{innerWidth:1000},document:{createElement:()=>({getContext:()=>drawing})},
    $:id=>elements[id],color:()=>[90,120,150],showMapNotice:()=>{},
    setTimeout:(callback)=>{timers.set(++nextTimer,callback);return nextTimer;},clearTimeout:id=>timers.delete(id)};
  vm.createContext(context);
  const overlayStart=source.indexOf('  const CanvasOverlay ='),overlayEnd=source.indexOf('  const stops =',overlayStart);
  const renderStart=source.indexOf('  function scheduleSurface('),renderEnd=source.indexOf('  function renderSaleDots(',renderStart);
  assert.ok(overlayStart>=0 && overlayEnd>overlayStart && renderStart>=0 && renderEnd>renderStart);
  vm.runInContext(source.slice(overlayStart,overlayEnd)+source.slice(renderStart,renderEnd),context);
  const overlay=vm.runInContext('surfaceOverlay',context);
  const render=()=>vm.runInContext('renderSurface()',context);
  const schedule=()=>vm.runInContext('scheduleSurface()',context);
  const flush=()=>{const jobs=[...timers.values()];timers.clear();for(const callback of jobs)callback();};
  const requests=()=>messages.filter(message=>message.type==='render');
  const finish=request=>worker.onmessage({data:{...request,values:new Float32Array(request.width*request.height*2).fill(800)}});
  return {context,canvas,map,worker,overlay,elements,messages,paints,timers,render,schedule,flush,requests,finish};
}

test('dragging retains the existing raster and rejects a late result for the old view', () => {
  const ui=harness();
  ui.render();ui.finish(ui.requests().at(-1));
  const oldBounds=JSON.stringify(ui.overlay.bounds),oldPaintCount=ui.paints.length;
  assert.equal(ui.canvas.style.display,'block');
  ui.render();const pending=ui.requests().at(-1);
  ui.map.emit('movestart');
  assert.equal(ui.canvas.style.display,'block','Existing shading must remain visible while dragging');
  assert.equal(ui.elements['map-loading'].hidden,true);
  assert.equal(ui.messages.at(-1).type,'cancel');
  ui.finish(pending);
  assert.equal(ui.paints.length,oldPaintCount,'A stale worker result must not overwrite the retained raster');
  assert.equal(JSON.stringify(ui.overlay.bounds),oldBounds);
});

test('surface work waits for motion to end, including delayed assumption or resize updates', () => {
  const ui=harness();
  ui.render();ui.finish(ui.requests().at(-1));
  ui.schedule();
  ui.map.emit('movestart');
  assert.equal(ui.timers.size,0,'Movement must cancel a previously scheduled render');
  ui.schedule();ui.flush();
  ui.map.emit('resize');ui.flush();
  ui.render();
  assert.equal(ui.requests().length,1,'Controls and resize must not start work mid-gesture');
  ui.map.offset=point(25,10);
  ui.map.emit('moveend');
  assert.equal(ui.requests().length,1,'Movement settling keeps the debounce');
  ui.flush();
  assert.equal(ui.requests().length,2);
  assert.equal(ui.canvas.style.display,'block','Keep the old view visible while its replacement calculates');
  const replacement=ui.requests().at(-1);ui.finish(replacement);
  assert.equal(ui.paints.length,4);
  assert.deepEqual(JSON.parse(JSON.stringify(ui.overlay.bounds)),[[40.002,-119.995],[39.918,-119.855]]);
});

test('completed raster attaches to geographic padded bounds and joins Leaflet zoom animation', () => {
  const ui=harness();ui.render();
  const request=ui.requests().at(-1);
  assert.equal(ui.map.hasLayer(ui.overlay),false,'No geographic overlay is attached before pixels are ready');
  assert.equal(request.origin.width,140);
  assert.equal(request.origin.height,84);
  assert.deepEqual(JSON.parse(JSON.stringify(request.origin.bounds)),[[40.012,-120.02],[39.928,-119.88]]);
  ui.finish(request);
  assert.equal(ui.map.hasLayer(ui.overlay),true);
  assert.equal(ui.overlay._image,ui.canvas);
  assert.equal(ui.canvas.classes.has('leaflet-image-layer'),true);
  assert.equal(ui.canvas.classes.has('leaflet-zoom-animated'),true);
  assert.equal(ui.overlay.options.pane,'surface');
  assert.equal(ui.canvas.style.display,'block');
  assert.equal(ui.paints.at(-1).masked,true,'Water is removed after the smoothed raster is drawn');
  assert.equal(ui.paints.at(-1).origin,request.origin);
});

test('shading waits for boundaries and does not paint a worker reply without them', () => {
  const ui=harness();ui.render();const pending=ui.requests().at(-1);
  ui.context.waterMask=null;
  ui.finish(pending);
  assert.equal(ui.paints.length,0);
  ui.render();
  assert.equal(ui.requests().length,1);
  assert.equal(ui.canvas.style.display,'none');
});

test('a missing boundary asset disables shading and preserves its failure notice', async () => {
  const ui=harness();ui.context.fetch=async()=>({ok:false});
  await vm.runInContext('loadWaterMask()',ui.context);
  vm.runInContext("showMapNotice('Partial snapshot')",ui.context);
  assert.equal(ui.context.state.maskFailed,true);
  assert.equal(ui.context.state.surface,false);
  assert.equal(ui.elements['toggle-surface'].disabled,true);
  assert.equal(ui.elements['toggle-surface']['aria-pressed'],'false');
  assert.match(ui.elements['map-notice-text'].textContent,/Shoreline boundaries/);
  assert.equal(ui.canvas.style.display,'none');
});

test('a masking error hides the existing canvas and invalidates pending work', () => {
  const ui=harness();ui.render();ui.finish(ui.requests().at(-1));
  ui.context.waterMask.erase=()=>{throw new Error('Canvas failure');};
  ui.render();const pending=ui.requests().at(-1);ui.finish(pending);
  assert.equal(ui.canvas.style.display,'none');
  assert.equal(ui.context.state.maskFailed,true);
  const paints=ui.paints.length;ui.finish(pending);
  assert.equal(ui.paints.length,paints);
  assert.equal(ui.messages.at(-1).type,'cancel');
});
