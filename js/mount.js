// Mount detection for the scan folder.

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
