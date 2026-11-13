// Result rows: markup for each scanned file, and the rotation picker.

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

// Copy-to-clipboard icon (same glyph as the folder headers)
var COPY_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2" stroke-linecap="round" aria-hidden="true"><rect x="9" y="9" width="13" height="13" rx="2"/>' +
  '<path d="M5 15H4a2 2 0 01-2-2V4a2 2 0 012-2h9a2 2 0 012 2v1"/></svg>';

// What Frayme found about each file: a facts line on the row, and a button
// that expands the bulleted notes behind it (details.findings for damaged
// files, the recovery's facts and notes once recovered)
var openDetails = {};    // filepath -> true while its notes are expanded
var contentNotes = {};   // filepath -> notes from a full look at the file, or 'loading'

var INFO_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2.2" stroke-linecap="round" aria-hidden="true"><circle cx="12" cy="12" r="10"/>' +
  '<line x1="12" y1="11" x2="12" y2="17"/><circle cx="12" cy="7.5" r=".6" fill="currentColor"/></svg>';
var WARN_ICON = '<svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<path d="M10.3 3.9L1.8 18a2 2 0 001.7 3h17a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/>' +
  '<line x1="12" y1="9" x2="12" y2="13"/><circle cx="12" cy="17" r=".6" fill="currentColor"/></svg>';
var CHECK_ICON = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
  'stroke-width="2.4" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
  '<polyline points="20 6 9 17 4 12"/></svg>';

function findIssue(filepath) {
  var found = null;
  Object.values(folderData).forEach(function(fd) {
    fd.issues.forEach(function(i) { if (i.filepath === filepath) found = i; });
  });
  return found;
}

function rowFindings(ev) {
  var rec = recoveredFiles[ev.filepath];
  if (rec) return { facts: rec.facts || [], notes: rec.notes || [] };
  var f = ev.details && ev.details.findings;
  return f ? { facts: f.facts || [], notes: f.notes || [] } : { facts: [], notes: [] };
}

// A full look at the media data takes seconds, so it runs when the notes
// are first opened, for damaged MP4/MOV files not yet recovered
function wantsContentNotes(ev) {
  var d = ev.details || {};
  return !recoveredFiles[ev.filepath] && (d.mdat_analysis || d.zero_bytes) &&
    !d.unrecoverable && ev.status !== 'empty';
}

function notesListHtml(ev) {
  var notes = rowFindings(ev).notes.slice();
  var extra = contentNotes[ev.filepath];
  if (wantsContentNotes(ev)) {
    if (extra === 'loading') notes.push({ level: 'pending', text: 'Looking through the whole file\u2026' });
    else if (Array.isArray(extra)) notes = notes.concat(extra);
  }
  return notes.map(function(n) {
    return '<li class="note-' + escHtml(n.level) + '">' + escHtml(n.text) + '</li>';
  }).join('');
}

function factsHtml(ev) {
  var f = rowFindings(ev);
  if (!f.facts.length && !f.notes.length) return '';
  var warns = f.notes.filter(function(n) { return n.level === 'warn'; }).length;
  var open = !!openDetails[ev.filepath];
  // Most damaged files share the same warnings (no orientation, frame rate
  // or sound), so the label stays "Details". No count in the tooltip: the
  // whole-file look adds notes (sometimes warnings) only once it's opened
  var title = warns
    ? 'What Frayme found, with warnings (amber) to read before trusting this file'
    : 'What Frayme found about this file';
  var button = f.notes.length || wantsContentNotes(ev)
    ? '<button class="notes-btn' + (warns ? ' has-warn' : '') + '" data-notes-toggle="' +
      escHtml(ev.filepath) + '" aria-expanded="' + open + '" title="' + title + '">' +
      (warns ? WARN_ICON : INFO_ICON) + '<span>Details</span></button>'
    : '';
  return '<div class="issue-facts">' +
    (f.facts.length ? '<span class="facts-text">' + f.facts.map(escHtml).join(' \u00b7 ') + '</span>' : '') +
    button + '</div>' +
    '<ul class="row-notes"' + (open ? '' : ' hidden') + '>' + (open ? notesListHtml(ev) : '') + '</ul>';
}

