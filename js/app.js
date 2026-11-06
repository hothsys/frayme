let eventSource = null;
let currentFilter = 'all';
let ffmpegAvailable = false;

const folderData = {};
const donorMap = {};
const recoveredFiles = {};

async function checkFfmpeg() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    ffmpegAvailable = d.ffmpeg;
    defaultOutputDir = d.default_output_dir || '';
    showOutputLabel();
    var badge = document.getElementById('ffmpeg-badge');
    if (d.ffmpeg) {
      badge.textContent = 'ffmpeg ready';
      badge.style.color = 'var(--green)';
      badge.style.borderColor = 'rgba(76,175,130,.3)';
    } else {
      badge.textContent = 'ffmpeg missing — click for help';
      badge.style.color = 'var(--amber)';
      badge.style.borderColor = 'rgba(245,166,35,.3)';
      badge.style.cursor = 'pointer';
      badge.onclick = function() {
        document.getElementById('ffmpeg-overlay').classList.add('open');
      };
      document.getElementById('ffmpeg-overlay').classList.add('open');
    }
  } catch(e) {
    document.getElementById('ffmpeg-badge').textContent = 'status error';
  }
}

// ── Browse modal ──

var browseCurrentPath = '/';

// Damaged file whose donor folder is being chosen; null when the browser
// picks the scan directory
var donorFolderFor = null;
// True while the browser picks the folder recovered videos are saved to
var choosingOutput = false;

// Output folder for recovered videos: the user's choice (remembered in this
// browser), else the server's default (~/Downloads)
var defaultOutputDir = '';
var outputDir = '';
try { outputDir = localStorage.getItem('outputDir') || ''; } catch(e) {}

function currentOutputDir() {
  return outputDir || defaultOutputDir;
}

function setOutputDir(path) {
  outputDir = (path && path !== defaultOutputDir) ? path : '';
  try {
    if (outputDir) localStorage.setItem('outputDir', outputDir);
    else localStorage.removeItem('outputDir');
  } catch(e) {}
  showOutputLabel();
}

function showOutputLabel() {
  var el = document.getElementById('output-dir');
  var dir = currentOutputDir();
  el.textContent = dir ? dir + (outputDir ? '' : '  (default)') : 'Downloads folder';
  el.title = dir;
}

function showOutputDir() {
  fetch('/api/reveal', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filepath: currentOutputDir(), open: true }),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.error) alert('Could not open ' + currentOutputDir() + ': ' + d.error);
  })
  .catch(function(err) { console.error('Opening the output folder failed:', err); });
}

function openOutputBrowse() {
  choosingOutput = true;
  document.querySelector('#browse-overlay .browse-top h3').textContent =
    'Choose a Folder for Recovered Videos';
  document.getElementById('browse-overlay').classList.add('open');
  browseTo(currentOutputDir() || '/Volumes');
}

function openBrowse() {
  document.getElementById('browse-overlay').classList.add('open');
  var input = document.getElementById('dir-input').value.trim();
  browseTo(input || '/Volumes');
}

function openDonorBrowse(filepath) {
  donorFolderFor = filepath;
  document.querySelector('#browse-overlay .browse-top h3').textContent =
    'Choose a Folder of Healthy Videos from the Same Camera';
  document.getElementById('browse-overlay').classList.add('open');
  browseTo(filepath.substring(0, filepath.lastIndexOf('/')) || '/Volumes');
}

function closeBrowse() {
  document.getElementById('browse-overlay').classList.remove('open');
  donorFolderFor = null;
  choosingOutput = false;
  document.querySelector('#browse-overlay .browse-top h3').textContent = 'Choose Directory';
}

function selectBrowseDir() {
  if (choosingOutput) {
    setOutputDir(browseCurrentPath);
    closeBrowse();
    return;
  }
  if (donorFolderFor) {
    addDonorFolder(donorFolderFor, browseCurrentPath);
    closeBrowse();
    return;
  }
  document.getElementById('dir-input').value = browseCurrentPath;
  watchMount(browseCurrentPath);
  closeBrowse();
}

function addDonorFolder(filepath, folder) {
  var btn = document.querySelector('[data-donor-folder="' + CSS.escape(filepath) + '"]');
  if (btn) { btn.textContent = 'Checking folder…'; btn.disabled = true; }
  fetch('/api/add-donor-folder', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filepath: filepath, donor_dir: folder }),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (btn) { btn.textContent = 'Choose Donor Folder'; btn.disabled = false; }
    if (!d.ok || !d.found) {
      alert(d.error || ('No healthy MP4/MOV videos found in ' + folder +
        ' (' + (d.checked || 0) + ' video files checked).'));
      return;
    }
    // Retry with the new candidates
    var recBtn = document.querySelector('[data-recover="' + CSS.escape(filepath) + '"]');
    if (recBtn) recoverVideo(recBtn);
  })
  .catch(function(err) {
    if (btn) { btn.textContent = 'Choose Donor Folder'; btn.disabled = false; }
    console.error('Adding donor folder failed:', err);
  });
}

