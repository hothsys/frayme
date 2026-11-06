// Shared state, ffmpeg status, and small helpers used across the page.

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

// Status Guide: a panel that slides in from the right. The results stay
// scrollable behind it, so it never has to be scrolled to.
function openStatusGuide() {
  document.getElementById('status-guide').classList.add('open');
}

function closeStatusGuide() {
  document.getElementById('status-guide').classList.remove('open');
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') closeStatusGuide();
});
