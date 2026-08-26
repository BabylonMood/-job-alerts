'use strict';

const STATUS_KEY = 'job-alert-status';
const PREFS_KEY = 'job-alert-prefs';
const AUTO_REFRESH_MS = 15 * 60 * 1000;
const MAX_AGE_DAYS = 14;

const SOURCE_LABELS = {
  linkedin: 'LinkedIn',
  computrabajo: 'Computrabajo',
  bumeran: 'Bumeran',
  zonajobs: 'ZonaJobs',
  indeed: 'Indeed'
};

const STATUS_LABELS = {
  new: 'Nuevo',
  reviewed: 'Revisado',
  discarded: 'Descartado'
};

let JOBS = [];
let STATUS = {};

const $ = (id) => document.getElementById(id);

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

function loadJSON(key, fallback) {
  try {
    const raw = localStorage.getItem(key);
    return raw ? JSON.parse(raw) : fallback;
  } catch (e) {
    return fallback;
  }
}

function saveJSON(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch (e) {
    console.warn('No se pudo guardar localmente', e);
  }
}

function timeAgo(iso) {
  const then = new Date(iso);
  if (isNaN(then)) return '—';
  const min = Math.floor((Date.now() - then.getTime()) / 60000);
  if (min < 1) return 'recién';
  if (min < 60) return 'hace ' + min + ' min';
  const h = Math.floor(min / 60);
  if (h < 24) return 'hace ' + h + ' h';
  const d = Math.floor(h / 24);
  return 'hace ' + d + ' d';
}

function formatDate(iso) {
  const d = new Date(iso);
  if (isNaN(d)) return '—';
  return d.toLocaleString('es-AR', { day: '2-digit', month: '2-digit', hour: '2-digit', minute: '2-digit' });
}

function renderStats() {
  $('stat-total').textContent = JOBS.length;
  $('stat-new').textContent = JOBS.filter((j) => STATUS[j.id] === 'new').length;
  $('stat-sources').textContent = new Set(JOBS.map((j) => j.source)).size;
  $('stat-last').textContent = JOBS[0] ? formatDate(JOBS[0].found_at) : '—';
}

function populateSourceFilter() {
  const sel = $('filter-source');
  sel.innerHTML = '<option value="">Todas las fuentes</option>';
  const sources = [...new Set(JOBS.map((j) => j.source))];
  sources.forEach((s) => {
    const opt = document.createElement('option');
    opt.value = s;
    opt.textContent = SOURCE_LABELS[s] || s;
    sel.appendChild(opt);
  });
  sel.value = loadJSON(PREFS_KEY, {}).source || '';
}

function restorePrefs() {
  const p = loadJSON(PREFS_KEY, {});
  $('search').value = p.search || '';
  $('filter-status').value = p.status || '';
  $('hide-discarded').checked = p.hideDiscarded !== false;
  $('hide-old').checked = p.hideOld !== false;
  $('filter-source').value = p.source || '';
}

function persistPrefs() {
  saveJSON(PREFS_KEY, {
    search: $('search').value,
    source: $('filter-source').value,
    status: $('filter-status').value,
    hideDiscarded: $('hide-discarded').checked,
    hideOld: $('hide-old').checked
  });
}

function buildDraft(job) {
  const kws = (job.matched_keywords || []).join(', ');
  return [
    'Asunto: Postulación - ' + job.title,
    '',
    'Hola,',
    '',
    'Me contacto en referencia a la búsqueda de ' + job.title +
      ' publicada en ' + (SOURCE_LABELS[job.source] || job.source) + '.',
    '',
    'Adjunto mi CV. Cuento con experiencia relacionada a ' + kws +
      ', y me interesa mucho la oportunidad en ' + job.company + '.',
    '',
    'Quedo atento/a a sus comentarios.',
    '',
    'Saludos cordiales,',
    '[Tu nombre]'
  ].join('\n');
}

function jobHTML(job) {
  const status = STATUS[job.id] || 'new';
  const kwChips = (job.matched_keywords || [])
    .map((k) => '<span class="kw">' + esc(k) + '</span>')
    .join('');
  const sourceName = SOURCE_LABELS[job.source] || esc(job.source) || 'otro';
  return [
    '<div class="job-top">',
    '  <span class="badge ' + esc(job.source) + '"><span class="dot"></span>' + esc(sourceName) + '</span>',
    '  <span class="status-tag ' + esc(status) + '">' + STATUS_LABELS[status] + '</span>',
    '</div>',
    '<h3>' + esc(job.title) + '</h3>',
    '<div class="company">' + esc(job.company) + '</div>',
    '<div class="meta"><span>' + timeAgo(job.found_at) + '</span><span>' + formatDate(job.found_at) + '</span></div>',
    '<div class="snippet">' + esc(job.snippet || '') + '</div>',
    '<div class="kw-row">' + kwChips + '</div>',
    '<div class="job-actions">',
    '  <a href="' + esc(job.url) + '" target="_blank" rel="noopener">Ver oferta &#8599;</a>',
    '  <button type="button" class="draft-btn">Generar borrador de mail</button>',
    '  <select class="status-select">',
    '    <option value="new" ' + (status === 'new' ? 'selected' : '') + '>Nuevo</option>',
    '    <option value="reviewed" ' + (status === 'reviewed' ? 'selected' : '') + '>Revisado</option>',
    '    <option value="discarded" ' + (status === 'discarded' ? 'selected' : '') + '>Descartado</option>',
    '  </select>',
    '</div>',
    '<div class="draft-box">',
    '  <textarea></textarea>',
    '  <div class="draft-actions">',
    '    <button type="button" class="copy-btn">Copiar texto</button>',
    '    <a class="link-btn" href="#">Abrir en mi correo</a>',
    '  </div>',
    '</div>'
  ].join('\n');
}

