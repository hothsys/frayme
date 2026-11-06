// Previous scans: saved results kept in the browser (IndexedDB).

// ── Previous scans (IndexedDB) ──

// Generic rather than the app's name, so renaming the app doesn't strand
// saved scans in a database the page no longer opens
const DB_NAME  = 'scan-history';
const DB_STORE = 'scan';
const MAX_SCANS = 10;

function openDB() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(DB_NAME, 1);
    req.onupgradeneeded = e => e.target.result.createObjectStore(DB_STORE);
    req.onsuccess = e => resolve(e.target.result);
    req.onerror   = e => reject(e.target.error);
  });
}

function idbReq(req) {
  return new Promise((resolve, reject) => {
    req.onsuccess = e => resolve(e.target.result);
    req.onerror   = e => reject(e.target.error);
  });
}

async function saveScan() {
  try {
    const db  = await openDB();
    const ts  = Date.now();
    const dir = document.getElementById('dir-input').value.trim();

    const scanned = parseInt(document.getElementById('s-scanned').textContent.replace(/,/g, '')) || 0;

    const tx1 = db.transaction(DB_STORE, 'readwrite');
    await idbReq(tx1.objectStore(DB_STORE).put({ ts, dir, folderData, scanned }, ts));

    // Prune oldest beyond MAX_SCANS
    const tx2   = db.transaction(DB_STORE, 'readwrite');
    const store = tx2.objectStore(DB_STORE);
    const keys  = (await idbReq(store.getAllKeys())).sort((a,b) => a - b);
    while (keys.length > MAX_SCANS) store.delete(keys.shift());

    const scans = await loadSavedScans();
    populateScanDropdown(scans, ts);
  } catch(e) {}
}

async function loadSavedScans() {
  try {
    const db  = await openDB();
    const all = await idbReq(db.transaction(DB_STORE, 'readonly').objectStore(DB_STORE).getAll());
    return all.sort((a,b) => b.ts - a.ts);
  } catch(e) { return []; }
}

async function loadScan(ts) {
  try {
    const db = await openDB();
    return await idbReq(db.transaction(DB_STORE, 'readonly').objectStore(DB_STORE).get(ts));
  } catch(e) { return null; }
}

async function clearAllScans() {
  try {
    const db = await openDB();
    await idbReq(db.transaction(DB_STORE, 'readwrite').objectStore(DB_STORE).clear());
  } catch(e) {}
  document.getElementById('prev-scans-row').style.display = 'none';
  Object.keys(folderData).forEach(k => delete folderData[k]);
  document.getElementById('folder-list').innerHTML = '';
  hideScanResults();
}

async function loadSelectedScan(sel) {
  const ts = parseInt(sel.value);
  if (!ts) return;
  const saved = await loadScan(ts);
  if (!saved) {
    alert('Could not load this scan. It may have been cleared. Try rescanning.');
    return;
  }
  Object.keys(folderData).forEach(k => delete folderData[k]);
  document.getElementById('folder-list').innerHTML = '';
  hideScanResults();
  restoreScan(saved);
  watchMount(saved.dir);

  // Export runs on the server, so hand it the loaded results
  const results = [].concat(...Object.values(folderData).map(fd => fd.issues));
  fetch('/api/load-scan', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ results: results }),
  }).catch(err => console.error('Loading scan on the server failed:', err));
}

function populateScanDropdown(scans, selectTs) {
  const row = document.getElementById('prev-scans-row');
  const sel = document.getElementById('prev-scans-select');
  if (!scans || scans.length === 0) { row.style.display = 'none'; return; }

  // Exact date and time in the user's locale, e.g. "Oct 26, 2026, 2:15 PM"
  const when = ts => new Date(ts).toLocaleString(undefined, {
    year: 'numeric', month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' });
  sel.innerHTML = '';
  if (!selectTs) {
    const placeholder = document.createElement('option');
    placeholder.value = '';
    placeholder.textContent = 'Select a scan to load…';
    placeholder.disabled = placeholder.selected = true;
    sel.appendChild(placeholder);
  }
  [...scans].sort((a, b) => b.ts - a.ts).forEach(scan => {   // most recent first
    const opt  = document.createElement('option');
    opt.value  = scan.ts;
    const parts = (scan.dir || '').split('/').filter(Boolean);
    const short = parts.length > 2 ? '…/' + parts.slice(-2).join('/') : scan.dir || '';
    opt.textContent = when(scan.ts) + (short ? ' · ' + short : '');
    opt.title = scan.dir || '';
    sel.appendChild(opt);
  });
  if (selectTs) sel.value = selectTs;
  row.style.display = 'flex';
}

function restoreScan(saved) {
  Object.assign(folderData, saved.folderData);
  if (saved.dir) document.getElementById('dir-input').value = saved.dir;

  document.getElementById('results-section').style.display = 'block';
  for (const [dir, fd] of Object.entries(folderData)) {
    upsertFolderGroup(dir);
    for (const ev of fd.issues) appendIssueRow(dir, ev);
  }

  const total = Object.values(folderData).reduce((s,f) => s+f.total, 0);
  updateSummary(total, saved.scanned || 0);
  document.getElementById('export-bar').style.display = 'flex';

  applyFilter();
}
