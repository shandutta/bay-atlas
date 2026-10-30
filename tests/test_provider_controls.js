'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const test = require('node:test');
const vm = require('node:vm');
const source = fs.readFileSync(path.join(__dirname, '../public/app.js'), 'utf8');
class Element {
  constructor() { this.value = ''; this.checked = false; this.disabled = false; this.hidden = false; this.events = {}; }
  addEventListener(type, callback) { this.events[type] = callback; }
}
function harness(provider) {
  const elements = new Map();
  const $ = id => { if (!elements.has(id)) elements.set(id, new Element()); return elements.get(id); };
  $('provider-kind').value = 'apify';
  $('provider-scope').value = 'bay_area';
  const context = {$, connectedProvider:provider, state:{refreshRunning:false, importing:false}, number:n=>Number(n).toLocaleString('en-US'), Number, Date};
  vm.createContext(context);
  const start = source.indexOf('  function providerControls(');
  const end = source.indexOf("  $('provider-kind').addEventListener", start);
  vm.runInContext(source.slice(start,end), context);
  const render = () => vm.runInContext('providerControls()',context);
  render();
  return {context, $, render};
}
function paidProvider(state='active') {
  return {provider_kind:'apify',configured:true,live_verified:true,apify_max_items_per_county:50,
    allowance:{mode:'paid',state,total_usd:75,used_usd:21,remaining_usd:54,
      usage_total_usd:73,usage_reserved_usd:0,usage_remaining_usd:73,expires_at:'2030-01-01T00:00:00+00:00'}};
}
test('paid allowance removes free-account assertions and shows one-time limits honestly', () => {
  const ui = harness(paidProvider());
  assert.equal(ui.$('apify-free-settings').hidden,true);
  assert.equal(ui.$('apify-free-plan').disabled,true);
  assert.equal(ui.$('apify-free-plan').required,false);
  assert.equal(ui.$('apify-paid-settings').hidden,false);
  assert.equal(ui.$('provider-refresh-button').disabled,false);
  assert.match(ui.$('apify-allowance-status').textContent,/\$75\.00 one-time spending limit/);
  assert.match(ui.$('apify-allowance-status').textContent,/committed or reserved/);
  assert.match(ui.$('apify-results-note').textContent,/limited to 50 homes/);
  assert.match(ui.$('apify-results-note').textContent,/do not establish complete coverage/);
});
test('expired exhausted disabled and completed allowances pause refresh without enabling free fallback', () => {
  const ui = harness(paidProvider());
  for (const status of ['expired','exhausted','disabled','invalid','completed','paused']) {
    ui.context.connectedProvider.allowance.state = status;
    ui.render();
    assert.equal(ui.$('provider-refresh-button').disabled,true,status);
    assert.equal(ui.$('apify-free-settings').hidden,true,status);
    assert.match(ui.$('apify-allowance-status').textContent,/paused|finished/,status);
  }
});
test('free accounts retain their editable credit controls', () => {
  const ui = harness({...paidProvider(),allowance:{mode:'free'}});
  assert.equal(ui.$('apify-free-settings').hidden,false);
  assert.equal(ui.$('apify-free-plan').disabled,false);
  assert.equal(ui.$('apify-credit-cap').disabled,false);
  assert.equal(ui.$('apify-paid-settings').hidden,true);
  assert.equal(ui.$('provider-refresh-button').disabled,false);
});
test('saving a paid connection never submits a grant, free confirmation or paid spending edits', async () => {
  const ui = harness(paidProvider());
  const calls = [];
  ui.context.api = async (endpoint, options) => { calls.push({endpoint,body:JSON.parse(options.body)}); return paidProvider(); };
  ui.$('apify-run-cap').value = '100';
  ui.$('apify-credit-cap').value = '100';
  ui.$('apify-free-plan').checked = true;
  const start = source.indexOf("  $('provider-form').addEventListener('submit'");
  const end = source.indexOf("  $('redfin-import-form').addEventListener",start);
  vm.runInContext(source.slice(start,end),ui.context);
  await ui.$('provider-form').events.submit({preventDefault(){}});
  assert.deepEqual(calls,[{endpoint:'provider',body:{provider_kind:'apify',scope:'bay_area'}}]);
  assert.equal(ui.$('save-provider').disabled,false);
});
test('returning from another provider preserves the saved paid allowance without submitting free settings', async () => {
  const ui = harness({provider_kind:'housedata',configured:true,apify_allowance_mode:'paid'});
  assert.equal(ui.$('apify-free-settings').hidden,true);
  assert.match(ui.$('apify-allowance-status').textContent,/existing allowance/);
  const calls = [];
  ui.context.api = async (_endpoint, options) => { calls.push(JSON.parse(options.body)); return paidProvider(); };
  const start = source.indexOf("  $('provider-form').addEventListener('submit'");
  const end = source.indexOf("  $('redfin-import-form').addEventListener",start);
  vm.runInContext(source.slice(start,end),ui.context);
  await ui.$('provider-form').events.submit({preventDefault(){}});
  assert.deepEqual(calls,[{provider_kind:'apify',scope:'bay_area'}]);
});
test('pending publication offers a saved-data action without presenting a new paid refresh', () => {
  const ui = harness(paidProvider('publication_pending'));
  assert.equal(ui.$('provider-refresh-button').disabled,false);
  assert.equal(ui.$('provider-refresh-label').textContent,'Publish saved results');
  assert.match(ui.$('apify-allowance-status').textContent,/makes no new provider request/);
  assert.equal(ui.$('apify-free-settings').hidden,true);
});

test('status monitoring retries only reads after a network failure and reloads partial publication', async () => {
  const ui = harness(paidProvider()), scheduled=[], requests=[];
  let fail=true, reloads=0;
  Object.assign(ui.context,{
    document:{hidden:false},setTimeout:(callback,delay)=>{scheduled.push({callback,delay});return scheduled.length;},clearTimeout:()=>{},
    showMapNotice:()=>{},loadData:async()=>{reloads++;},
    api:async(endpoint,options)=>{requests.push({endpoint,options});if(fail)throw Error('Temporarily offline');return endpoint==='refresh'?{state:'partial',message:'Saved county results were published offline.'}:paidProvider('disabled');}
  });
  const start = source.indexOf('  function applyRefresh(');
  const end = source.indexOf("  $('provider-refresh-button').addEventListener",start);
  vm.runInContext(source.slice(start,end),ui.context);
  vm.runInContext("applyRefresh({state:'running'})",ui.context);
  assert.equal(scheduled.at(-1).delay,5000);
  await vm.runInContext('checkRefresh()',ui.context);
  assert.equal(scheduled.length,2,'A read error must reschedule status monitoring');
  fail=false;
  await vm.runInContext('checkRefresh()',ui.context);
  assert.equal(reloads,1,'Partial publication must appear without waiting for the periodic map reload');
  assert.equal(ui.context.state.refreshRunning,false);
  assert.equal(ui.$('provider-refresh-button').disabled,true,'A disabled allowance stays disabled');
  assert.ok(requests.every(call=>!call.options),'Status recovery must never make a POST request');
});
