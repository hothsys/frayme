// The folder browser: scan directory, output folder, and donor folders.

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
    if (btn) { btn.textContent = 'Choose Donor'; btn.disabled = false; }
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
    if (btn) { btn.textContent = 'Choose Donor'; btn.disabled = false; }
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
