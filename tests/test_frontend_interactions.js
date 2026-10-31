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
function dragHarness(expanded=true,onMinimize=()=>{}) {
  const panel = new Element(), handle = new Element(), header = new Element(), content = new Element(), label = new Element();
  const classes = new Set();
  const classList = {contains:x=>classes.has(x),add:x=>classes.add(x),remove:x=>classes.delete(x),toggle(x,on){if(on)classes.add(x);else classes.delete(x);}};
  panel.classList = classList;
  panel.parentElement = {clientHeight:800,classList:{add(){},remove(){}},style:{setProperty(key,value){panel.style.height=value;}}};
  panel.getBoundingClientRect = () => ({height:parseFloat(panel.style.height) || 344});
  panel.querySelector = () => content; handle.querySelector = () => label; handle.focus = () => {};
  const install = helper('installPanelDrag','panelControls',{window:{innerWidth:390,addEventListener(){}},document:{activeElement:null},Date});
  const controls = install(panel,handle,header,onMinimize); if (expanded) controls.setExpanded(true);
  return {panel,handle,header,content,label,controls};
}
async function drag(ui,dy,id=1) {
  await ui.handle.emit('pointerdown',{pointerId:id,clientX:100,clientY:100,pointerType:'touch',timeStamp:0});
  await ui.handle.emit('pointermove',{pointerId:id,clientX:101,clientY:100+dy,timeStamp:500});
  await ui.handle.emit('pointerup',{pointerId:id,clientX:101,clientY:100+dy,timeStamp:800});
}
test('the initial drawer follows a downward drag and can minimize below its old lower bound', async () => {
  const ui = dragHarness(false);
  await ui.handle.emit('pointerdown',{pointerId:1,clientX:100,clientY:100,pointerType:'touch'});
  await ui.handle.emit('pointermove',{pointerId:1,clientX:101,clientY:190});
  assert.equal(ui.panel.style.height,'254px');
  await ui.handle.emit('pointerup',{pointerId:1,clientX:101,clientY:190});
  assert.equal(ui.panel.style.height,'64px');
  assert.equal(ui.handle.attrs['aria-expanded'],'false');
  assert.equal(ui.content.inert,true);
  await ui.handle.emit('click');
  assert.equal(ui.panel.style.height,'64px');
  await ui.handle.emit('click',{detail:0});
  assert.equal(ui.panel.style.height,'344px');
  assert.equal(ui.content.inert,false);
});
test('successive drags move from full details through search to the map, and back', async () => {
  const ui = dragHarness();
  await drag(ui,90); assert.equal(ui.panel.style.height,'344px');
  await drag(ui,90,2); assert.equal(ui.panel.style.height,'64px');
  await drag(ui,-90,3); assert.equal(ui.panel.style.height,'344px');
  await drag(ui,-90,4); assert.equal(ui.panel.style.height,'600px');
  assert.equal(ui.controls.interaction(),4);
});
test('short flicks advance a snap while canceled and horizontal gestures retain the previous state', async () => {
  const ui = dragHarness(false);
  await ui.handle.emit('pointerdown',{pointerId:1,clientX:100,clientY:100,pointerType:'touch',timeStamp:0});
  await ui.handle.emit('pointermove',{pointerId:1,clientX:100,clientY:120,timeStamp:20});
  await ui.handle.emit('pointerup',{pointerId:1,clientX:100,clientY:120,timeStamp:30});
  assert.equal(ui.panel.style.height,'64px');
  await ui.handle.emit('keydown',{key:'End'});
  await ui.handle.emit('pointerdown',{pointerId:2,clientX:100,clientY:100,pointerType:'touch'});
  await ui.handle.emit('pointermove',{pointerId:2,clientX:101,clientY:180});
  await ui.handle.emit('pointercancel',{pointerId:2,clientX:101,clientY:180});
  assert.equal(ui.panel.style.height,'600px');
  await ui.handle.emit('pointerdown',{pointerId:3,clientX:100,clientY:100,pointerType:'touch'});
  await ui.handle.emit('pointermove',{pointerId:3,clientX:200,clientY:104});
  assert.equal(ui.panel.style.height,'600px');
});
test('keyboard snaps work and content gestures preserve native scrolling while cancelling delayed auto-scroll', async () => {
  const ui = dragHarness(false);
  await ui.handle.emit('keydown',{key:'ArrowUp'}); assert.equal(ui.panel.style.height,'600px');
  await ui.handle.emit('keydown',{key:'Home'}); assert.equal(ui.content.inert,true);
  await ui.handle.emit('click',{detail:0}); assert.equal(ui.panel.style.height,'600px');
  const before=ui.controls.interaction();
  const event=await ui.content.emit('pointerdown');
  assert.equal(event.prevented,undefined);
  assert.equal(ui.controls.interaction(),before+1);
  assert.equal(ui.content.events.pointermove,undefined);
});
test('an estimate arriving after manual collapse cannot reopen the mobile panel', async () => {
  const {panel,controls} = dragHarness(false), elements = {'explorer-panel':panel,'welcome-state':new Element(),'estimate-section':new Element()};
  let finishEstimate, scrolled=false;
  const context={state:{selected:null},marker:null,waterMask:null,Number,window:{innerWidth:390,matchMedia:()=>({matches:false})},panelControls:controls,
    $:id=>elements[id],fetchEstimate:()=>new Promise(resolve=>{finishEstimate=resolve;}),
    L:{divIcon:()=>({}),marker:()=>({addTo(){return this;}})},map:{getZoom:()=>14,project:()=>({add(){return this;}}),getSize:()=>({y:800}),unproject:x=>x,setView(){}},
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
  const context={state:{selected:{lat:37.82,lng:-122.25},estimateId:0},waterMask:null,marker:null,AbortController,URLSearchParams,
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

test('canceling an in-flight address request removes the false loading message', async () => {
  let finish; const ui=searchHarness(()=>new Promise(resolve=>{finish=resolve;}));
  ui.input.value='123 Test Street'; const pending=ui.form.emit('submit');
  assert.equal(ui.status.textContent,'Finding that address…');
  await ui.input.emit('keydown',{key:'Escape'});
  assert.equal(ui.status.textContent,'');
  finish({results:[result]}); await pending;
  assert.equal(ui.selected.length,0);
});
test('submitted street-level matches require a deliberate choice and retain their approximation label', async () => {
  const ui=searchHarness(async()=>({results:[{...result,precision:'Approximate street location'}]}));
  ui.input.value='123 Test Street'; await ui.form.emit('submit');
  assert.equal(ui.selected.length,0); assert.equal(ui.list.hidden,false);
  await ui.list.buttons[0].emit('click');
  assert.ok(ui.selected[0][2].endsWith('approximate street location'));
});
test('no-match responses display the actionable provider message', async () => {
  const ui=searchHarness(async()=>({results:[],message:'Add the city to narrow this street.'}));
  ui.input.value='123 Test Street'; await ui.form.emit('submit');
  assert.equal(ui.status.textContent,'Add the city to narrow this street.');
});

test('minimizing during address lookup prevents a late geocode from reopening the drawer', async () => {
  let finish; const search=searchHarness(()=>new Promise(resolve=>{finish=resolve;}));
  const drawer=dragHarness(false,()=>search.controls.cancel());
  search.input.value='123 Test Street'; const pending=search.form.emit('submit');
  await drawer.handle.emit('click',{detail:0});
  finish({results:[result]}); await pending;
  assert.equal(search.selected.length,0);
  assert.equal(search.status.textContent,'');
  assert.equal(drawer.panel.style.height,'64px');
});

test('a horizontal handle swipe cannot become an accidental collapse tap', async () => {
  const ui=dragHarness(false);
  await ui.handle.emit('pointerdown',{pointerId:1,clientX:100,clientY:100,pointerType:'mouse',button:0});
  await ui.handle.emit('pointermove',{pointerId:1,clientX:200,clientY:104});
  await ui.handle.emit('pointerup',{pointerId:1,clientX:200,clientY:104});
  await ui.handle.emit('click');
  assert.equal(ui.panel.style.height,'344px');
});

test('water map selections remain unavailable after assumption changes or data refresh', async () => {
  const element=new Element(); let calls=0,aborted=0;
  const context={state:{selected:{lat:37.8,lng:-122.4,mapTap:true},estimateId:0,estimateController:{abort(){++aborted;}}},
    waterMask:{isWater:()=>true},clearEstimateTooltip(){},$:()=>element,api(){++calls;}};
  const from=source.indexOf('  async function fetchEstimate('),to=source.indexOf('\n  function installAddressSearch',from);
  vm.createContext(context);const fetchEstimate=vm.runInContext(`${source.slice(from,to)}\nfetchEstimate`,context);
  await fetchEstimate(); await fetchEstimate();
  assert.equal(calls,0);assert.equal(aborted,2);assert.equal(element.hidden,false);
  assert.ok(element.html.includes('This point is on mapped water.'));
});
