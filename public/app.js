'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const currencyFormat = new Intl.NumberFormat('en-US', {style:'currency',currency:'USD',maximumFractionDigits:0});
  const countFormat = new Intl.NumberFormat('en-US', {maximumFractionDigits:0});
  const money = value => currencyFormat.format(value);
  const number = value => countFormat.format(value);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  const shortDate = value => { const d = new Date(`${String(value).slice(0,10)}T12:00:00`); return Number.isNaN(+d) ? 'Date unavailable' : d.toLocaleDateString('en-US',{month:'short',year:'numeric'}); };
  const state = {sales:[],data:null,selected:null,estimateId:0,estimateController:null,renderId:0,surface:true,surfaceFailed:false,showSales:false,refreshTimer:null,providerConfigured:false,importing:false,refreshRunning:false};
  const boundsEast = L.latLngBounds([[37.68,-122.39],[37.97,-121.99]]);
  // Nine-county extent, including northern Sonoma/Napa and southern Santa Clara.
  // U.S. Census 2025 county cartographic boundaries, rounded outward.
  const boundsBay = L.latLngBounds([[36.89,-123.54],[38.87,-121.20]]);
  const map = L.map('map',{zoomControl:false,minZoom:7,maxZoom:19,tap:true,preferCanvas:true}).setView([37.823,-122.18],11);
  L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png',{maxZoom:19,attribution:'&copy; <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noopener">OpenStreetMap</a> contributors',crossOrigin:true}).addTo(map);
  L.control.zoom({position:'topright'}).addTo(map);
  map.createPane('surface'); map.getPane('surface').style.zIndex = 350;
  map.createPane('saleDots'); map.getPane('saleDots').style.zIndex = 410;
  const salesLayer = L.layerGroup();
  const worker = new Worker('surface-worker.js');
  const canvas = L.DomUtil.create('canvas','price-surface',map.getPane('surface'));
  const surfaceContext = canvas.getContext('2d');
  // Reuse Leaflet's geographic image positioning for the canvas. Its inherited
  // zoom/viewreset/zoomanim handlers keep existing pixels aligned during pans,
  // button/wheel zooms and continuous pinch zooms, without encoding an image.
  const CanvasOverlay = L.ImageOverlay.extend({
    _initImage() {
      this._image = canvas;
      L.DomUtil.addClass(canvas,'leaflet-image-layer');
      if (this._zoomAnimated) L.DomUtil.addClass(canvas,'leaflet-zoom-animated');
    }
  });
  const surfaceOverlay = new CanvasOverlay('',boundsEast,{pane:'surface',interactive:false});
  const stops = [[89,123,156],[91,155,153],[168,189,134],[223,189,112],[200,120,84]];
  let marker = null, renderTimer = null, surfaceMoving = false;
  function fitRegion(bounds) {
    const mobile = window.innerWidth <= 700;
    map.fitBounds(bounds,{paddingTopLeft:mobile ? [12,60] : [385,55],paddingBottomRight:mobile ? [12,map.getSize().y * .44] : [60,45],animate:false});
  }
  fitRegion(boundsEast);
  async function api(path, options={}) {
    const response = await fetch(`api/${path}`,{cache:'no-store',...options,headers:{'Accept':'application/json',...(options.body ? {'Content-Type':'application/json'} : {}),...options.headers}});
    let body;
    try { body = await response.json(); } catch { throw new Error('The map service returned an unreadable response. Try again shortly.'); }
    if (!response.ok) throw new Error(body.error || body.message || `Request failed (${response.status}).`);
    return body;
  }
  function saleSource(url) {
    try {
      const parsed = new URL(url);
      if (parsed.protocol !== 'https:' || parsed.username || parsed.password) return null;
      for (const [host,label] of [['zillow.com','Zillow'],['redfin.com','Redfin']]) {
        if (parsed.hostname === host || parsed.hostname.endsWith(`.${host}`)) return {href:parsed.href,label};
      }
    } catch {}
    return null;
  }
  function color(value) {
    const fraction = Math.max(0,Math.min(1,(value - 450) / 1000)) * (stops.length - 1);
    const i = Math.min(stops.length - 2,Math.floor(fraction)), t = fraction - i;
    return stops[i].map((v,k) => Math.round(v + (stops[i + 1][k] - v) * t));
  }
  function scheduleSurface() {
    clearTimeout(renderTimer); ++state.renderId; worker.postMessage({type:'cancel'});
    renderTimer = setTimeout(renderSurface,120);
  }
  function renderSurface() {
    if (!state.surface || state.surfaceFailed || !state.sales.length) { worker.postMessage({type:'cancel'}); canvas.style.display = 'none'; $('map-loading').hidden = true; return; }
    if (surfaceMoving) return;
    const size = map.getSize();
    // A modest margin keeps nearby shading available during ordinary drags.
    // Newly exposed territory beyond it is calculated when movement settles.
    const padding = size.multiplyBy(.2).ceil(), rasterSize = size.add(padding.multiplyBy(2));
    // Every low-resolution pixel is an independently estimated scalar sample.
    // Browser bilinear interpolation makes the visible color field continuous.
    const baseStep = window.innerWidth <= 700 ? 11 : 14;
    const centerPoint = size.divideBy(2), metersPerPixel = map.distance(map.containerPointToLatLng(centerPoint), map.containerPointToLatLng(centerPoint.add([1,0])));
    const step = Math.max(3, Math.ceil(Math.sqrt(rasterSize.x * rasterSize.y / 60000)), Math.min(baseStep, Math.floor(700 / metersPerPixel)));
    const width = Math.ceil(rasterSize.x / step) + 1, height = Math.ceil(rasterSize.y / step) + 1, points = new Float64Array(width * height * 2);
    for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
      const ll = map.containerPointToLatLng([x * step - padding.x,y * step - padding.y]), offset = (y * width + x) * 2;
      points[offset] = ll.lat; points[offset + 1] = ll.lng;
    }
    const northWest = map.containerPointToLatLng(padding.multiplyBy(-1));
    const southEast = map.containerPointToLatLng(size.add(padding));
    $('map-loading').hidden = false;
    worker.postMessage({type:'render',id:++state.renderId,points,width,height,origin:{bounds:[[northWest.lat,northWest.lng],[southEast.lat,southEast.lng]],width:rasterSize.x,height:rasterSize.y,step},sqft:Number($('sqft').value),beds:Number($('beds').value)},[points.buffer]);
  }
  worker.onmessage = ({data}) => {
    if (data.id !== state.renderId) return;
    $('map-loading').hidden = true;
    if (!state.surface) return;
    const mini = document.createElement('canvas'); mini.width = data.width; mini.height = data.height;
    const ctx = mini.getContext('2d'), img = ctx.createImageData(data.width,data.height);
    for (let i = 0; i < data.values.length / 2; i++) {
      const value = data.values[i * 2], support = data.values[i * 2 + 1]; if (!value) continue;
      const rgb = color(value); img.data[i * 4] = rgb[0]; img.data[i * 4 + 1] = rgb[1]; img.data[i * 4 + 2] = rgb[2]; img.data[i * 4 + 3] = Math.round(255 * (.25 + .4 * Math.max(0, (support - .24) / .76)));
    }
    ctx.putImageData(img,0,0); canvas.width = data.origin.width; canvas.height = data.origin.height;
    surfaceContext.clearRect(0,0,canvas.width,canvas.height); surfaceContext.imageSmoothingEnabled = true; surfaceContext.imageSmoothingQuality = 'high';
    surfaceContext.drawImage(mini,-data.origin.step / 2,-data.origin.step / 2,data.width * data.origin.step,data.height * data.origin.step);
    surfaceOverlay.setBounds(data.origin.bounds);
    if (!map.hasLayer(surfaceOverlay)) surfaceOverlay.addTo(map);
    canvas.style.display = 'block';
  };
  worker.onerror = () => {
    state.surfaceFailed = true; canvas.style.display = 'none'; $('map-loading').hidden = true;
    $('toggle-surface').disabled = true;
    showMapNotice('The price surface could not load. Address estimates are still available. Reload the page to retry shading.');
  };
  map.on('movestart',() => { surfaceMoving = true; clearTimeout(renderTimer); ++state.renderId; worker.postMessage({type:'cancel'}); $('map-loading').hidden = true; });
  map.on('moveend',() => { surfaceMoving = false; scheduleSurface(); });
  map.on('resize',scheduleSurface);
  function showMapNotice(message) { $('map-notice-text').textContent = message; $('map-notice').hidden = !message; }
  function renderSaleDots() {
    salesLayer.clearLayers();
    if (!state.showSales) return;
    const bounds = map.getBounds().pad(.1);
    const visible = state.sales.filter(s => bounds.contains([s.lat,s.lng]));
    $('sales-limit').hidden = visible.length <= 3000;
    $('sales-limit').textContent = visible.length > 3000 ? `Showing 3,000 of ${number(visible.length)} sales in view. Zoom in to see individual homes.` : '';
    for (const sale of visible.slice(0,3000)) {
      const rgb = color(sale.price / sale.sqft), source = saleSource(sale.source_url);
      const dot = L.circleMarker([sale.lat,sale.lng],{pane:'saleDots',radius:map.getZoom() >= 14 ? 5 : 3,weight:1,color:'#ffffff',fillColor:`rgb(${rgb.join(',')})`,fillOpacity:.9});
      dot.bindPopup(() => `<strong>${esc(sale.address)}</strong><br>${esc(sale.city)}<br><strong>${money(sale.price / sale.sqft)} / sqft</strong> · ${money(sale.price)}<br>${number(sale.sqft)} sqft · ${number(sale.beds)} beds · sold ${shortDate(sale.sold_date)}${source ? `<br><a href="${esc(source.href)}" target="_blank" rel="noopener noreferrer">View sold home on ${source.label} ↗</a>` : ''}`);
      dot.on('click',L.DomEvent.stopPropagation); salesLayer.addLayer(dot);
    }
    if (!map.hasLayer(salesLayer)) salesLayer.addTo(map);
  }
  map.on('moveend',renderSaleDots);
  function applyMetadata(data) {
    const meta = {...data,...(data.metadata || {})};
    const hasData = state.sales.length > 0;
    $('status-dot').className = `status-dot ${hasData ? 'ready' : ''}`;
    $('header-status').textContent = hasData ? `${number(state.sales.length)} recent sold homes` : 'Sales data not loaded';
    $('record-count').textContent = `${number(state.sales.length)} eligible sold homes · 2-year window`;
    const updated = meta.updated_at || meta.last_updated;
    $('updated-at').textContent = updated ? `Data updated ${new Date(updated).toLocaleString('en-US',{month:'short',day:'numeric',hour:'numeric',minute:'2-digit'})}` : 'No data refresh completed yet';
    const warnings = Array.isArray(meta.warnings) ? meta.warnings.join(' ') : '';
    const message = meta.message || warnings;
    const sources = [...new Set(state.sales.map(s => saleSource(s.source_url)?.label).filter(Boolean))];
    const hasRedfin = sources.includes('Redfin');
    const partial = meta.coverage?.complete !== true;
    if (!hasData) {
      $('data-notice').hidden = false;
      $('data-notice').innerHTML = `<strong>No sold-home data is loaded yet.</strong>${esc(message || 'Import a Redfin CSV or connect the Zillow data provider. Prices appear only where real, dated sales support an estimate.')}<br><button type="button" id="connect-empty">Manage sales data ↗</button>`;
      $('connect-empty').addEventListener('click',openConnection);
      showMapNotice('The map is ready. Add dated sales to see price shading.');
    } else {
      $('data-notice').hidden = !partial && !hasRedfin;
      $('data-notice').textContent = `${partial ? 'Partial sale coverage. ' : ''}${meta.coverage?.refresh_mode === 'automatic_api' ? `${meta.coverage.counties_completed || 0}/9 county searches finished. Finished searches can still have missing records. View source details for known gaps; Update data shows available import options.` : hasRedfin ? 'Use Update data for the Zillow API connection or to import a newer Redfin CSV.' : 'Use Update data to add recent sales or manage the data connection.'}`;
      showMapNotice(partial ? `Partial snapshot · ${number(state.sales.length)} eligible sales.` : '');
    }
    const coverage = typeof meta.coverage === 'string' ? meta.coverage : (meta.coverage?.description || meta.coverage?.label || 'Coverage is limited to imported sales.');
    $('source-detail').textContent = `Source: ${typeof meta.source === 'string' ? meta.source : `${sources.join(' and ') || 'Imported'} sold-home records`}. ${coverage} ${number(state.sales.length)} eligible homes loaded. ${updated ? `Last updated ${new Date(updated).toLocaleString()}.` : 'No successful data refresh yet.'} ${warnings}`;
    if (data.model?.time_buckets) {
      const buckets = data.model.time_buckets;
      $('method-weights').innerHTML = buckets.map(b => `<span>${esc(b.label)}<strong>${Number(b.weight).toFixed(2)}×</strong></span>`).join('');
      document.querySelectorAll('.weight-bar').forEach((bar,i) => { if (buckets[i]) { bar.style.setProperty('--weight',`${buckets[i].weight * 100}%`); bar.parentElement.title = `${buckets[i].label}: ${buckets[i].weight}× weight`; } });
    }
    if (data.model?.sqft_min) $('method-eligibility').textContent = `Detached single-family homes with ${number(data.model.sqft_min)}–${number(data.model.sqft_max)} interior sqft and at least two bedrooms, sold within the last two calendar years. Homes closer to your selected size and bedroom count carry more weight. Other qualifying homes still contribute; five or more bedrooms are grouped together. Every comparable links to its source record.`;
  }
  async function loadData() {
    try {
      const data = await api('data'); state.data = data;
      state.sales = (data.sales || data.rows || data.properties || []).filter(s => Number.isFinite(Number(s.lat)) && Number.isFinite(Number(s.lng)) && Number(s.price) > 0 && Number(s.sqft) > 0);
      // Transfer only numeric model inputs. Addresses and provenance stay on the main thread.
      const packed = new Float64Array(state.sales.length * 7);
      state.sales.forEach((sale, i) => packed.set([Number(sale.lat),Number(sale.lng),Number(sale.price),Number(sale.sqft),Number(sale.beds),Date.parse(sale.sold_date),Number(sale.time_weight)], i * 7));
      ++state.renderId;
      worker.postMessage({type:'data',packed,asOf:data.model?.as_of || data.as_of,oldestDate:data.model?.oldest_sale_date,weights:data.model?.time_buckets?.map(b => b.weight)},[packed.buffer]);
      applyMetadata(data); renderSaleDots(); scheduleSurface();
      if (state.selected) fetchEstimate();
      return true;
    } catch (error) {
      $('status-dot').className = 'status-dot error'; $('header-status').textContent = 'Data service unavailable';
      $('record-count').textContent = 'Could not reach the local data service';
      $('data-notice').hidden = false; $('data-notice').innerHTML = `<strong>The data service could not be reached.</strong>${esc(error.message)}<br><button type="button" id="retry-data">Try again</button>`;
      $('retry-data').addEventListener('click',loadData);
      showMapNotice('The data service is unavailable. No new estimate can be shown.');
      return false;
    }
  }
  function clearEstimateTooltip() {
    // Leaflet retains closed overlays for a 200ms fade. Retire stale prices
    // immediately, including from the accessibility tree, before that cleanup.
    const element = marker?.getTooltip?.()?.getElement?.();
    if (element) { element.hidden = true; element.setAttribute('aria-hidden','true'); }
    marker?.unbindTooltip();
  }
  async function selectLocation(lat,lng,label,move=true) {
    state.selected = {lat:Number(lat),lng:Number(lng),label:label || `${Number(lat).toFixed(5)}, ${Number(lng).toFixed(5)}`};
    if (marker) { clearEstimateTooltip(); map.removeLayer(marker); }
    marker = L.marker([lat,lng],{icon:L.divIcon({className:'',html:'<span class="selected-marker"></span>',iconSize:[28,28],iconAnchor:[14,14]}),keyboard:true,title:state.selected.label}).addTo(map);
    if (move) {
      const mobile = window.innerWidth <= 700, zoom = Math.max(map.getZoom(),14), point = map.project([lat,lng],zoom);
      const center = point.add(mobile ? [0,map.getSize().y * .18] : [-150,0]);
      map.setView(map.unproject(center,zoom),zoom,{animate:!window.matchMedia('(prefers-reduced-motion: reduce)').matches});
    }
    $('welcome-state').hidden = true; $('estimate-section').hidden = false;
    const selected = state.selected, interaction = panelControls.interaction();
    if (window.innerWidth <= 700) panelControls.setExpanded(true);
    await fetchEstimate();
    if (window.innerWidth <= 700 && state.selected === selected && panelControls.interaction() === interaction && $('explorer-panel').classList.contains('expanded')) {
      requestAnimationFrame(() => {
        const scroll = document.querySelector('.panel-scroll');
        const top = scroll.scrollTop + $('estimate-section').getBoundingClientRect().top - scroll.getBoundingClientRect().top - 12;
        scroll.scrollTo({top:Math.max(0,top),behavior:'auto'});
      });
    }
  }
  function renderEstimate(data) {
    const value = Number(data.estimate_ppsf), good = data.estimate_ppsf != null && Number.isFinite(value) && value > 0;
    const count = data.comparable_count ?? data.comp_count ?? 0;
    const comps = data.comparables || data.comps || [];
    const label = `<p class="estimate-location"><span aria-hidden="true">⌖</span><span>${esc(state.selected.label)}</span></p>`;
    if (!good) {
      $('estimate-section').innerHTML = `${label}<div class="estimate-empty"><h3>More nearby sales<br>would make this clearer.</h3><p>${esc(data.message || 'There are not enough recent comparable sales here to give a supported estimate.')} ${count ? `${number(count)} comparable ${count === 1 ? 'home was' : 'homes were'} found.` : ''}</p></div>`;
      clearEstimateTooltip(); return;
    }
    const low = data.range_ppsf?.low ?? data.low_ppsf, high = data.range_ppsf?.high ?? data.high_ppsf;
    const limited = data.status === 'limited' || ['low','limited'].includes(data.confidence);
    const confidence = limited ? 'Limited evidence' : 'Supported estimate';
    const effective = Number(data.effective_sample_count ?? data.effective_comp_count ?? count);
    const price = data.estimated_price || value * Number($('sqft').value);
    const radius = data.radius_km ? `${data.radius_km.toFixed(2).replace(/0$/,'')} km` : `${Number(data.radius_miles || 0).toFixed(1)} miles`;
    const note = data.message || (Array.isArray(data.notes) ? data.notes.join(' ') : '') || `${number(count)} nearby sold homes, weighted by recency, distance, and similarity.`;
    $('estimate-section').innerHTML = `${label}<div class="estimate-value-row"><p class="estimate-value">${money(value)}<small>/ sqft</small></p><span class="confidence-badge ${limited ? 'limited' : ''}">${confidence}</span></div>${low != null && high != null ? `<p class="estimate-range">Comparable spread ${money(low)}–${money(high)} / sqft</p>` : ''}<div class="price-summary"><span>At ${number($('sqft').value)} interior sqft</span><strong>≈ ${money(Math.round(price / 10000) * 10000)}</strong></div><p class="estimate-note">${esc(note)}</p><p class="evidence-detail">${number(count)} sales · ${effective.toFixed(1)} effective after weighting</p><div class="comps-heading"><h3>${number(count)} nearby comparable sales</h3><span>Within ${esc(radius)}</span></div>${comps.slice(0,6).map(s => {
      const source = saleSource(s.source_url), ppsf = s.price_per_sqft || s.price / s.sqft;
      return `<${source ? 'a' : 'div'} class="comp-row"${source ? ` href="${esc(source.href)}" target="_blank" rel="noopener noreferrer"` : ''}><div><div class="comp-address">${esc(s.address)}${source ? ' ↗' : ''}</div><div class="comp-meta">${number(s.sqft)} sqft · ${number(s.beds)} beds · ${esc(s.city)}<br>Sold ${shortDate(s.sold_date)}${s.distance_miles != null ? ` · ${Number(s.distance_miles).toFixed(1)} mi away` : ''}${source ? ` · ${source.label}` : ''}${String(s.price_evidence || '').toLowerCase().includes('may be rounded') ? ' · Price may be rounded' : ''}</div></div><div class="comp-price">${money(ppsf)}<small>per sqft</small></div></${source ? 'a' : 'div'}>`;
    }).join('')}${comps.length > 6 ? `<p class="estimate-note">Showing the six sales with the most influence on this estimate.</p>` : ''}`;
    if (marker) marker.bindTooltip(`${money(value)} / sqft`,{permanent:true,direction:'top',offset:[0,-16],className:'selected-tooltip'}).openTooltip();
  }
  async function fetchEstimate() {
    if (!state.selected) return;
    const request = ++state.estimateId;
    state.estimateController?.abort(); state.estimateController = new AbortController();
    $('estimate-section').hidden = false;
    $('estimate-section').innerHTML = '<p class="estimate-note"><span class="spinner"></span> Reading the nearby sales…</p>';
    clearEstimateTooltip();
    try {
      const params = new URLSearchParams({lat:state.selected.lat,lng:state.selected.lng,sqft:$('sqft').value,beds:$('beds').value});
      const data = await api(`estimate?${params}`,{signal:state.estimateController.signal}); if (request !== state.estimateId) return; renderEstimate(data);
    } catch (error) { if (request === state.estimateId) $('estimate-section').innerHTML = `<div class="estimate-empty"><h3>Estimate unavailable</h3><p>${esc(error.message)}</p></div>`; }
  }
  function installAddressSearch({input,form,list,status,submit,fetchJson,onSelect}) {
    let results = [], active = -1, timer = null, controller = null, sequence = 0;
    function close() {
      list.hidden = true; input.setAttribute('aria-expanded','false');
      input.removeAttribute('aria-activedescendant'); active = -1;
    }
    function cancel() {
      ++sequence; clearTimeout(timer); controller?.abort(); controller = null;
      input.removeAttribute('aria-busy'); close(); submit.disabled = false;
    }
    function highlight(index) {
      active = index;
      list.querySelectorAll('[role=option]').forEach((button,i) => button.setAttribute('aria-selected',String(i === active)));
      input.setAttribute('aria-activedescendant',`address-option-${active}`);
      list.querySelectorAll('[role=option]')[active]?.scrollIntoView({block:'nearest'});
    }
    function choose(index) {
      const result = results[index]; if (!result) return;
      cancel(); input.value = result.display_name; input.blur(); status.textContent = '';
      const approximate = String(result.precision || '').toLowerCase().startsWith('approximate');
      onSelect(result.lat,result.lng,result.display_name + (approximate ? ' · approximate street location' : ''));
    }
    function show(rows) {
      results = rows.filter(r => r && Number.isFinite(Number(r.lat)) && Number.isFinite(Number(r.lng)) && typeof r.display_name === 'string').slice(0,5);
      close();
      if (!results.length) return;
      list.innerHTML = results.map((r,i) => `<button type="button" role="option" aria-selected="false" tabindex="-1" id="address-option-${i}" data-result="${i}"><span>${esc(r.display_name)}</span>${r.source ? `<small>${esc(r.source)}${String(r.precision || '').toLowerCase().startsWith('approximate') ? ' · approximate street location' : ''}</small>` : ''}</button>`).join('');
      list.querySelectorAll('[role=option]').forEach((button,i) => button.addEventListener('click',() => choose(i)));
      list.hidden = false; input.setAttribute('aria-expanded','true');
    }
    input.addEventListener('input',() => {
      cancel(); status.textContent = '';
      const query = input.value.trim(), request = sequence;
      if (query.length < 6) return;
      timer = setTimeout(async () => {
        controller = new AbortController(); input.setAttribute('aria-busy','true');
        try {
          let data = await fetchJson(`suggest?q=${encodeURIComponent(query)}`,{signal:controller.signal});
          if (data.remote_status === 'busy' && !(data.results || []).length && request === sequence) {
            await new Promise(resolve => setTimeout(resolve,Math.min(2000,Math.max(650,Number(data.retry_after_ms) || 1100))));
            if (request !== sequence || input.value.trim() !== query || document.activeElement !== input) return;
            data = await fetchJson(`suggest?q=${encodeURIComponent(query)}`,{signal:controller.signal});
          }
          if (request !== sequence || input.value.trim() !== query || document.activeElement !== input) return;
          show(data.results || []);
        } catch (error) {
          // Explicit full-address search remains available when suggestions fail.
          if (request === sequence && error.name !== 'AbortError') close();
        } finally { if (request === sequence) input.removeAttribute('aria-busy'); }
      },650);
    });
    input.addEventListener('keydown',event => {
      if (event.key === 'Escape') { cancel(); return; }
      if (list.hidden || !results.length) return;
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault(); highlight((active + (event.key === 'ArrowDown' ? 1 : active < 0 ? 0 : -1) + results.length) % results.length);
      } else if (event.key === 'Enter' && active >= 0) { event.preventDefault(); choose(active); }
    });
    list.addEventListener('mousedown',event => event.preventDefault());
    input.addEventListener('blur',() => setTimeout(() => { if (document.activeElement !== input && !list.contains(document.activeElement)) close(); },150));
    form.addEventListener('submit',async event => {
      event.preventDefault(); const query = input.value.trim(); if (!query) return;
      cancel(); const request = sequence;
      controller = new AbortController();
      submit.disabled = true; status.textContent = 'Finding that address…';
      try {
        const data = await fetchJson(`geocode?q=${encodeURIComponent(query)}`,{signal:controller.signal});
        if (request !== sequence || input.value.trim() !== query) return;
        const rows = data.results || (data.lat ? [data] : []);
        if (!rows.length) { status.textContent = 'No match found. Try a full street address with a city or ZIP code.'; return; }
        status.textContent = '';
        if (rows.length === 1) { const r = rows[0]; cancel(); input.blur(); onSelect(r.lat,r.lng,r.display_name || query); }
        else { show(rows); input.focus(); }
      } catch (error) { if (request === sequence) status.textContent = error.message; }
      finally { if (request === sequence) submit.disabled = false; }
    });
    return {cancel,close};
  }
  const addressSearch = installAddressSearch({input:$('address'),form:$('search-form'),list:$('search-results'),status:$('search-status'),submit:$('search-submit'),fetchJson:api,onSelect:selectLocation});
  map.on('click',event => { addressSearch.cancel(); selectLocation(event.latlng.lat,event.latlng.lng,'Selected map location',false); });
  ['sqft','beds'].forEach(id => $(id).addEventListener('change',() => { scheduleSurface(); if (state.selected) fetchEstimate(); }));
  function setRegion(bayArea) {
    for (const [id,selected] of [['east-bay',!bayArea],['bay-area',bayArea]]) {
      $(id).classList.toggle('active',selected); $(id).setAttribute('aria-pressed',String(selected));
    }
    fitRegion(bayArea ? boundsBay : boundsEast);
  }
  $('east-bay').addEventListener('click',() => setRegion(false));
  $('bay-area').addEventListener('click',() => setRegion(true));
  $('toggle-sales').addEventListener('click',() => { state.showSales = !state.showSales; $('toggle-sales').setAttribute('aria-pressed',String(state.showSales)); if (state.showSales) renderSaleDots(); else { salesLayer.remove(); $('sales-limit').hidden = true; } });
  $('toggle-surface').addEventListener('click',() => { state.surface = !state.surface; $('toggle-surface').setAttribute('aria-pressed',String(state.surface)); renderSurface(); });
  function openMethod() { $('method-dialog').showModal(); }
  $('source-button').addEventListener('click',openMethod); $('method-button').addEventListener('click',openMethod);
  $('close-method').addEventListener('click',() => $('method-dialog').close());
  function installPanelDrag(panel,handle,header) {
    let drag = null, suppressClickUntil = 0, interactions = 0;
    function setExpanded(expanded, manual = false) {
      if (manual) ++interactions;
      panel.classList.toggle('expanded',expanded);
      handle.setAttribute('aria-expanded',String(expanded));
      handle.setAttribute('aria-label',expanded ? 'Collapse details; drag down to see the map' : 'Expand details; drag up');
      panel.style.height = ''; panel.classList.remove('dragging');
    }
    function begin(event) {
      if (window.innerWidth > 700 || event.isPrimary === false || (event.pointerType === 'mouse' && event.button !== 0)) return;
      if (event.target.closest('input,select,a') || drag) return;
      drag = {id:event.pointerId,x:event.clientX,y:event.clientY,height:panel.getBoundingClientRect().height,expanded:panel.classList.contains('expanded'),moved:false};
      handle.setPointerCapture?.(event.pointerId);
    }
    function move(event) {
      if (!drag || event.pointerId !== drag.id) return;
      const dy = event.clientY - drag.y, dx = event.clientX - drag.x;
      if (!drag.moved && (Math.abs(dy) < 6 || Math.abs(dy) < Math.abs(dx))) return;
      drag.moved = true; event.preventDefault(); panel.classList.add('dragging');
      const available = panel.parentElement.clientHeight;
      const low = Math.max(230,Math.min(520,available * .43)), high = Math.max(low,available * .75);
      panel.style.height = `${Math.max(low,Math.min(high,drag.height - dy))}px`;
    }
    function end(event) {
      if (!drag || event.pointerId !== drag.id) return;
      const previous = drag; drag = null;
      if (handle.hasPointerCapture?.(event.pointerId)) handle.releasePointerCapture(event.pointerId);
      if (!previous.moved) return;
      suppressClickUntil = Date.now() + 500;
      const dy = event.clientY - previous.y;
      const expanded = event.type === 'pointercancel' ? previous.expanded : Math.abs(dy) >= 35 ? dy < 0 : previous.expanded;
      setExpanded(expanded,true);
    }
    for (const target of [handle,header]) {
      target.addEventListener('pointerdown',begin);
      target.addEventListener('pointermove',move);
      target.addEventListener('pointerup',end);
      target.addEventListener('pointercancel',end);
    }
    handle.addEventListener('click',event => {
      if (event.detail !== 0 && Date.now() < suppressClickUntil) { event.preventDefault(); return; }
      setExpanded(!panel.classList.contains('expanded'),true);
    });
    return {setExpanded,interaction:() => interactions};
  }
  const panelControls = installPanelDrag($('explorer-panel'),$('panel-toggle'),document.querySelector('.intro'));
  for (const dialog of [$('method-dialog'),$('connection-dialog')]) dialog.addEventListener('click',event => { if (event.target === dialog) { const rect = dialog.getBoundingClientRect(); if (event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom) dialog.close(); } });
  let connectedProvider = null;
  function providerControls() {
    const apify = $('provider-kind').value === 'apify';
    $('provider-description').textContent = apify ? 'Search dated Zillow sold records across all nine counties, including unincorporated areas. Progress is saved so an interrupted import can continue. Search and spending limits can leave gaps.' : 'Fetch Zillow sold records through HouseData on RapidAPI. Its city searches can omit unincorporated areas and hit result limits.';
    $('apify-limits').hidden = !apify; $('housedata-limits').hidden = apify;
    $('provider-key-name').textContent = apify ? 'Apify API token' : 'RapidAPI key';
    $('provider-scope').disabled = apify;
    if (apify) $('provider-scope').value = 'bay_area';
    const saved = connectedProvider?.provider_kind === $('provider-kind').value && connectedProvider.configured;
    const p = connectedProvider;
    const allowance = !apify ? null : p?.provider_kind === 'apify' ? p.allowance
      : p?.apify_allowance_mode === 'paid' ? {mode:'paid',state:'saved'} : null;
    const paid = allowance?.mode === 'paid';
    const paused = paid && !['active','publication_pending'].includes(allowance.state);
    $('apify-paid-settings').hidden = !paid;
    $('apify-free-settings').hidden = paid;
    $('apify-free-plan').required = false;
    for (const id of ['apify-free-plan','apify-run-cap','apify-credit-cap','apify-run-count']) $(id).disabled = paid;
    if (paid) {
      const money = value => `$${Number(value).toFixed(2)}`;
      const bounds = allowance.total_usd == null ? 'One-time allowance' : `${money(allowance.total_usd)} one-time spending limit`;
      const usage = allowance.used_usd == null ? '' : ` ${money(allowance.used_usd)} committed or reserved; ${money(allowance.remaining_usd)} remains within that limit.`;
      const credits = allowance.usage_total_usd == null ? '' : ` Data usage: ${money(allowance.usage_reserved_usd || 0)} reserved of ${money(allowance.usage_total_usd)}.`;
      const expiry = allowance.expires_at ? ` Expires ${new Date(allowance.expires_at).toLocaleString()}.` : '';
      const messages = {exhausted:'Refresh paused: the remaining allowance cannot fund the next county.',expired:'Refresh paused: this allowance has expired.',disabled:'Refresh paused: paid refresh is disabled. Saved sales remain available. A new paid import requires a separately approved allowance.',invalid:'Refresh paused: this allowance needs verification on the server.',completed:'The one-time import is finished. A new paid refresh needs a new allowance.',paused:'Refresh paused: the saved provider run needs review.',saved:'Save the Apify connection to view its existing allowance.',publication_pending:'Downloaded results are ready to publish. This uses saved data and makes no new provider request.'};
      $('apify-allowance-status').textContent = `${bounds}.${usage}${credits}${expiry} ${messages[allowance.state] || ''}`;
    }
    $('apify-results-note').textContent = apify && p?.provider_kind === 'apify' && p.apify_max_items_per_county
      ? `Each county search is limited to ${number(p.apify_max_items_per_county)} homes. Finished searches may still be partial; they do not establish complete coverage.` : '';
    if (p?.provider_kind !== $('provider-kind').value) $('provider-status').textContent = 'Save this provider connection before refreshing. The previously saved connection is still active.';
    else if (p) $('provider-status').textContent = `${p.configured ? (p.live_verified ? 'API connection verified.' : 'Provider key saved; live access not yet verified.') : 'No provider key is configured yet.'} ${p.budget ? `${p.budget.counties_completed || 0}/9 county searches finished; $${Number(p.budget.credit_reserved_usd || 0).toFixed(2)} of the credit allowance reserved.` : ''} ${p.calls_used != null ? `${number(p.calls_used)} calls used in the last 31 days.` : ''} ${p.message || ''}`;
    $('key-optional').textContent = saved ? '(optional to replace)' : '(required)';
    $('provider-key').required = !saved;
    $('provider-pricing').href = apify ? 'https://apify.com/topapi/zillow-scraper/pricing' : 'https://rapidapi.com/bachhungcong/api/zillow-full-data-api/pricing';
    $('provider-refresh-button').disabled = state.refreshRunning || state.importing || !saved || paused;
    $('provider-refresh-label').textContent = state.refreshRunning ? 'Refreshing…' : allowance?.state === 'publication_pending' ? 'Publish saved results' : 'Refresh sales';
  }
  $('provider-kind').addEventListener('change',providerControls);
  async function openConnection() {
    $('method-dialog').close(); $('connection-dialog').showModal(); $('provider-message').textContent = ''; $('provider-key').value = '';
    try {
      const p = await api('provider'); connectedProvider = p; state.providerConfigured = Boolean(p.configured);
      $('provider-kind').value = p.provider_kind || 'housedata';
      $('provider-scope').value = p.scope || 'east_bay'; $('refresh-cap').value = p.max_calls_per_refresh || 900; $('monthly-cap').value = p.monthly_call_limit || 900;
      $('apify-run-cap').value = p.apify_max_cost_per_run_usd || 0.08;
      $('apify-credit-cap').value = p.apify_credit_budget_usd || 0.10;
      $('apify-run-count').value = p.apify_max_runs_per_refresh || 1;
      $('apify-free-plan').checked = Boolean(p.apify_free_plan_verified);
      $('provider-status').textContent = `${p.configured ? (p.live_verified ? 'API connection verified.' : 'Provider key saved; live access not yet verified.') : 'No provider key is configured yet.'} ${p.calls_used != null ? `${number(p.calls_used)} calls used in the last 31 days.` : ''} ${p.message || ''}`;
      providerControls();
    } catch (error) { $('provider-status').textContent = error.message; }
  }
  $('open-connection').addEventListener('click',openConnection); $('update-data-button').addEventListener('click',openConnection); $('close-connection').addEventListener('click',() => $('connection-dialog').close());
  $('connection-dialog').addEventListener('close',() => { $('provider-key').value = ''; });
  $('provider-form').addEventListener('submit',async event => {
    event.preventDefault(); $('save-provider').disabled = true; $('provider-message').textContent = 'Saving the connection…';
    const body = {provider_kind:$('provider-kind').value,scope:$('provider-scope').value};
    if (body.provider_kind === 'apify') {
      if (connectedProvider?.allowance?.mode !== 'paid' && connectedProvider?.apify_allowance_mode !== 'paid') Object.assign(body,{apify_max_cost_per_run_usd:Number($('apify-run-cap').value),apify_credit_budget_usd:Number($('apify-credit-cap').value),apify_max_runs_per_refresh:Number($('apify-run-count').value),apify_free_plan_verified:$('apify-free-plan').checked});
    } else Object.assign(body,{max_calls_per_refresh:Number($('refresh-cap').value),monthly_call_limit:Number($('monthly-cap').value)});
    if ($('provider-key').value.trim()) body.api_key = $('provider-key').value.trim();
    try { const p = await api('provider',{method:'POST',body:JSON.stringify(body)}); connectedProvider = p; $('provider-key').value = ''; state.providerConfigured = p.configured !== false; $('provider-message').textContent = 'Connection saved. Available refresh and import options are shown below.'; $('provider-status').textContent = 'Provider key saved. A successful refresh will verify the connection.'; providerControls(); }
    catch (error) { $('provider-message').textContent = error.message; }
    finally { $('save-provider').disabled = false; }
  });
  $('redfin-import-form').addEventListener('submit',async event => {
    event.preventDefault();
    const file = $('redfin-csv').files[0];
    if (!file || state.importing || state.refreshRunning) return;
    const message = $('redfin-import-message');
    if (!file.name.toLowerCase().endsWith('.csv')) { message.textContent = 'Choose a CSV downloaded from Redfin.'; return; }
    if (!file.size || file.size > 5 * 1024 * 1024) { message.textContent = 'Choose a nonempty CSV no larger than 5 MB.'; return; }
    state.importing = true; $('import-redfin').disabled = true; $('redfin-csv').disabled = true; $('provider-refresh-button').disabled = true;
    message.textContent = 'Checking the CSV and importing eligible sold homes…';
    try {
      const csv = await file.text();
      const result = await api('import/redfin',{method:'POST',body:JSON.stringify({csv,filename:file.name})});
      const reloaded = await loadData();
      const counts = result.counts;
      const summary = counts ? `${number(counts.eligible)} eligible rows; ${number(counts.added)} sales added and ${number(counts.updated)} updated.` : 'Redfin snapshot imported.';
      message.textContent = `${summary} ${reloaded ? `${number(state.sales.length)} eligible sales are now loaded on the map.` : 'The import completed, but the map could not reload. Reload this page to try again.'} ${Array.isArray(result.warnings) ? result.warnings.join(' ') : ''}`;
      $('redfin-csv').value = '';
    } catch (error) { message.textContent = error.message; }
    finally { state.importing = false; $('import-redfin').disabled = state.refreshRunning; $('redfin-csv').disabled = false; providerControls(); }
  });
  function applyRefresh(info) {
    const running = info.state === 'running'; state.refreshRunning = running;
    clearTimeout(state.refreshTimer);
    providerControls(); $('import-redfin').disabled = running || state.importing;
    if (running) {
      const p = info.progress, message = `${info.message || 'Fetching Zillow sold records…'}${p?.total ? ` ${p.completed || 0} / ${p.total}` : ''}`;
      showMapNotice(message); $('provider-message').textContent = message;
      state.refreshTimer = setTimeout(checkRefresh,document.hidden ? 30000 : 5000);
    } else if (info.state === 'error' || info.state === 'partial' || info.state === 'busy') { const message = info.message || 'The data refresh paused. Existing sales have been retained.'; showMapNotice(message); $('provider-message').textContent = message; }
    return running;
  }
  async function reloadProviderStatus() {
    connectedProvider = await api('provider');
    providerControls();
  }
  async function checkRefresh() {
    try {
      const result = await api('refresh'), running = applyRefresh(result);
      if (!running) await reloadProviderStatus();
      if (!running && ['success','partial'].includes(result.state)) {
        await loadData();
        const message = result.message || 'Saved Zillow sales have been loaded.';
        showMapNotice(message); $('provider-message').textContent = message;
      }
    } catch (error) {
      providerControls(); showMapNotice(error.message); $('provider-message').textContent = error.message;
      // A failed status read must not abandon an import still running on the server.
      // This only retries GET status; it never retries a provider refresh request.
      if (state.refreshRunning) {
        clearTimeout(state.refreshTimer);
        state.refreshTimer = setTimeout(checkRefresh,document.hidden ? 30000 : 5000);
      }
    }
  }
  $('provider-refresh-button').addEventListener('click',async () => {
    if (!connectedProvider?.configured || connectedProvider.provider_kind !== $('provider-kind').value || $('provider-key').value.trim()) { $('provider-message').textContent = 'Save the connection before refreshing.'; return; }
    const publishSaved = connectedProvider.allowance?.state === 'publication_pending';
    $('provider-refresh-button').disabled = true; $('provider-refresh-label').textContent = publishSaved ? 'Publishing…' : 'Connecting…';
    $('provider-message').textContent = publishSaved ? 'Publishing previously downloaded results…' : 'Connecting to the Zillow provider…';
    try { const result = await api('refresh',{method:'POST',body:'{}'}); const running = applyRefresh(result); if (!running) await reloadProviderStatus(); if (result.state === 'unconfigured' || result.configured === false) { const message = result.message || 'Connect a Zillow data provider before refreshing.'; showMapNotice(message); $('provider-message').textContent = message; } else if (!running && ['success','partial'].includes(result.state)) { await loadData(); const message = result.message || 'Saved Zillow sales have been loaded.'; showMapNotice(message); $('provider-message').textContent = message; } }
    catch (error) { providerControls(); showMapNotice(error.message); $('provider-message').textContent = error.message; }
  });
  document.addEventListener('keydown',event => { if (event.key === 'Escape') addressSearch.cancel(); });
  window.addEventListener('resize',() => map.invalidateSize());
  loadData();
  api('refresh').then(applyRefresh).catch(() => {});
  // Reload local data only. New provider requests require the explicit refresh button.
  setInterval(() => { if (!document.hidden) loadData(); },300000);
})();