function browseTo(path) {
  browseCurrentPath = path;
  var list = document.getElementById('browse-list');
  list.innerHTML = '<div style="padding:20px;color:var(--text3)">Loading…</div>';

  fetch('/api/browse?path=' + encodeURIComponent(path))
    .then(r => r.json())
    .then(d => {
      if (d.error) {
        if (path !== '/') {
          browseTo('/');
          return;
        }
        list.innerHTML = '<div style="padding:20px;color:var(--red)">' + escHtml(d.error) + '</div>';
        return;
      }
      browseCurrentPath = d.path;

      // Breadcrumb
      var bc = document.getElementById('browse-breadcrumb');
      var parts = d.path.split('/').filter(Boolean);
      var html = '<span data-browse="/"' +
                 (parts.length === 0 ? ' class="current"' : '') + '>/</span>';
      for (var i = 0; i < parts.length; i++) {
        var sub = '/' + parts.slice(0, i + 1).join('/');
        if (i === parts.length - 1) {
          html += ' <span class="current">' + escHtml(parts[i]) + '</span>';
        } else {
          html += ' <span data-browse="' + escHtml(sub) + '">' + escHtml(parts[i]) + '</span> /';
        }
      }
      bc.innerHTML = html;

      // List
      if (d.entries.length === 0) {
        list.innerHTML = '<div style="padding:20px;color:var(--text3)">No subdirectories</div>';
      } else {
        list.innerHTML = d.entries.map(function(e) {
          return '<div class="browse-item" data-browse="' + escHtml(e.path) + '">' +
                 '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="var(--text3)" ' +
                 'stroke-width="2"><path d="M22 19a2 2 0 01-2 2H4a2 2 0 01-2-2V5a2 2 0 012-2h5l2 3h9a2 2 0 012 2z"/></svg>' +
                 escHtml(e.name) + '</div>';
        }).join('');
      }
    })
    .catch(function() {
      list.innerHTML = '<div style="padding:20px;color:var(--red)">Failed to load directory</div>';
    });
}

// ── Scan ──

function startScan() {
  const dir = document.getElementById('dir-input').value.trim();
  if (!dir) {
    alert('Please enter a directory path.');
    return;
  }

  Object.keys(folderData).forEach(k => delete folderData[k]);
  Object.keys(recoveredFiles).forEach(k => delete recoveredFiles[k]);
  document.getElementById('folder-list').innerHTML = '';
  document.getElementById('s-total').textContent       = '0';
  document.getElementById('s-no-moov').textContent     = '0';
  document.getElementById('s-corrupt').textContent     = '0';
  document.getElementById('s-truncated').textContent   = '0';
  document.getElementById('s-empty').textContent       = '0';
  document.getElementById('s-scanned').textContent     = '0';
  hideScanResults();

  setScanControls(true);
  document.getElementById('progress-section').style.display = 'block';
  document.getElementById('p-bar').style.width   = '0%';
  document.getElementById('p-scanned').textContent = '0';
  document.getElementById('p-total').textContent   = '?';
  document.getElementById('p-issues').textContent  = '0';
  document.getElementById('p-pct').textContent      = '';
  document.getElementById('p-current').textContent  = dir;
  setCountingPhase(true);

  try { localStorage.setItem('lastVideoScanDir', dir); } catch(e) {}

  if (eventSource) eventSource.close();
  eventSource = new EventSource('/api/scan?dir=' + encodeURIComponent(dir));

  eventSource.onmessage = e => {
    const ev = JSON.parse(e.data);
    handleEvent(ev);
  };

  eventSource.onerror = () => {
    eventSource.close();
    eventSource = null;
    scanDone(
      parseInt(document.getElementById('p-scanned').textContent) || 0,
      parseInt(document.getElementById('p-issues').textContent) || 0
    );
  };
}

function setCountingPhase(counting) {
  document.getElementById('p-track').classList.toggle('indeterminate', counting);
  document.getElementById('p-counting').style.display = counting ? 'block' : 'none';
  document.getElementById('p-stats').style.display    = counting ? 'none' : 'flex';
  if (counting) {
    document.getElementById('p-found').textContent = '0';
    document.getElementById('p-dirs').textContent  = '0';
  }
}

