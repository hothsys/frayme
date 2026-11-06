// Recovery: queueing recoveries, the Recovery Queue panel, and donors.

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
  // Non-breaking spaces inside each item and before each dot, so a wrap
  // falls between items and never leaves a dot at the start of a line
  document.getElementById('queue-counts').textContent = parts
    .map(function(p) { return p.replace(/ /g, '\u00a0'); }).join('\u00a0\u00b7 ');

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
  btn.textContent = 'Retry';
  btn.disabled = false;
  // Most failures come down to a missing or mismatched donor
  if (!document.querySelector('[data-donor-folder="' + CSS.escape(job.filepath) + '"]')) {
    btn.insertAdjacentHTML('beforebegin', '<button class="row-btn" data-donor-folder="' +
      escHtml(job.filepath) + '">Choose Donor</button>');
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