function filteredJobs() {
  const q = $('search').value.trim().toLowerCase();
  const src = $('filter-source').value;
  const st = $('filter-status').value;
  const hideDiscarded = $('hide-discarded').checked;
  const hideOld = $('hide-old').checked;
  const cutoff = Date.now() - MAX_AGE_DAYS * 86400000;
  return JOBS.filter((j) => {
    const status = STATUS[j.id] || 'new';
    const hay = (j.title + ' ' + (j.company || '') + ' ' + (j.matched_keywords || []).join(' ')).toLowerCase();
    const isOld = new Date(j.found_at).getTime() < cutoff;
    return (
      (!q || hay.includes(q)) &&
      (!src || j.source === src) &&
      (!st || status === st) &&
      (!hideDiscarded || status !== 'discarded') &&
      (!hideOld || !isOld)
    );
  });
}

function render() {
  const list = $('job-list');
  const filtered = filteredJobs();

  list.innerHTML = '';
  $('empty-state').style.display = filtered.length ? 'none' : 'block';

  filtered.forEach((job) => {
    const el = document.createElement('article');
    el.className = 'job';
    el.dataset.id = job.id;
    el.innerHTML = jobHTML(job);
    bindCard(el, job);
    list.appendChild(el);
  });
}

function bindCard(el, job) {
  const draftBox = el.querySelector('.draft-box');
  const textarea = el.querySelector('textarea');
  textarea.value = buildDraft(job);

  el.querySelector('.draft-btn').addEventListener('click', () => {
    draftBox.classList.toggle('open');
  });

  el.querySelector('.copy-btn').addEventListener('click', async () => {
    const ok = await copyText(textarea.value);
    const btn = el.querySelector('.copy-btn');
    const old = btn.textContent;
    btn.textContent = ok ? 'Copiado' : 'No se pudo copiar';
    setTimeout(() => { btn.textContent = old; }, 1500);
  });

  el.querySelector('.link-btn').addEventListener('click', (e) => {
    e.preventDefault();
    const lines = textarea.value.split('\n');
    const subject = lines[0].replace(/^Asunto:\s*/, '').trim();
    const body = lines.slice(1).join('\n').trim();
    window.location.href = 'mailto:?subject=' + encodeURIComponent(subject) + '&body=' + encodeURIComponent(body);
  });

  el.querySelector('.status-select').addEventListener('change', (e) => {
    setStatus(job.id, e.target.value);
    if ($('hide-discarded').checked && e.target.value === 'discarded') {
      el.remove();
      $('empty-state').style.display = filteredJobs().length ? 'none' : 'block';
    }
  });
}

async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (e2) { ok = false; }
    ta.remove();
    return ok;
  }
}

function setStatus(id, status) {
  STATUS[id] = status;
  saveJSON(STATUS_KEY, STATUS);
  const card = document.querySelector('.job[data-id="' + CSS.escape(id) + '"] .status-tag');
  if (card) {
    card.className = 'status-tag ' + status;
    card.textContent = STATUS_LABELS[status];
  }
  renderStats();
}

async function loadJobs() {
  try {
    const res = await fetch('jobs.json?_=' + Date.now());
    if (!res.ok) throw new Error('no jobs.json');
    const data = await res.json();
    JOBS = Array.isArray(data) ? data : [];
    JOBS.sort((a, b) => new Date(b.found_at) - new Date(a.found_at));
  } catch (e) {
    $('job-list').innerHTML =
      '<div class="empty">No pude leer jobs.json (¿lo estás abriendo con doble clic? los ' +
      'navegadores bloquean fetch() sobre archivos locales). Corré ' +
      '<code>python3 -m http.server</code> en esta carpeta y abrí ' +
      '<code>http://localhost:8000</code>, o publicá la carpeta con GitHub Pages.</div>';
    return;
  }
  const stored = loadJSON(STATUS_KEY, {});
  STATUS = {};
  JOBS.forEach((j) => { STATUS[j.id] = stored[j.id] || j.status || 'new'; });
  populateSourceFilter();
  renderStats();
  render();
}

function resetStatus() {
  if (!confirm('¿Restablecer todos los estados a "Nuevo" en este navegador?')) return;
  STATUS = {};
  saveJSON(STATUS_KEY, {});
  renderStats();
  render();
}

function exportStatus() {
  const blob = new Blob([JSON.stringify(STATUS, null, 2)], { type: 'application/json' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'job-alert-status.json';
  a.click();
  URL.revokeObjectURL(a.href);
}

function markAllReviewed() {
  const changed = [];
  JOBS.forEach((j) => {
    if ((STATUS[j.id] || 'new') === 'new') { STATUS[j.id] = 'reviewed'; changed.push(j.id); }
  });
  if (!changed.length) return;
  saveJSON(STATUS_KEY, STATUS);
  renderStats();
  render();
}

function init() {
  restorePrefs();

  let debounce;
  $('search').addEventListener('input', () => {
    persistPrefs();
    clearTimeout(debounce);
    debounce = setTimeout(render, 150);
  });
  $('filter-source').addEventListener('change', () => { persistPrefs(); render(); });
  $('filter-status').addEventListener('change', () => { persistPrefs(); render(); });
  $('hide-discarded').addEventListener('change', () => { persistPrefs(); render(); });
  $('hide-old').addEventListener('change', () => { persistPrefs(); render(); });
  $('btn-refresh').addEventListener('click', () => {
    loadJobs();
  });
  $('btn-mark-all').addEventListener('click', markAllReviewed);
  $('btn-reset-status').addEventListener('click', resetStatus);
  $('btn-export-status').addEventListener('click', exportStatus);

  setInterval(loadJobs, AUTO_REFRESH_MS);
  loadJobs();
}

init();