function handleEvent(ev) {
  switch (ev.type) {
    case 'counting':
      document.getElementById('p-found').textContent   = ev.found.toLocaleString();
      document.getElementById('p-dirs').textContent    = ev.dirs.toLocaleString();
      document.getElementById('p-current').textContent = ev.current;
      break;
    case 'total':
      setCountingPhase(false);
      document.getElementById('p-total').textContent = ev.total;
      document.getElementById('p-current').textContent = 'Checking files…';
      break;
    case 'scanning':
      document.getElementById('p-scanned').textContent = ev.scanned;
      document.getElementById('p-issues').textContent  = ev.issues;
      var fileText = ev.file || '';
      if (ev.duration != null) {
        var dm = Math.floor(ev.duration / 60);
        var ds = Math.floor(ev.duration % 60);
        fileText += ' \u00b7 ' + dm + ':' + (ds < 10 ? '0' : '') + ds;
      }
      document.getElementById('p-current').textContent = fileText;
      if (ev.total > 0) {
        var pct = Math.round(ev.scanned / ev.total * 100);
        document.getElementById('p-pct').textContent = pct + '%';
        document.getElementById('p-bar').style.width = pct + '%';
      }
      break;
    case 'issue':
      addIssue(ev);
      document.getElementById('p-issues').textContent =
        (parseInt(document.getElementById('p-issues').textContent) || 0);
      break;
    case 'complete':
      scanDone(ev.scanned, ev.issues);
      break;
    case 'cancelled':
      scanDone(ev.scanned, ev.issues, true);
      break;
    case 'error':
      alert(ev.message);
      scanDone(0, 0);
      break;
  }
}

function addIssue(ev) {
  var dir = ev.dir;
  if (!folderData[dir]) {
    folderData[dir] = { issues: [], total: 0 };
  }
  folderData[dir].issues.push(ev);
  folderData[dir].total++;

  document.getElementById('results-section').style.display = 'block';
  upsertFolderGroup(dir);
  appendIssueRow(dir, ev);
}

function upsertFolderGroup(dir) {
  var id = 'fg-' + CSS.escape(dir);
  var existing = document.getElementById(id);
  if (existing) {
    var fc = existing.querySelector('.fc');
    if (fc) fc.textContent = folderData[dir].total + ' issues';
    return;
  }
  var html = '<div class="folder-group open" id="' + id + '">' +
    '<div class="folder-header" onclick="this.parentElement.classList.toggle(\'open\')">' +
    '<svg class="folder-chevron" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
    'stroke-width="2"><polyline points="9 18 15 12 9 6"/></svg>' +
    '<span class="folder-path">' + escHtml(dir) + '</span>' +
    '<span class="folder-counts"><span class="fc">' + folderData[dir].total + ' issues</span></span>' +
    '</div>' +
    '<div class="folder-body"></div>' +
    '</div>';
  document.getElementById('folder-list').insertAdjacentHTML('beforeend', html);
}

// Rotation picked per file before recovering ('' or absent = Auto: a
// matching donor's rotation). Degrees clockwise.
var rotationChoice = {};
var ROTATION_OPTIONS = [[0, 'No rotation'], [90, '\u21bb 90\u00b0'],
                        [180, '180\u00b0'], [270, '\u21ba 90\u00b0']];

function rotationSelectHtml(attr, value, title, withAuto) {
  var opts = (withAuto ? [['', 'Auto']] : []).concat(ROTATION_OPTIONS);
  return '<select class="rotation-select" ' + attr + ' title="' + escHtml(title) + '">' +
    opts.map(function(o) {
      return '<option value="' + o[0] + '"' + (String(o[0]) === String(value) ? ' selected' : '') +
        '>' + o[1] + '</option>';
    }).join('') + '</select>';
}

