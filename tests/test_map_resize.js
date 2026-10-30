'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');

const source = fs.readFileSync(path.join(__dirname,'../public/app.js'),'utf8');
const from = source.indexOf('  function installMapResize(');
const to = source.indexOf('\n  installMapResize(',from);
assert.ok(from >= 0 && to > from);

function eventTarget() {
  const listeners = new Map();
  return {
    addEventListener(type,listener) {
      if (!listeners.has(type)) listeners.set(type,new Set());
      listeners.get(type).add(listener);
    },
    removeEventListener(type,listener) { listeners.get(type)?.delete(listener); },
    emit(type) { for (const listener of listeners.get(type) || []) listener({type}); },
    listenerCount() { return [...listeners.values()].reduce((sum,set) => sum + set.size,0); }
  };
}

function harness({observer = true, visualViewport = true, width = 390, height = 720} = {}) {
  const window = eventTarget(), document = {...eventTarget(),hidden:false};
  if (visualViewport) window.visualViewport = eventTarget();
  const frames = new Map(), invalidations = [], mapEvents = new Map();
  const element = {clientWidth:width,clientHeight:height};
  let nextFrame = 0, resizeObserver = null;
  if (observer) window.ResizeObserver = class {
    constructor(callback) { this.callback = callback; this.disconnected = false; resizeObserver = this; }
    observe(target) { this.target = target; }
    disconnect() { this.disconnected = true; }
    notify() { if (!this.disconnected) this.callback(); }
  };
  const map = {
    invalidateSize(options) { invalidations.push({width:element.clientWidth,height:element.clientHeight,...options}); },
    on(type,handler) { mapEvents.set(type,handler); }
  };
  const context = {window,document,
    requestAnimationFrame:callback => { frames.set(++nextFrame,callback); return nextFrame; },
    cancelAnimationFrame:id => frames.delete(id)};
  vm.createContext(context);
  const install = vm.runInContext(`${source.slice(from,to)}\ninstallMapResize`,context);
  install(map,element);
  const flush = () => { const pending = [...frames.values()]; frames.clear(); for (const callback of pending) callback(); };
  return {window,document,element,frames,invalidations,flush,resizeObserver,unload:() => mapEvents.get('unload')()};
}

test('element-only height changes refresh Leaflet without panning the map', () => {
  const ui = harness();
  assert.equal(ui.resizeObserver.target,ui.element);
  ui.flush();
  ui.element.clientHeight = 835;
  ui.resizeObserver.notify();
  assert.equal(ui.invalidations.length,1,'No work runs before the next animation frame');
  ui.flush();
  assert.deepEqual(ui.invalidations.at(-1),{width:390,height:835,pan:false,debounceMoveend:true});
});

test('observer, browser and visual viewport notifications share one measurement per frame', () => {
  const ui = harness(); ui.flush();
  ui.element.clientHeight = 810;
  ui.resizeObserver.notify();
  ui.window.emit('resize');
  ui.window.visualViewport.emit('resize');
  ui.window.emit('pageshow');
  assert.equal(ui.frames.size,1);
  ui.element.clientHeight = 825;
  ui.resizeObserver.notify();
  ui.flush();
  assert.equal(ui.invalidations.length,2);
  assert.equal(ui.invalidations.at(-1).height,825,'Read the final element size, not an earlier observer notification');
});

test('unchanged dimensions do not restart tile or surface work', () => {
  const ui = harness(); ui.flush();
  for (const notify of [() => ui.resizeObserver.notify(),() => ui.window.emit('resize'),
    () => ui.window.visualViewport.emit('resize'),() => ui.window.emit('pageshow'),
    () => ui.document.emit('visibilitychange')]) {
    notify(); ui.flush();
  }
  assert.equal(ui.invalidations.length,1);
});

test('a hidden zero-size map waits for a usable size and recovers after later hiding', () => {
  const ui = harness({width:0,height:0}); ui.flush();
  assert.equal(ui.invalidations.length,0);
  ui.element.clientWidth = 390; ui.element.clientHeight = 720;
  ui.resizeObserver.notify(); ui.flush();
  assert.equal(ui.invalidations.length,1);
  ui.element.clientHeight = 0; ui.resizeObserver.notify(); ui.flush();
  assert.equal(ui.invalidations.length,1,'Do not feed a collapsed dimension into Leaflet');
  ui.element.clientHeight = 830; ui.resizeObserver.notify(); ui.flush();
  assert.equal(ui.invalidations.length,2);
  assert.equal(ui.invalidations.at(-1).height,830);
});

test('returning to the page catches viewport changes missed while backgrounded', () => {
  const ui = harness(); ui.flush();
  ui.document.hidden = true;
  ui.element.clientHeight = 800;
  ui.document.emit('visibilitychange');
  assert.equal(ui.frames.size,0,'Becoming hidden does not request a repaint');
  ui.document.hidden = false; ui.document.emit('visibilitychange'); ui.flush();
  assert.equal(ui.invalidations.at(-1).height,800);
  ui.element.clientWidth = 844; ui.element.clientHeight = 390;
  ui.window.emit('pageshow'); ui.flush();
  assert.equal(ui.invalidations.length,3);
  assert.deepEqual(ui.invalidations.at(-1),{width:844,height:390,pan:false,debounceMoveend:true});
});

test('window resize and page restoration remain available without optional browser APIs', () => {
  const ui = harness({observer:false,visualViewport:false}); ui.flush();
  ui.element.clientHeight = 800; ui.window.emit('resize'); ui.flush();
  ui.element.clientHeight = 760; ui.window.emit('pageshow'); ui.flush();
  assert.equal(ui.invalidations.length,3);
  assert.equal(ui.invalidations.at(-1).height,760);
  ui.unload();
  assert.equal(ui.window.listenerCount(),0);
  assert.equal(ui.document.listenerCount(),0);
});

test('unloading disconnects observation, removes listeners and cancels pending work', () => {
  const ui = harness(); ui.flush();
  ui.element.clientHeight = 800; ui.resizeObserver.notify();
  assert.equal(ui.frames.size,1);
  ui.unload();
  assert.equal(ui.resizeObserver.disconnected,true);
  assert.equal(ui.frames.size,0);
  assert.equal(ui.window.listenerCount(),0);
  assert.equal(ui.window.visualViewport.listenerCount(),0);
  assert.equal(ui.document.listenerCount(),0);
  ui.resizeObserver.notify(); ui.window.emit('resize'); ui.window.emit('pageshow');
  ui.window.visualViewport.emit('resize'); ui.document.emit('visibilitychange'); ui.flush();
  assert.equal(ui.invalidations.length,1,'No map updates are allowed after its unload');
});
