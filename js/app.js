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

function openBrowse() {
  document.getElementById('browse-overlay').classList.add('open');
  var input = document.getElementById('dir-input').value.trim();
  browseTo(input || '/Volumes');
}

function closeBrowse() {
  document.getElementById('browse-overlay').classList.remove('open');
}

function selectBrowseDir() {
  document.getElementById('dir-input').value = browseCurrentPath;
  watchMount(browseCurrentPath);
  closeBrowse();
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
  document.getElementById('summary-bar').style.display = 'none';
  document.getElementById('export-bar').style.display  = 'none';
  document.getElementById('results-section').style.display = 'none';
  document.getElementById('empty-results').style.display   = 'none';

  document.getElementById('scan-btn').disabled   = true;
  document.getElementById('cancel-btn').disabled = false;
  document.getElementById('progress-section').style.display = 'block';
  document.getElementById('p-bar').style.width   = '0%';
  document.getElementById('p-scanned').textContent = '0';
  document.getElementById('p-total').textContent   = '?';
  document.getElementById('p-issues').textContent  = '0';
  document.getElementById('p-pct').textContent      = '';
  document.getElementById('p-current').textContent  = 'Starting…';

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

function handleEvent(ev) {
  switch (ev.type) {
    case 'total':
      document.getElementById('p-total').textContent = ev.total;
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
      scanDone(ev.scanned, ev.issues);
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
  } else {
    var canRecover = (ev.status === 'no_moov' || ev.status === 'corrupt') &&
      !(ev.details && ev.details.unrecoverable);
    var recoverBtn = canRecover
      ? '<button class="recover-btn" data-recover="' + escHtml(ev.filepath) + '"'
        + (!ffmpegAvailable && ev.status !== 'no_moov' ? ' disabled title="Requires ffmpeg"' : '')
        + '>Recover Video</button>'
      : '';
    actionHtml = '<button class="copy-path-btn" data-copy="' + escHtml(ev.filepath) + '">Copy Path</button>' +
      recoverBtn;
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

function scanDone(scanned, issues) {
  if (eventSource) { eventSource.close(); eventSource = null; }
  document.getElementById('scan-btn').disabled   = false;
  document.getElementById('cancel-btn').disabled = true;
  document.getElementById('progress-section').style.display = 'none';

  // Count by status
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

  if (issues > 0) {
    document.getElementById('export-bar').style.display = 'flex';
    document.getElementById('results-section').style.display = 'block';
  } else {
    document.getElementById('empty-results').style.display = 'block';
    document.getElementById('empty-results').textContent = 'All ' + scanned + ' video files are intact.';
  }

  applyFilter();
}

function cancelScan() {
  fetch('/api/cancel', { method: 'POST' });
  document.getElementById('cancel-btn').disabled = true;
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

document.addEventListener('click', function(e) {
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

  var donorTarget = e.target.closest('[data-set-donor]');
  if (donorTarget) {
    e.preventDefault();
    e.stopPropagation();
    setDonor(donorTarget);
    return;
  }
});

function recoverVideo(btn) {
  var filepath = btn.getAttribute('data-recover');
  var dir = filepath.substring(0, filepath.lastIndexOf('/'));
  var donor = donorMap[dir] || '';

  btn.textContent = 'Queuing…';
  btn.disabled = true;

  var body = { filepath: filepath };
  if (donor) body.donor = donor;

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
    } else {
      btn.textContent = d.error || 'Failed';
      btn.style.color = 'var(--red)';
      btn.style.borderColor = 'rgba(224,82,82,.35)';
      setTimeout(function() {
        btn.textContent = 'Recover Video';
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
      btn.textContent = 'Recover Video';
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
  if (queuePollTimer) return;
  pollQueueStatus();
  queuePollTimer = setInterval(pollQueueStatus, 2000);
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
      updateInlineRows(jobs);
      var active = jobs.some(function(j) {
        return j.status === 'queued' || j.status === 'recovering';
      });
      if (!active && jobs.length > 0) {
        stopQueuePolling();
      }
    })
    .catch(function() {});
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
      detail = escHtml(job.error.length > 50 ? job.error.substring(0, 50) + '…' : job.error);
    } else if (job.status === 'recovering' && job.started_at) {
      var elapsed = Math.floor(Date.now() / 1000 - job.started_at);
      detail = elapsed + 's elapsed';
    }

    return '<div class="queue-item ' + statusClass + '">' +
      '<span class="queue-status-icon">' + icon + '</span>' +
      '<span class="queue-filename">' + escHtml(job.filename) + '</span>' +
      '<span class="queue-detail">' + detail + '</span>' +
      '<span class="queue-status-label">' + job.status.toUpperCase() + '</span>' +
      '</div>';
  }).join('');
}

function updateInlineRows(jobs) {
  jobs.forEach(function(job) {
    if (job.status === 'done' && job.result && !recoveredFiles[job.filepath]) {
      recoveredFiles[job.filepath] = {
        size: job.result.size,
        method: job.result.method,
        duration: job.result.duration,
        recovery_pct: job.result.recovery_pct,
        original_size: job.result.original_size,
        donor_used: job.result.donor_used,
        output: job.result.output,
        video_only: job.result.video_only
      };
      var ev = null;
      Object.values(folderData).forEach(function(fd) {
        fd.issues.forEach(function(i) {
          if (i.filepath === job.filepath) ev = i;
        });
      });
      if (ev) {
        var id = 'fg-' + CSS.escape(ev.dir);
        var group = document.getElementById(id);
        if (group) {
          var rows = group.querySelectorAll('.issue-row');
          rows.forEach(function(row) {
            var recBtn = row.querySelector('[data-recover="' + CSS.escape(job.filepath) + '"]');
            var queuedBtn = null;
            if (!recBtn) {
              row.querySelectorAll('button').forEach(function(b) {
                if (b.textContent === 'Queued') queuedBtn = b;
              });
            }
            if (recBtn || queuedBtn) {
              row.outerHTML = buildIssueRowHtml(ev);
            }
          });
        }
      }
    }
  });
}

function clearCompletedJobs() {
  fetch('/api/recover-queue-clear', { method: 'POST' })
    .then(function() { pollQueueStatus(); });
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
try {
  var savedDir = localStorage.getItem('lastVideoScanDir');
  if (savedDir) {
    document.getElementById('dir-input').value = savedDir;
    watchMount(savedDir);
  }
} catch(e) {}