function buildIssueRowHtml(ev) {
  var rec = recoveredFiles[ev.filepath];
  var dotClass = rec ? 'dot-recovered' : 'dot-' + ev.status;
  var badgeClass = rec ? 'badge-recovered' : 'badge-' + ev.status;
  var badgeText = rec ? 'RECOVERED' : statusLabel(ev.status);

  var actionHtml;
  if (rec) {
    var parts = [];
    if (rec.duration != null) {
      var mins = Math.floor(rec.duration / 60);
      var secs = Math.floor(rec.duration % 60);
      parts.push('<div class="rr-item"><span class="rr-label">Duration</span><span class="rr-value">' +
        mins + ':' + (secs < 10 ? '0' : '') + secs + '</span></div>');
    }
    parts.push('<div class="rr-item"><span class="rr-label">Size</span><span class="rr-value">' +
      formatSize(rec.size) + (rec.original_size ? ' / ' + formatSize(rec.original_size) : '') +
      '</span></div>');
    if (rec.recovery_pct != null) {
      parts.push('<div class="rr-item"><span class="rr-label">Recovery</span><span class="rr-value">' +
        rec.recovery_pct + '%</span></div>');
    }
    parts.push('<div class="rr-item"><span class="rr-label">Method</span><span class="rr-value rr-method">' +
      escHtml(rec.method || '') + '</span></div>');
    if (rec.donor_used) {
      var donorName = rec.donor_used.split('/').pop();
      parts.push('<div class="rr-item"><span class="rr-label">Donor</span><span class="rr-value rr-method">' +
        escHtml(donorName) + '</span></div>');
    }
    if (rec.video_only) {
      parts.push('<div class="rr-item"><span class="rr-label">Audio</span><span class="rr-value rr-warn">No Audio</span></div>');
    }
    actionHtml = '<div class="recovery-result">' + parts.join('') + '</div>';
    if (rec.output) {
      actionHtml += rotationSelectHtml('data-rotate-output="' + escHtml(rec.output) + '"' +
        ' data-rotate-for="' + escHtml(ev.filepath) + '"', rec.rotation || 0,
        'Rotation of the recovered video (applies right away)', false);
    }
  } else {
    var canRecover = ((ev.status === 'no_moov' || ev.status === 'corrupt') &&
      !(ev.details && ev.details.unrecoverable)) ||
      !!(ev.details && ev.details.salvageable);
    var recoverBtn = canRecover
      ? '<button class="recover-btn" data-recover="' + escHtml(ev.filepath) + '"'
        + (!ffmpegAvailable && ev.status !== 'no_moov' ? ' disabled title="Requires ffmpeg"' : '')
        + '>Recover</button>'
      : '';
    var intactCopy = ev.details && ev.details.intact_copy;
    var copyBtn = intactCopy
      ? '<button class="copy-path-btn" data-reveal="' + escHtml(intactCopy) + '">Show Intact Copy</button>'
      : '';
    var rotation = canRecover
      ? rotationSelectHtml('data-rotation-for="' + escHtml(ev.filepath) + '"',
          rotationChoice[ev.filepath] === undefined ? '' : rotationChoice[ev.filepath],
          'Rotation for the recovered video (Auto: from a matching donor)', true)
      : '';
    actionHtml = '<button class="copy-path-btn" data-copy="' + escHtml(ev.filepath) + '">Copy Path</button>' +
      copyBtn + rotation + recoverBtn;
  }

  return '<div class="issue-row">' +
    '<span class="status-dot ' + dotClass + '"></span>' +
    '<div class="issue-info">' +
    '<a class="issue-filename" data-reveal="' + escHtml(ev.filepath) + '">' + escHtml(ev.filename) + '</a>' +
    '<div class="issue-reason">' + escHtml(ev.reason || '') + '</div>' +
    '</div>' +
    actionHtml +
    '<span class="issue-meta">' + (ev.duration != null ? Math.floor(ev.duration / 60) + ':' + (Math.floor(ev.duration % 60) < 10 ? '0' : '') + Math.floor(ev.duration % 60) + ' \u00b7 ' : '') + formatSize(ev.file_size) + '</span>' +
    '<span class="status-badge ' + badgeClass + '">' + badgeText + '</span>' +
    '</div>';
}

function appendIssueRow(dir, ev) {
  var id = 'fg-' + CSS.escape(dir);
  var group = document.getElementById(id);
  if (!group) return;
  var body = group.querySelector('.folder-body');
  body.insertAdjacentHTML('beforeend', buildIssueRowHtml(ev));
}

function scanDone(scanned, issues, wasCancelled) {
  if (eventSource) { eventSource.close(); eventSource = null; }
  clearTimeout(cancelFallbackTimer);
  setScanControls(false);
  setCountingPhase(false);
  document.getElementById('progress-section').style.display = 'none';

  if (wasCancelled) {
    var total = document.getElementById('p-total').textContent;
    var notice = document.getElementById('scan-notice');
    notice.style.display = 'block';
    if (total === '?') {
      // Nothing was checked yet, so there are no results to summarize
      notice.textContent = 'Scan cancelled while finding video files.';
      return;
    }
    notice.textContent = 'Scan cancelled after ' + scanned + ' of ' + total + ' files. Results are partial.';
  }

  updateSummary(issues, scanned);

  // Before the messages below, since it resets the empty-results text
  applyFilter();

  if (issues > 0) {
    document.getElementById('export-bar').style.display = 'flex';
    document.getElementById('results-section').style.display = 'block';
    saveScan();
  } else {
    document.getElementById('empty-results').style.display = 'block';
    document.getElementById('empty-results').textContent = wasCancelled
      ? 'No issues found in the ' + scanned + ' files checked before cancelling.'
      : 'All ' + scanned + ' video files are intact.';
  }
}

function updateSummary(issues, scanned) {
  var counts = { no_moov: 0, corrupt: 0, truncated: 0, empty: 0, error: 0 };
  Object.values(folderData).forEach(function(fd) {
    fd.issues.forEach(function(i) {
      if (counts[i.status] !== undefined) counts[i.status]++;
    });
  });

  document.getElementById('s-total').textContent     = issues;
  document.getElementById('s-no-moov').textContent   = counts.no_moov;
  document.getElementById('s-corrupt').textContent   = counts.corrupt;
  document.getElementById('s-truncated').textContent = counts.truncated;
  document.getElementById('s-empty').textContent     = counts.empty;
  document.getElementById('s-scanned').textContent   = scanned;
  document.getElementById('summary-bar').style.display = 'flex';
}