function toggleRowNotes(btn) {
  var path = btn.getAttribute('data-notes-toggle');
  var ev = findIssue(path);
  var list = btn.closest('.issue-info').querySelector('.row-notes');
  if (!ev || !list) return;
  var open = !openDetails[path];
  if (open) openDetails[path] = true; else delete openDetails[path];
  btn.setAttribute('aria-expanded', String(open));
  list.hidden = !open;
  if (!open) return;
  if (wantsContentNotes(ev) && contentNotes[path] === undefined) {
    contentNotes[path] = 'loading';
    fetch('/api/file-details', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ filepath: path }),
    })
    .then(function(r) { return r.json(); })
    .then(function(d) { contentNotes[path] = d.ok ? d.notes : []; })
    .catch(function() { contentNotes[path] = []; })
    .then(function() {
      // The row may have been redrawn (filter, sort) while waiting
      var link = document.querySelector('.issue-filename[data-reveal="' + CSS.escape(path) + '"]');
      var current = link && link.closest('.issue-info').querySelector('.row-notes');
      if (current && openDetails[path]) current.innerHTML = notesListHtml(ev);
    });
  }
  list.innerHTML = notesListHtml(ev);
}

function durationText(seconds) {
  return Math.floor(seconds / 60) + ':' + (Math.floor(seconds % 60) < 10 ? '0' : '') +
    Math.floor(seconds % 60);
}

// One line under the reason: where the recovered file is and how it was made
function recoveredLineHtml(rec) {
  var bits = [];
  if (rec.output) {
    bits.push('Recovered as <a class="recovered-link" data-reveal="' + escHtml(rec.output) + '">' +
      escHtml(rec.output.split('/').pop()) + '</a>');
  }
  if (rec.duration != null) bits.push(durationText(rec.duration));
  if (rec.original_size) {
    bits.push(formatSize(rec.size) + ' of ' + formatSize(rec.original_size) +
      (rec.recovery_pct != null ? ' (' + rec.recovery_pct + '%)' : ''));
  } else {
    bits.push(formatSize(rec.size));
  }
  bits.push(escHtml(rec.method || ''));
  if (rec.donor_used) bits.push('donor ' + escHtml(rec.donor_used.split('/').pop()));
  var line = bits.join(' · ');
  if (rec.video_only) line += ' · <span class="rr-warn">no audio</span>';
  return '<div class="recovered-line">' + line + '</div>';
}

function buildIssueRowHtml(ev) {
  var rec = recoveredFiles[ev.filepath];
  var dotClass = rec ? 'dot-recovered' : 'dot-' + ev.status;
  var badgeClass = rec ? 'badge-recovered' : 'badge-' + ev.status;
  var badgeText = rec ? 'RECOVERED' : statusLabel(ev.status);

  var reason = '';
  var actions = '';
  if (rec) {
    reason = recoveredLineHtml(rec);
    if (rec.output) {
      actions = rotationSelectHtml('data-rotate-output="' + escHtml(rec.output) + '"' +
        ' data-rotate-for="' + escHtml(ev.filepath) + '"', rec.rotation || 0,
        'Rotation of the recovered video (applies right away)', false);
    }
  } else {
    reason = '<div class="issue-reason">' + escHtml(ev.reason || '') + '</div>';
    var canRecover = ((ev.status === 'no_moov' || ev.status === 'corrupt') &&
      !(ev.details && ev.details.unrecoverable)) ||
      !!(ev.details && ev.details.salvageable);
    var intactCopy = ev.details && ev.details.intact_copy;
    actions = (intactCopy
      ? '<button class="row-btn" data-reveal="' + escHtml(intactCopy) + '">Show Intact Copy</button>'
      : '') +
      (canRecover
        ? rotationSelectHtml('data-rotation-for="' + escHtml(ev.filepath) + '"',
            rotationChoice[ev.filepath] === undefined ? '' : rotationChoice[ev.filepath],
            'Rotation for the recovered video (Auto: from a matching donor)', true)
        : '') +
      (canRecover
        ? '<button class="recover-btn" data-recover="' + escHtml(ev.filepath) + '"'
          + (!ffmpegAvailable && ev.status !== 'no_moov' ? ' disabled title="Requires ffmpeg"' : '')
          + '>Recover</button>'
        : '');
  }
  var copyBtn = '<button class="copy-btn" data-copy="' + escHtml(ev.filepath) +
    '" title="Copy path: ' + escHtml(ev.filepath) + '" aria-label="Copy path">' + COPY_ICON + '</button>';

  var meta = (ev.duration != null ? durationText(ev.duration) + ' · ' : '') + formatSize(ev.file_size);

  return '<div class="issue-row">' +
    '<span class="status-dot ' + dotClass + '"></span>' +
    '<div class="issue-info">' +
    '<a class="issue-filename" data-reveal="' + escHtml(ev.filepath) + '">' + escHtml(ev.filename) + '</a>' +
    reason + factsHtml(ev) +
    '</div>' +
    '<div class="row-actions">' + copyBtn + actions + '</div>' +
    '<span class="issue-meta">' + meta + '</span>' +
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
