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
    reason +
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