function hideScanResults() {
  document.getElementById('summary-bar').style.display     = 'none';
  document.getElementById('export-bar').style.display      = 'none';
  document.getElementById('results-section').style.display = 'none';
  document.getElementById('empty-results').style.display   = 'none';
  document.getElementById('scan-notice').style.display     = 'none';
}

// While scanning, only Cancel is usable
function setScanControls(scanning) {
  document.getElementById('dir-input').disabled  = scanning;
  document.getElementById('browse-btn').disabled = scanning;
  document.getElementById('scan-btn').disabled   = scanning;
  document.getElementById('prev-scans-select').disabled = scanning;
  document.getElementById('clear-scans-btn').disabled   = scanning;
  var cancelBtn = document.getElementById('cancel-btn');
  cancelBtn.disabled = !scanning;
  cancelBtn.textContent = 'Cancel';
  document.body.classList.toggle('scanning', scanning);
}

var cancelFallbackTimer = null;

function cancelScan() {
  var cancelBtn = document.getElementById('cancel-btn');
  cancelBtn.disabled = true;
  cancelBtn.textContent = 'Cancelling…';
  fetch('/api/cancel', { method: 'POST' }).catch(function() {});
  // The server normally confirms within a moment; don't leave the UI stuck if it doesn't
  cancelFallbackTimer = setTimeout(function() {
    if (eventSource) {
      scanDone(parseInt(document.getElementById('p-scanned').textContent) || 0,
               parseInt(document.getElementById('p-issues').textContent) || 0, true);
    }
  }, 5000);
}

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

// ── Filtering ──

function setFilter(btn, filter) {
  currentFilter = filter;
  document.querySelectorAll('.filter-pill').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  applyFilter();
}

function matchesFilter(status) {
  if (currentFilter === 'all') return true;
  return status === currentFilter;
}

function applyFilter() {
  var totalVisible = 0;
  Object.keys(folderData).forEach(function(dir) {
    var id = 'fg-' + CSS.escape(dir);
    var group = document.getElementById(id);
    if (!group) return;

    var body = group.querySelector('.folder-body');
    body.innerHTML = '';

    var visible = folderData[dir].issues.filter(i => matchesFilter(i.status));
    visible.forEach(function(ev) {
      body.insertAdjacentHTML('beforeend', buildIssueRowHtml(ev));
    });

    var fc = group.querySelector('.fc');
    if (fc) fc.textContent = visible.length + ' issues';
    group.style.display = visible.length > 0 ? '' : 'none';
    totalVisible += visible.length;
  });

  document.getElementById('empty-results').style.display =
    totalVisible === 0 ? 'block' : 'none';
  document.getElementById('empty-results').textContent =
    'No issues found matching this filter.';
}

// ── Click handlers ──

// Cancel a queued recovery and put its row's button back to Recover
function cancelQueuedJob(jobId) {
  fetch('/api/recover-cancel', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_id: jobId }),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (!d.ok) {
      alert(d.error || 'Could not cancel this recovery.');
    } else {
      var btn = document.querySelector('[data-recover="' + CSS.escape(d.filepath) + '"]');
      if (btn && btn.textContent === 'Queued') {
        btn.textContent = 'Recover';
        btn.disabled = false;
      }
    }
    pollQueueStatus();
  })
  .catch(function(err) { console.error('Cancelling recovery failed:', err); });
}

// Rotation pickers: before recovery the choice rides along with the job;
// after recovery it rewrites the recovered file's rotation in place
document.addEventListener('change', function(e) {
  var sel = e.target;
  if (!sel.classList || !sel.classList.contains('rotation-select')) return;
  if (sel.hasAttribute('data-rotation-for')) {
    var path = sel.getAttribute('data-rotation-for');
    if (sel.value === '') delete rotationChoice[path];
    else rotationChoice[path] = Number(sel.value);
    return;
  }
  var output = sel.getAttribute('data-rotate-output');
  var source = sel.getAttribute('data-rotate-for');
  var previous = String((recoveredFiles[source] || {}).rotation || 0);
  sel.disabled = true;
  fetch('/api/rotate-output', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ output: output, rotation: Number(sel.value) }),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    sel.disabled = false;
    if (d.ok) {
      if (recoveredFiles[source]) recoveredFiles[source].rotation = d.rotation;
    } else {
      sel.value = previous;
      alert(d.error || 'Could not change the rotation.');
    }
  })
  .catch(function(err) {
    sel.disabled = false;
    sel.value = previous;
    console.error('Changing rotation failed:', err);
  });
});

