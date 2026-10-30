'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname,'../public/app.js'),'utf8');
function helper(name, next, context) {
  const from = source.indexOf(`  function ${name}(`), to = source.indexOf(`\n  const ${next}`, from);
  assert.ok(from >= 0 && to > from);
  vm.createContext(context);
  return vm.runInContext(`${source.slice(from,to)}\n${name}`,context);
}
class Element {
  constructor() { this.events = {}; this.attrs = {}; this.style = {}; this.hidden = true; this.value = ''; this.disabled = false; this.buttons = []; }
  addEventListener(type, handler) { (this.events[type] ||= []).push(handler); }
  async emit(type, values={}) {
    const event = {type,detail:1,preventDefault(){this.prevented=true;},target:this,...values};
    for (const handler of this.events[type] || []) await handler(event);
    return event;
  }
  setAttribute(key,value) { this.attrs[key] = value; }
  removeAttribute(key) { delete this.attrs[key]; }
  closest() { return null; }
  contains(target) { return target === this || this.buttons.includes(target); }
  scrollIntoView() {}
  set innerHTML(value) { this.html = value; this.buttons = Array.from(value.matchAll(/data-result="(\d+)"/g), () => new Element()); }
  querySelectorAll() { return this.buttons; }
  setPointerCapture() { this.captured = true; }
  hasPointerCapture() { return this.captured; }
  releasePointerCapture() { this.captured = false; }
}
function dragHarness(expanded=true) {
  const panel = new Element(), handle = new Element(), header = new Element();
  const classes = new Set(expanded ? ['expanded'] : []);
  panel.classList = {contains:x=>classes.has(x),add:x=>classes.add(x),remove:x=>classes.delete(x),toggle(x,on){if(on)classes.add(x);else classes.delete(x);}};
  panel.parentElement = {clientHeight:800}; panel.getBoundingClientRect = () => ({height:classes.has('expanded') ? 600 : 344});
  const install = helper('installPanelDrag','panelControls',{window:{innerWidth:390},Date});
  return {panel,handle,header,controls:install(panel,handle,header)};
}
test('drag handle collapses and expands, suppressing the synthetic post-drag click', async () => {
  const {panel,handle,controls} = dragHarness();
  await handle.emit('pointerdown',{pointerId:1,clientX:100,clientY:100,pointerType:'touch'});
  await handle.emit('pointermove',{pointerId:1,clientX:101,clientY:190});
  assert.equal(panel.style.height,'510px');
  await handle.emit('pointerup',{pointerId:1,clientX:101,clientY:190});
  assert.equal(handle.attrs['aria-expanded'],'false');
  assert.equal(controls.interaction(),1);
  await handle.emit('click');
  assert.equal(handle.attrs['aria-expanded'],'false');
  await handle.emit('pointerdown',{pointerId:2,clientX:100,clientY:200,pointerType:'touch'});
  await handle.emit('pointermove',{pointerId:2,clientX:100,clientY:100});
  await handle.emit('pointerup',{pointerId:2,clientX:100,clientY:100});
  assert.equal(handle.attrs['aria-expanded'],'true');
});
test('keyboard activation stays accessible and content scrolling has no drag handlers', async () => {
  const {panel,handle} = dragHarness(false);
  await handle.emit('click',{detail:0});
  assert.equal(handle.attrs['aria-expanded'],'true');
  assert.equal(panel.events.pointerdown,undefined);
  await handle.emit('pointerdown',{pointerId:1,clientX:100,clientY:100,pointerType:'touch'});
  await handle.emit('pointermove',{pointerId:1,clientX:200,clientY:104});
  assert.equal(panel.style.height,'');
  await handle.emit('pointercancel',{pointerId:1,clientX:200,clientY:104});
  assert.equal(handle.attrs['aria-expanded'],'true');
});
test('an estimate arriving after manual collapse cannot reopen the mobile panel', async () => {
  const {panel,controls} = dragHarness(false), elements = {'explorer-panel':panel,'welcome-state':new Element(),'estimate-section':new Element()};
  let finishEstimate, scrolled=false;
  const context={state:{selected:null},marker:null,Number,window:{innerWidth:390},panelControls:controls,
    $:id=>elements[id],fetchEstimate:()=>new Promise(resolve=>{finishEstimate=resolve;}),
    L:{divIcon:()=>({}),marker:()=>({addTo(){return this;}})},map:{},
    requestAnimationFrame:()=>{scrolled=true;},document:{querySelector:()=>({})}};
  const start=source.indexOf('  async function selectLocation('),end=source.indexOf('\n  function renderEstimate(',start);
  vm.createContext(context);
  const select=vm.runInContext(`${source.slice(start,end)}\nselectLocation`,context);
  const pending=select(37.82,-122.25,'Test home',false);
  assert.equal(panel.classList.contains('expanded'),true);
  controls.setExpanded(false,true);
  finishEstimate();await pending;
  assert.equal(panel.classList.contains('expanded'),false);
  assert.equal(scrolled,false);
});
function searchHarness(fetchJson) {
  const input = new Element(), form = new Element(), list = new Element(), status = new Element(), submit = new Element();
  const timers = new Map(), selected = [], document = {activeElement:input}; let next = 0;
  input.focus = () => { document.activeElement = input; }; input.blur = () => { document.activeElement = null; };
  const context = {AbortController,Number,encodeURIComponent,document,
    esc:value=>String(value).replaceAll('&','&amp;').replaceAll('<','&lt;').replaceAll('>','&gt;').replaceAll('"','&quot;'),
    setTimeout:(callback,delay)=>{timers.set(++next,{callback,delay});return next;},clearTimeout:id=>timers.delete(id)};
  const install = helper('installAddressSearch','addressSearch',context);
  const controls = install({input,form,list,status,submit,fetchJson,onSelect:(...args)=>selected.push(args)});
  const flush = async () => { const jobs=[...timers.values()];timers.clear();for(const job of jobs)await job.callback(); };
  return {input,form,list,status,submit,timers,selected,controls,flush};
}
const result = {lat:37.87,lng:-122.27,display_name:'123 Test Street, Berkeley, CA',source:'Local sale records'};
test('suggestions wait for six characters and support arrows plus Enter', async () => {
  const calls = [], ui = searchHarness(async url => {calls.push(url);return {results:[result]};});
  ui.input.value='123 T';await ui.input.emit('input');assert.equal(ui.timers.size,0);
  ui.input.value='123 Te';await ui.input.emit('input');assert.equal([...ui.timers.values()][0].delay,650);
  await ui.flush();assert.equal(calls[0],'suggest?q=123%20Te');assert.equal(ui.input.attrs['aria-expanded'],'true');
  await ui.input.emit('keydown',{key:'ArrowDown'});assert.equal(ui.input.attrs['aria-activedescendant'],'address-option-0');
  const enter=await ui.input.emit('keydown',{key:'Enter'});assert.equal(enter.prevented,true);
  assert.equal(ui.selected.length,1);assert.equal(ui.selected[0][2],result.display_name);assert.equal(ui.list.hidden,true);
});
test('late suggestion responses and Escape cannot reopen a dismissed list', async () => {
  let resolve;const ui=searchHarness(()=>new Promise(done=>{resolve=done;}));
  ui.input.value='123 Test';await ui.input.emit('input');const pending=ui.flush();
  await ui.input.emit('keydown',{key:'Escape'});resolve({results:[result]});await pending;
  assert.equal(ui.list.hidden,true);assert.equal(ui.input.attrs['aria-expanded'],'false');
});
test('explicit search remains available after autocomplete failure', async () => {
  const calls=[];const ui=searchHarness(async url=>{calls.push(url);if(url.startsWith('suggest'))throw Error('offline');return {results:[result]};});
  ui.input.value='123 Test Street';await ui.input.emit('input');await ui.flush();
  await ui.form.emit('submit');assert.equal(calls.length,2);assert.ok(calls[1].startsWith('geocode?'));
  assert.equal(ui.selected.length,1);assert.equal(ui.submit.disabled,false);
});
test('suggestion labels escape markup and flag street precision', async () => {
  const ui=searchHarness(async()=>({results:[{...result,display_name:'<img src=x>',precision:'Approximate street or place location'}]}));
  ui.input.value='123 Test';await ui.input.emit('input');await ui.flush();
  assert.ok(ui.list.html.includes('&lt;img'));assert.ok(!ui.list.html.includes('<img'));
  await ui.list.buttons[0].emit('click');assert.ok(ui.selected[0][2].includes('approximate street location'));
});
test('mapped street addresses are not mislabeled as street-centre suggestions', async () => {
  const ui=searchHarness(async()=>({results:[{...result,precision:'Mapped street address'}]}));
  ui.input.value='123 Test';await ui.input.emit('input');await ui.flush();
  assert.ok(!ui.list.html.includes('approximate street location'));
  await ui.list.buttons[0].emit('click');assert.equal(ui.selected[0][2],result.display_name);
});

