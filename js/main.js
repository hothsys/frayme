// Event delegation for clicks and changes, and startup. Loaded last.

// ── Click handlers ──

// Cancel a queued recovery and put its row's button back to Recover
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
      // A check mark for a moment, then the copy icon again
      copyTarget.innerHTML = CHECK_ICON;
      copyTarget.classList.add('copied');
      setTimeout(function() {
        copyTarget.innerHTML = COPY_ICON;
        copyTarget.classList.remove('copied');
      }, 1200);
    });
    return;
  }

  var notesTarget = e.target.closest('[data-notes-toggle]');
  if (notesTarget) {
    e.preventDefault();
    e.stopPropagation();
    toggleRowNotes(notesTarget);
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