document.addEventListener('click', function(e) {
  var cancelTarget = e.target.closest('[data-cancel-job]');
  if (cancelTarget) {
    e.preventDefault();
    e.stopPropagation();
    cancelQueuedJob(cancelTarget.getAttribute('data-cancel-job'));
    return;
  }
  var browseTarget = e.target.closest('[data-browse]');
  if (browseTarget) {
    e.preventDefault();
    browseTo(browseTarget.getAttribute('data-browse'));
    return;
  }

  var copyTarget = e.target.closest('[data-copy]');
  if (copyTarget) {
    e.preventDefault();
    var path = copyTarget.getAttribute('data-copy');
    navigator.clipboard.writeText(path).then(function() {
      copyTarget.textContent = 'Copied!';
      setTimeout(function() { copyTarget.textContent = 'Copy Path'; }, 1200);
    });
    return;
  }

  var revealTarget = e.target.closest('[data-reveal]');
  if (revealTarget) {
    e.preventDefault();
    e.stopPropagation();
    fetch('/api/reveal', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filepath: revealTarget.getAttribute('data-reveal') }),
    });
    return;
  }

  var recoverTarget = e.target.closest('[data-recover]');
  if (recoverTarget) {
    e.preventDefault();
    e.stopPropagation();
    recoverVideo(recoverTarget);
    return;
  }

  var donorFolderTarget = e.target.closest('[data-donor-folder]');
  if (donorFolderTarget) {
    e.preventDefault();
    e.stopPropagation();
    openDonorBrowse(donorFolderTarget.getAttribute('data-donor-folder'));
    return;
  }
  var donorTarget = e.target.closest('[data-set-donor]');
  if (donorTarget) {
    e.preventDefault();
    e.stopPropagation();
    setDonor(donorTarget);
    return;
  }
});