test('editing an address aborts the old full-address request and ignores its late response', async () => {
  let finish, signal;
  const ui=searchHarness((_url,options)=>{signal=options.signal;return new Promise(resolve=>{finish=resolve;});});
  ui.input.value='123 Example Street';
  const pending=ui.form.emit('submit');
  assert.equal(signal.aborted,false);
  ui.input.value='456 Example Street';await ui.input.emit('input');
  assert.equal(signal.aborted,true);
  finish({results:[result]});await pending;
  assert.equal(ui.selected.length,0);
  assert.equal(ui.submit.disabled,false);
});

test('changing a selected location cancels the old estimate and cannot display stale values', async () => {
  const requests=[], rendered=[], elements={'estimate-section':new Element(),'sqft':{value:'2500'},'beds':{value:'3'}};
  const context={state:{selected:{lat:37.82,lng:-122.25},estimateId:0},marker:null,AbortController,URLSearchParams,
    $:id=>elements[id],clearEstimateTooltip:()=>{},esc:String,renderEstimate:data=>rendered.push(data),
    api:(url,options)=>new Promise(resolve=>requests.push({url,options,resolve}))};
  const from=source.indexOf('  async function fetchEstimate('),to=source.indexOf('\n  function installAddressSearch',from);
  vm.createContext(context);
  const fetchEstimate=vm.runInContext(`${source.slice(from,to)}\nfetchEstimate`,context);
  const first=fetchEstimate();
  context.state.selected={lat:37.87,lng:-122.27};
  const second=fetchEstimate();
  assert.equal(requests[0].options.signal.aborted,true);
  requests[0].resolve({estimate_ppsf:500});await first;
  assert.equal(rendered.length,0);
  requests[1].resolve({estimate_ppsf:900});await second;
  assert.equal(rendered[0].estimate_ppsf,900);
});

test('retired estimate tooltip is hidden immediately while Leaflet waits for its fade cleanup', () => {
  const element=new Element();element.hidden=false;
  let unbound=false;
  const context={marker:{getTooltip:()=>({getElement:()=>element}),unbindTooltip:()=>{unbound=true;}}};
  const from=source.indexOf('  function clearEstimateTooltip('),to=source.indexOf('\n  async function selectLocation',from);
  vm.createContext(context);
  vm.runInContext(`${source.slice(from,to)}\nclearEstimateTooltip()`,context);
  assert.equal(element.hidden,true);
  assert.equal(element.attrs['aria-hidden'],'true');
  assert.equal(unbound,true);
});
