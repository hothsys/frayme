// Scanning: start/cancel, streamed events, results, summary, and filters.

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

// ── Filtering and sorting ──

// Files in each folder are sorted by name, size (largest first) or status;
// the choice is remembered
var currentSort = 'name';
try { currentSort = localStorage.getItem('resultSort') || 'name'; } catch (e) {}
var STATUS_ORDER = { no_moov: 0, corrupt: 1, truncated: 2, error: 3, empty: 4 };

function setSort(value) {
  currentSort = value;
  try { localStorage.setItem('resultSort', value); } catch (e) {}
  applyFilter();
}

function sortIssues(list) {
  var byName = function(a, b) {
    return a.filename.localeCompare(b.filename, undefined, { numeric: true, sensitivity: 'base' });
  };
  return list.slice().sort(function(a, b) {
    if (currentSort === 'size') return (b.file_size || 0) - (a.file_size || 0) || byName(a, b);
    if (currentSort === 'status') {
      var sa = STATUS_ORDER[a.status], sb = STATUS_ORDER[b.status];
      return (sa === undefined ? 9 : sa) - (sb === undefined ? 9 : sb) || byName(a, b);
    }
    return byName(a, b);
  });
}

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

    var visible = sortIssues(folderData[dir].issues.filter(i => matchesFilter(i.status)));
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

// ── Export ──

function exportData(fmt) {
  window.location.href = '/api/export?format=' + fmt;
}

// Scripts load at the end of the page, so the picker exists already
document.getElementById('sort-select').value = currentSort;