function recoverVideo(btn) {
  if (document.body.classList.contains('scanning')) return;
  var filepath = btn.getAttribute('data-recover');
  var dir = filepath.substring(0, filepath.lastIndexOf('/'));
  var donor = donorMap[dir] || '';

  btn.textContent = 'Queuing…';
  btn.disabled = true;
  btn.closest('.issue-row').querySelectorAll('.recover-error, .recover-hints')
    .forEach(function(el) { el.remove(); });

  var body = { filepath: filepath };
  if (donor) body.donor = donor;
  if (outputDir) body.output_dir = outputDir;
  if (rotationChoice[filepath] !== undefined) body.rotation = rotationChoice[filepath];

  fetch('/api/recover-queue', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
  .then(function(r) { return r.json(); })
  .then(function(d) {
    if (d.ok) {
      btn.textContent = 'Queued';
      showQueuePanel();
      startQueuePolling();
    } else if (d.error_kind === 'output_dir') {
      // Too long for the button, and it applies to every file
      alert(d.error + '\n\nChoose another folder with Browse next to "Save recovered to".');
      btn.textContent = 'Recover';
      btn.disabled = false;
    } else {
      btn.textContent = d.error || 'Failed';
      btn.style.color = 'var(--red)';
      btn.style.borderColor = 'rgba(224,82,82,.35)';
      setTimeout(function() {
        btn.textContent = 'Recover';
        btn.style.color = '';
        btn.style.borderColor = '';
        btn.disabled = false;
      }, 3000);
    }
  })
  .catch(function() {
    btn.textContent = 'Error';
    btn.style.color = 'var(--red)';
    setTimeout(function() {
      btn.textContent = 'Recover';
      btn.style.color = '';
      btn.style.borderColor = '';
      btn.disabled = false;
    }, 2000);
  });
}

// ── Recovery Queue ──

var queuePollTimer = null;

function showQueuePanel() {
  document.getElementById('queue-panel').style.display = 'flex';
}

function startQueuePolling() {
  // Refresh right away even if polling is already running, so a newly
  // queued job shows up immediately rather than on the next tick
  pollQueueStatus();
  if (!queuePollTimer) queuePollTimer = setInterval(pollQueueStatus, 2000);
}

function stopQueuePolling() {
  if (queuePollTimer) {
    clearInterval(queuePollTimer);
    queuePollTimer = null;
  }
}

function pollQueueStatus() {
  fetch('/api/recover-status')
    .then(function(r) { return r.json(); })
    .then(function(d) {
      var jobs = d.jobs || [];
      updateQueuePanel(jobs);
      try {
        updateInlineRows(jobs);
      } catch (err) {
        console.error('Updating result rows failed:', err);
      }
      var active = jobs.some(function(j) {
        return j.status === 'queued' || j.status === 'recovering';
      });
      if (!active) stopQueuePolling();
    })
    .catch(function(err) { console.error('Recovery status poll failed:', err); });
}

function updateQueuePanel(jobs) {
  var panel = document.getElementById('queue-panel');
  var body = document.getElementById('queue-body');
  if (!jobs.length) {
    panel.style.display = 'none';
    return;
  }
  panel.style.display = 'flex';

  var counts = { queued: 0, recovering: 0, done: 0, failed: 0 };
  jobs.forEach(function(j) { counts[j.status]++; });
  var parts = [];
  if (counts.recovering) parts.push(counts.recovering + ' recovering');
  if (counts.queued) parts.push(counts.queued + ' queued');
  if (counts.done) parts.push(counts.done + ' done');
  if (counts.failed) parts.push(counts.failed + ' failed');
  document.getElementById('queue-counts').textContent = parts.join(' · ');

  var clearBtn = document.querySelector('.queue-clear-btn');
  var finished = counts.done + counts.failed;
  clearBtn.disabled = finished === 0;
  clearBtn.title = finished ? 'Remove finished and failed jobs from this list'
                            : 'Nothing has finished yet';

  body.innerHTML = jobs.map(function(job) {
    var statusClass;
    switch (job.status) {
      case 'queued': statusClass = 'qs-queued'; break;
      case 'recovering': statusClass = 'qs-recovering'; break;
      case 'done': statusClass = 'qs-done'; break;
      case 'failed': statusClass = 'qs-failed'; break;
    }
    var icon = job.status === 'done' ? '✓' :
               job.status === 'failed' ? '✗' :
               job.status === 'recovering' ? '◎' : '○';

    var detail = '';
    if (job.status === 'done' && job.result) {
      detail = formatSize(job.result.size);
      if (job.result.method) detail += ' · ' + escHtml(job.result.method);
    } else if (job.status === 'failed' && job.error) {
      detail = escHtml(job.error);
    } else if (job.status === 'recovering' && job.started_at) {
      var elapsed = Math.floor(Date.now() / 1000 - job.started_at);
      detail = elapsed + 's elapsed';
    }

    var cancel = job.status === 'queued'
      ? '<button class="queue-cancel-btn" data-cancel-job="' + escHtml(job.id) +
        '" title="Cancel this recovery">Cancel</button>'
      : '';
    return '<div class="queue-item ' + statusClass + '">' +
      '<span class="queue-status-icon">' + icon + '</span>' +
      (job.status === 'done' && job.result && job.result.output
        ? '<a class="queue-filename" data-reveal="' + escHtml(job.result.output) +
          '" title="Show ' + escHtml(job.result.output) + ' in Finder">' + escHtml(job.filename) + '</a>'
        : '<span class="queue-filename">' + escHtml(job.filename) + '</span>') +
      '<span class="queue-detail">' + detail + '</span>' +
      '<span class="queue-status-label">' + job.status.toUpperCase() + '</span>' +
      cancel +
      '</div>';
  }).join('');
}

// Failed job ids already reflected in the UI. Failed jobs stay in the status
// list, so without this an old failure would reset a later retry's button.
var handledFailures = {};

function showRecoverFailure(job) {
  var btn = document.querySelector('[data-recover="' + CSS.escape(job.filepath) + '"]');
  if (!btn) return;
  btn.textContent = 'Retry Recovery';
  btn.disabled = false;
  // Most failures come down to a missing or mismatched donor
  if (!document.querySelector('[data-donor-folder="' + CSS.escape(job.filepath) + '"]')) {
    btn.insertAdjacentHTML('beforebegin', '<button class="copy-path-btn" data-donor-folder="' +
      escHtml(job.filepath) + '">Choose Donor Folder</button>');
  }
  var info = btn.closest('.issue-row').querySelector('.issue-info');
  var msg = 'Recovery failed: ' + (job.error || 'unknown error');
  info.insertAdjacentHTML('beforeend',
    '<div class="issue-reason recover-error" title="' + escHtml(msg) + '">' + escHtml(msg) + '</div>');
  var clues = donorClues(job.hints);
  if (clues) {
    info.insertAdjacentHTML('beforeend',
      '<div class="issue-reason recover-hints" title="' + escHtml(clues) + '">' + escHtml(clues) + '</div>');
  }
}

// Clues to the source camera, so the right donor can be found for files with
// generic names. Text found loose in the data may come from another file.
function donorClues(hints) {
  if (!hints) return '';
  var strong = (hints.camera || []).filter(function(c) { return c[1]; }).map(function(c) { return c[0]; });
  var weak = (hints.camera || []).filter(function(c) { return !c[1]; }).map(function(c) { return c[0]; });
  var parts = [];
  if (strong.length) parts.push(strong.join(', '));
  if (hints.resolution) parts.push(hints.resolution.replace('x', '×'));
  var unverified = [];
  if (weak.length) unverified.push('mentions ' + weak.join(', '));
  if (hints.dates && hints.dates.length) unverified.push('dates ' + hints.dates.join(', '));
  if (unverified.length) {
    parts.push('data also ' + unverified.join('; ') + ' (unverified, may belong to another file)');
  }
  return parts.length ? 'Donor clues: ' + parts.join(' · ')
                      : 'Donor clues: none found in this file';
}

function updateInlineRows(jobs) {
  jobs.forEach(function(job) {
    if (job.status === 'failed' && !handledFailures[job.id]) {
      handledFailures[job.id] = true;
      showRecoverFailure(job);
    }
    if (job.status === 'done' && job.result && !recoveredFiles[job.filepath]) {
      recoveredFiles[job.filepath] = {
        size: job.result.size,
        method: job.result.method,
        duration: job.result.duration,
        recovery_pct: job.result.recovery_pct,
        original_size: job.result.original_size,
        donor_used: job.result.donor_used,
        output: job.result.output,
        video_only: job.result.video_only,
        rotation: job.result.rotation
      };
      var ev = null;
      Object.values(folderData).forEach(function(fd) {
        fd.issues.forEach(function(i) {
          if (i.filepath === job.filepath) ev = i;
        });
      });
      // Replace only this file's row, found by its filename link. Matching
      // any row with a "Queued" button overwrote every other queued file's
      // row with this file when several were queued at once.
      var link = ev && document.querySelector(
        '.issue-filename[data-reveal="' + CSS.escape(job.filepath) + '"]');
      if (link) {
        link.closest('.issue-row').outerHTML = buildIssueRowHtml(ev);
      }
    }
  });
}

function clearCompletedJobs() {
  // Remove finished rows right away; the poll below syncs with the server
  document.querySelectorAll('#queue-body .qs-done, #queue-body .qs-failed')
    .forEach(function(el) { el.remove(); });
  if (!document.querySelector('#queue-body .queue-item')) {
    document.getElementById('queue-panel').style.display = 'none';
  }
  fetch('/api/recover-queue-clear', { method: 'POST' })
    .catch(function(err) { console.error('Clearing finished jobs failed:', err); })
    .then(pollQueueStatus);
}

function setDonor(btn) {
  var filepath = btn.getAttribute('data-set-donor');
  var dir = btn.getAttribute('data-donor-dir');

  fetch('/api/set-donor', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ filepath: filepath, directory: dir }),
  })
  .then(r => r.json())
  .then(d => {
    if (d.ok) {
      donorMap[dir] = filepath;
      btn.textContent = 'Donor Set!';
      btn.style.color = 'var(--green)';
      setTimeout(function() {
        btn.textContent = 'Set as Donor';
        btn.style.color = '';
      }, 1500);
    }
  });
}

// ── Export ──

function exportData(fmt) {
  window.location.href = '/api/export?format=' + fmt;
}

// ── Helpers ──

function formatSize(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  if (bytes < 1024) return bytes + ' B';
  if (bytes < 1048576) return (bytes / 1024).toFixed(1) + ' KB';
  if (bytes < 1073741824) return (bytes / 1048576).toFixed(1) + ' MB';
  return (bytes / 1073741824).toFixed(2) + ' GB';
}

function statusLabel(s) {
  var labels = {
    no_moov: 'NO MOOV', corrupt: 'CORRUPT', truncated: 'TRUNCATED',
    empty: 'EMPTY', error: 'ERROR'
  };
  return labels[s] || s.toUpperCase();
}

function escHtml(s) {
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

// ── Mount detection ──

var mountSource = null;

function watchMount(path) {
  if (mountSource) { mountSource.close(); mountSource = null; }
  var el = document.getElementById('mount-status');
  if (!path) { el.style.display = 'none'; return; }

  mountSource = new EventSource('/api/mount-status?path=' + encodeURIComponent(path));

  mountSource.onmessage = function(e) {
    var data = JSON.parse(e.data);
    if (data.exists) {
      el.className = 'mount-status mounted';
      el.innerHTML = '<span class="dot"></span>Directory available — ready to scan';
      el.style.display = 'flex';
      setTimeout(function() {
        if (el.className.includes('mounted')) el.style.display = 'none';
      }, 5000);
    } else {
      el.className = 'mount-status unmounted';
      el.innerHTML = '<span class="dot"></span>Volume not mounted — waiting for drive…';
      el.style.display = 'flex';
    }
  };

  mountSource.onerror = function() {
    mountSource.close();
    mountSource = null;
    el.style.display = 'none';
  };
}

document.getElementById('dir-input').addEventListener('change', function() {
  watchMount(this.value.trim());
});

checkFfmpeg();
// Saved scans are listed but not loaded; the user picks one from the dropdown
loadSavedScans().then(scans => populateScanDropdown(scans, null));
try {
  var savedDir = localStorage.getItem('lastVideoScanDir');
  if (savedDir) {
    document.getElementById('dir-input').value = savedDir;
    watchMount(savedDir);
  }
} catch(e) {}
