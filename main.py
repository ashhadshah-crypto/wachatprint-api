<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>WAChatPrint - Dashboard</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: Arial, sans-serif; background: #f8fafc; color: #0f172a; min-height: 100vh; padding: 24px; }
    .wrap { max-width: 1100px; margin: 0 auto; }
    .topbar { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
    .logo { font-size: 24px; font-weight: 700; }
    .topbar-right { display: flex; gap: 10px; flex-wrap: wrap; }
    .btn { border: none; background: #2563eb; color: white; padding: 12px 18px; border-radius: 10px; cursor: pointer; font-weight: 600; text-decoration: none; display: inline-flex; align-items: center; justify-content: center; }
    .btn:hover { background: #1d4ed8; }
    .btn:disabled { background: #94a3b8; cursor: not-allowed; }
    .btn-secondary { background: #e2e8f0; color: #0f172a; }
    .btn-secondary:hover { background: #cbd5e1; }
    .btn-green { background: #0f8f7d; }
    .btn-green:hover { background: #0b7466; }
    .grid { display: grid; grid-template-columns: 1.1fr 0.9fr; gap: 20px; margin-bottom: 20px; }
    .card { background: white; border-radius: 18px; padding: 24px; box-shadow: 0 10px 30px rgba(0,0,0,0.06); margin-bottom: 20px; }
    h1 { font-size: 30px; margin-bottom: 10px; }
    h2 { font-size: 22px; margin-bottom: 12px; }
    p { color: #475569; line-height: 1.6; }
    .status { margin-top: 10px; font-size: 15px; color: #15803d; font-weight: 600; }
    .plan-grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 14px; margin-top: 16px; }
    .plan-box { background: #f8fafc; border: 1px solid #e2e8f0; border-radius: 14px; padding: 16px; }
    .plan-label { color: #64748b; font-size: 13px; margin-bottom: 8px; }
    .plan-value { font-size: 24px; font-weight: 700; color: #0f172a; }
    .plan-sub { font-size: 13px; color: #64748b; margin-top: 6px; line-height: 1.5; }
    .plan-actions { display: flex; gap: 12px; flex-wrap: wrap; margin-top: 18px; }
    .pill { display: inline-block; padding: 8px 12px; border-radius: 999px; font-size: 12px; font-weight: 700; margin-top: 12px; }
    .pill-free { background: #e2e8f0; color: #334155; }
    .pill-pro { background: #dcfce7; color: #166534; }
    .upload-box { margin-top: 20px; padding: 24px; border: 2px dashed #cbd5e1; border-radius: 16px; background: #f8fafc; }
    .upload-box input[type="file"] { display: block; margin-top: 14px; margin-bottom: 16px; width: 100%; }
    .note { font-size: 14px; color: #64748b; margin-top: 8px; line-height: 1.6; }
    .file-info { margin-top: 16px; padding: 16px; border-radius: 12px; background: #eff6ff; color: #1e3a8a; font-size: 14px; line-height: 1.7; display: none; }
    .message { margin-top: 16px; font-size: 14px; font-weight: 600; line-height: 1.6; }
    .success { color: #15803d; }
    .error { color: #dc2626; }
    .muted { color: #64748b; }
    .actions { margin-top: 18px; display: flex; gap: 12px; flex-wrap: wrap; }
    .history-list { margin-top: 16px; display: grid; gap: 12px; }
    .history-item { border: 1px solid #e2e8f0; border-radius: 12px; padding: 14px; background: #f8fafc; }
    .history-item strong { display: block; margin-bottom: 6px; color: #0f172a; }
    .history-meta { font-size: 13px; color: #475569; line-height: 1.8; }
    .history-status-success { color: #15803d; font-weight: 700; }
    .history-status-failed { color: #dc2626; font-weight: 700; }
    @media (max-width: 920px) { .grid { grid-template-columns: 1fr; } .plan-grid { grid-template-columns: 1fr; } }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="topbar">
      <div class="logo">WAChatPrint</div>
      <div class="topbar-right">
        <button class="btn btn-secondary" id="refreshPlanBtn" type="button">Refresh Plan</button>
        <button class="btn" id="logoutBtn" type="button">Logout</button>
      </div>
    </div>

    <div class="grid">
      <div class="card">
        <h1>Welcome</h1>
        <p id="userEmail">Checking your session...</p>
        <div class="status">Your account is active.</div>
        <div id="planBadge" class="pill pill-free">FREE</div>

        <div class="plan-grid">
          <div class="plan-box">
            <div class="plan-label">Current Plan</div>
            <div class="plan-value" id="planValue">FREE</div>
            <div class="plan-sub" id="planStatusText">Loading plan...</div>
          </div>
          <div class="plan-box">
            <div class="plan-label">File Limit</div>
            <div class="plan-value" id="fileLimitValue">5 MB</div>
            <div class="plan-sub">Maximum file size for your plan</div>
          </div>
          <div class="plan-box">
            <div class="plan-label">Daily Limit</div>
            <div class="plan-value" id="dailyLimitValue">2</div>
            <div class="plan-sub">Conversions allowed per 24 hours</div>
          </div>
          <div class="plan-box">
            <div class="plan-label">Remaining</div>
            <div class="plan-value" id="remainingValue">2</div>
            <div class="plan-sub" id="usedText">0 used in last 24 hours</div>
          </div>
        </div>

        <div class="plan-actions">
          <button class="btn btn-green" id="upgradeBtn" type="button">Upgrade to Pro</button>
          <button class="btn btn-secondary" id="billingBtn" type="button" style="display:none;">Manage Billing</button>
        </div>

        <div class="message muted" id="planMessage">Loading billing details...</div>
      </div>

      <div class="card">
        <h2>How it works</h2>
        <p>Upload your WhatsApp export file and convert it to PDF. Your plan controls the file size limit and daily conversion count.</p>
        <div class="note" style="margin-top:16px;">
          Free plan: 5 MB and 2 conversions per 24 hours<br />
          Pro plan: 50 MB and 50 conversions per 24 hours<br />
          Supported files: .txt and .zip
        </div>
      </div>
    </div>

    <div class="card">
      <h2>Convert WhatsApp Export to PDF</h2>
      <p>Upload your exported WhatsApp chat file. Direct conversion works for <strong>.txt</strong> and <strong>.zip</strong> files.</p>

      <div class="upload-box">
        <label for="chatFile"><strong>Select file</strong></label>
        <input type="file" id="chatFile" accept=".zip,.txt" />
        <div class="note">Final file size and usage checks are enforced on the server based on your plan.</div>
        <div class="file-info" id="fileInfo"></div>

        <div class="actions">
          <button class="btn" id="checkBtn" type="button">Validate File</button>
          <button class="btn" id="convertBtn" type="button">Convert to PDF</button>
          <button class="btn btn-secondary" id="clearBtn" type="button">Clear</button>
        </div>

        <div class="message muted" id="uploadMessage">No file selected yet.</div>
      </div>
    </div>

    <div class="card">
      <h2>Recent History</h2>
      <p>Your latest conversions will appear here.</p>
      <div id="historyList" class="history-list">
        <div class="message muted">No history yet.</div>
      </div>
    </div>
  </div>

  <script type="module">
    import { createClient } from 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js/+esm';

    const supabaseUrl = 'https://owlzekmxjuaqxyqbssit.supabase.co';
    const supabaseKey = 'sb_publishable_GjhNJ7BAtIG4Rk9qvGFk3w_7hVgqDJF';
    const API_BASE = 'https://web-production-4854d.up.railway.app';
    const supabase = createClient(supabaseUrl, supabaseKey);

    const els = {
      userEmail: document.getElementById('userEmail'),
      logoutBtn: document.getElementById('logoutBtn'),
      refreshPlanBtn: document.getElementById('refreshPlanBtn'),
      upgradeBtn: document.getElementById('upgradeBtn'),
      billingBtn: document.getElementById('billingBtn'),
      planValue: document.getElementById('planValue'),
      fileLimitValue: document.getElementById('fileLimitValue'),
      dailyLimitValue: document.getElementById('dailyLimitValue'),
      remainingValue: document.getElementById('remainingValue'),
      usedText: document.getElementById('usedText'),
      planStatusText: document.getElementById('planStatusText'),
      planMessage: document.getElementById('planMessage'),
      planBadge: document.getElementById('planBadge'),
      chatFile: document.getElementById('chatFile'),
      fileInfo: document.getElementById('fileInfo'),
      uploadMessage: document.getElementById('uploadMessage'),
      checkBtn: document.getElementById('checkBtn'),
      convertBtn: document.getElementById('convertBtn'),
      clearBtn: document.getElementById('clearBtn'),
      historyList: document.getElementById('historyList')
    };

    const MAX_SYSTEM_SIZE = 50 * 1024 * 1024;
    const ALLOWED_EXTENSIONS = ['zip', 'txt'];

    let currentUser = null;
    let currentPlanData = {
      plan: 'free',
      max_file_size_mb: 5,
      daily_conversion_limit: 2,
      used_last_24h: 0,
      remaining_conversions: 2,
      subscription_status: 'inactive'
    };

    function formatFileSize(bytes) {
      if (bytes < 1024) return `${bytes} bytes`;
      if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(2)} KB`;
      return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
    }

    function escapeHtml(value) {
      return String(value ?? '')
        .replaceAll('&', '&amp;')
        .replaceAll('<', '&lt;')
        .replaceAll('>', '&gt;')
        .replaceAll('"', '&quot;')
        .replaceAll("'", '&#039;');
    }

    async function getAccessToken() {
      const result = await supabase.auth.getSession();
      return result?.data?.session?.access_token || null;
    }

    async function apiFetch(path, options = {}) {
      const token = await getAccessToken();
      if (!token) throw new Error('Please log in again.');

      const headers = Object.assign({}, options.headers || {}, {
        Authorization: `Bearer ${token}`
      });

      const response = await fetch(`${API_BASE}${path}`, Object.assign({}, options, { headers }));

      let data = null;
      try {
        data = await response.json();
      } catch (_) {}

      if (!response.ok) {
        throw new Error((data && (data.detail || data.error)) || 'Request failed');
      }
      return data;
    }

    function renderPlan(data) {
      currentPlanData = Object.assign({}, currentPlanData, data || {});
      const planUpper = (currentPlanData.plan || 'free').toUpperCase();
      const fileLimit = currentPlanData.max_file_size_mb || 5;
      const dailyLimit = currentPlanData.daily_conversion_limit || 2;
      const used = currentPlanData.used_last_24h || 0;
      const remaining = Math.max((currentPlanData.remaining_conversions ?? (dailyLimit - used)), 0);

      els.planValue.textContent = planUpper;
      els.fileLimitValue.textContent = `${fileLimit} MB`;
      els.dailyLimitValue.textContent = `${dailyLimit}`;
      els.remainingValue.textContent = `${remaining}`;
      els.usedText.textContent = `${used} used in last 24 hours`;

      if (planUpper === 'PRO') {
        els.planBadge.textContent = 'PRO';
        els.planBadge.className = 'pill pill-pro';
        els.upgradeBtn.style.display = 'none';
        els.billingBtn.style.display = 'inline-flex';
      } else {
        els.planBadge.textContent = 'FREE';
        els.planBadge.className = 'pill pill-free';
        els.upgradeBtn.style.display = 'inline-flex';
        els.billingBtn.style.display = 'none';
      }

      let statusText = currentPlanData.subscription_status || 'inactive';
      if (currentPlanData.subscription_cancel_at_period_end) {
        statusText += ' • cancels at period end';
      }
      if (currentPlanData.current_period_end) {
        try {
          statusText += ` • ends ${new Date(currentPlanData.current_period_end).toLocaleString()}`;
        } catch (_) {}
      }
      els.planStatusText.textContent = statusText;
    }

    function setPlanMessage(text, type) {
      els.planMessage.textContent = text;
      els.planMessage.className = `message ${type}`;
    }

    function resetFileView() {
      els.chatFile.value = '';
      els.fileInfo.style.display = 'none';
      els.fileInfo.innerHTML = '';
      els.uploadMessage.textContent = 'No file selected yet.';
      els.uploadMessage.className = 'message muted';
    }

    function getSelectedFile() {
      return els.chatFile.files[0] || null;
    }

    function validateSelectedFile() {
      const file = getSelectedFile();

      if (!file) {
        els.uploadMessage.textContent = 'Please select a file first.';
        els.uploadMessage.className = 'message error';
        els.fileInfo.style.display = 'none';
        return { ok: false };
      }

      const extension = file.name.split('.').pop().toLowerCase();

      if (!ALLOWED_EXTENSIONS.includes(extension)) {
        els.uploadMessage.textContent = 'Invalid file type. Please upload only .zip or .txt files.';
        els.uploadMessage.className = 'message error';
        els.fileInfo.style.display = 'none';
        return { ok: false };
      }

      if (file.size > MAX_SYSTEM_SIZE) {
        els.uploadMessage.textContent = 'File is too large. Current system max is 50 MB.';
        els.uploadMessage.className = 'message error';
        els.fileInfo.style.display = 'block';
        els.fileInfo.innerHTML = `<strong>Selected File:</strong> ${escapeHtml(file.name)}<br><strong>Size:</strong> ${formatFileSize(file.size)}<br><strong>Status:</strong> Over system limit`;
        return { ok: false };
      }

      const planLimit = currentPlanData.max_file_size_mb || 5;
      els.fileInfo.style.display = 'block';
      els.fileInfo.innerHTML = `<strong>Selected File:</strong> ${escapeHtml(file.name)}<br><strong>Type:</strong> .${escapeHtml(extension)}<br><strong>Size:</strong> ${formatFileSize(file.size)}<br><strong>Status:</strong> Valid<br><strong>Plan Check:</strong> Your current plan limit is ${planLimit} MB`;
      els.uploadMessage.textContent = 'File looks good. Click Convert to PDF.';
      els.uploadMessage.className = 'message success';

      return { ok: true, file };
    }

    async function uploadPdfToStorage(blob, originalFileName) {
      if (!currentUser) throw new Error('User session not found.');

      const baseName = originalFileName.replace(/\.[^/.]+$/, '');
      const safeBaseName = baseName.replace(/[^a-zA-Z0-9._-]/g, '_');
      const pdfFileName = `${Date.now()}-${safeBaseName}.pdf`;
      const pdfPath = `${currentUser.id}/${pdfFileName}`;

      const result = await supabase.storage
        .from('generated-pdfs')
        .upload(pdfPath, blob, { contentType: 'application/pdf', upsert: false });

      const data = result.data;
      const error = result.error;

      if (error) {
        const msg = (error.message || '').toLowerCase();
        if (msg.includes('maximum allowed size') || msg.includes('object exceeded') || msg.includes('payload too large') || msg.includes('entity too large')) {
          return {
            ok: false,
            tooLarge: true,
            path: null,
            message: 'PDF was generated and downloaded, but it was too large to save in history.'
          };
        }
        throw new Error(`PDF upload failed: ${error.message}`);
      }

      return { ok: true, tooLarge: false, path: data.path, message: null };
    }

    async function saveHistoryRow(file, status, pdfPath, errorMessage) {
      try {
        await supabase.from('conversion_history').insert({
          original_filename: file.name,
          file_type: file.name.split('.').pop().toLowerCase(),
          file_size_bytes: file.size,
          status: status,
          error_message: errorMessage || null,
          pdf_path: pdfPath || null
        });
      } catch (_) {}
    }

    async function openSignedPdf(pdfPath) {
      const result = await supabase.storage.from('generated-pdfs').createSignedUrl(pdfPath, 60);
      if (result.error) {
        alert(`Could not create download link: ${result.error.message}`);
        return;
      }
      window.open(result.data.signedUrl, '_blank');
    }

    function renderHistory(rows) {
      if (!rows || rows.length === 0) {
        els.historyList.innerHTML = `<div class="message muted">No history yet.</div>`;
        return;
      }

      els.historyList.innerHTML = rows.map((row) => {
        const statusClass = row.status === 'success' ? 'history-status-success' : 'history-status-failed';
        const noteLabel = row.status === 'success' ? 'Note' : 'Error';
        const errorLine = row.error_message ? `<div><strong>${noteLabel}:</strong> ${escapeHtml(row.error_message)}</div>` : '';
        const createdAt = new Date(row.created_at).toLocaleString();
        const downloadButton = row.pdf_path ? `<button class="btn btn-green" data-pdf-path="${escapeHtml(row.pdf_path)}" style="margin-top:10px;">Download PDF</button>` : '';

        return `<div class="history-item"><strong>${escapeHtml(row.original_filename)}</strong><div class="history-meta"><div><strong>Type:</strong> .${escapeHtml(row.file_type)}</div><div><strong>Size:</strong> ${formatFileSize(row.file_size_bytes)}</div><div><strong>Status:</strong> <span class="${statusClass}">${escapeHtml(row.status)}</span></div><div><strong>Created:</strong> ${escapeHtml(createdAt)}</div>${errorLine}</div>${downloadButton}</div>`;
      }).join('');

      els.historyList.querySelectorAll('[data-pdf-path]').forEach((btn) => {
        btn.addEventListener('click', function () {
          openSignedPdf(btn.getAttribute('data-pdf-path'));
        });
      });
    }

    async function loadHistory() {
      try {
        const result = await supabase
          .from('conversion_history')
          .select('id, original_filename, file_type, file_size_bytes, status, error_message, pdf_path, created_at')
          .order('created_at', { ascending: false })
          .limit(20);

        if (result.error) {
          els.historyList.innerHTML = `<div class="message error">Could not load history.</div>`;
          return;
        }
        renderHistory(result.data || []);
      } catch (_) {
        els.historyList.innerHTML = `<div class="message error">Could not load history.</div>`;
      }
    }

    async function loadPlanSummary() {
      try {
        setPlanMessage('Loading billing details...', 'muted');
        const data = await apiFetch('/usage-summary');
        renderPlan(data);
        const remaining = Math.max((data.remaining_conversions ?? 0), 0);
        setPlanMessage(`Plan synced. ${remaining} conversions remaining in the last 24-hour window.`, 'success');
      } catch (err) {
        renderPlan({
          plan: 'free',
          max_file_size_mb: 5,
          daily_conversion_limit: 2,
          used_last_24h: 0,
          remaining_conversions: 2,
          subscription_status: 'inactive'
        });
        setPlanMessage(err.message || 'Could not load billing details.', 'error');
      }
    }

    async function refreshPlan() {
      try {
        setPlanMessage('Refreshing plan...', 'muted');
        await apiFetch('/refresh-subscription', { method: 'POST' });
      } catch (_) {}
      await loadPlanSummary();
    }

    async function startCheckout() {
      try {
        els.upgradeBtn.disabled = true;
        setPlanMessage('Opening checkout...', 'muted');
        const data = await apiFetch('/create-checkout-session', { method: 'POST' });
        if (data.url) {
          window.location.href = data.url;
          return;
        }
        throw new Error('Checkout URL not received.');
      } catch (err) {
        setPlanMessage(err.message || 'Could not open checkout.', 'error');
      } finally {
        els.upgradeBtn.disabled = false;
      }
    }

    async function openBillingPortal() {
      try {
        els.billingBtn.disabled = true;
        setPlanMessage('Opening billing portal...', 'muted');
        const data = await apiFetch('/create-billing-portal', { method: 'POST' });
        if (data.url) {
          window.location.href = data.url;
          return;
        }
        throw new Error('Billing portal URL not received.');
      } catch (err) {
        setPlanMessage(err.message || 'Could not open billing portal.', 'error');
      } finally {
        els.billingBtn.disabled = false;
      }
    }

    async function verifyCheckoutIfNeeded() {
      const params = new URLSearchParams(window.location.search);
      const billing = params.get('billing');
      const sessionId = params.get('session_id');

      if (billing === 'success' && sessionId) {
        try {
          setPlanMessage('Verifying payment...', 'muted');
          await apiFetch('/verify-checkout-session', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId })
          });
          const cleanUrl = `${window.location.origin}${window.location.pathname}`;
          window.history.replaceState({}, document.title, cleanUrl);
        } catch (err) {
          setPlanMessage(err.message || 'Payment verification failed.', 'error');
        }
      }

      if (billing === 'cancel') {
        setPlanMessage('Checkout was canceled.', 'error');
        const cleanUrl = `${window.location.origin}${window.location.pathname}`;
        window.history.replaceState({}, document.title, cleanUrl);
      }
    }

    async function convertFileToPdf() {
      const result = validateSelectedFile();
      if (!result.ok) return;

      const file = result.file;
      const formData = new FormData();
      formData.append('file', file);

      try {
        els.convertBtn.disabled = true;
        els.uploadMessage.textContent = 'Converting file to PDF...';
        els.uploadMessage.className = 'message muted';

        const token = await getAccessToken();
        if (!token) throw new Error('Please log in again.');

        const response = await fetch(`${API_BASE}/convert-txt`, {
          method: 'POST',
          headers: { Authorization: `Bearer ${token}` },
          body: formData
        });

        if (!response.ok) {
          let errorText = 'Conversion failed.';
          try {
            const errorData = await response.json();
            errorText = errorData.detail || errorData.error || errorText;
          } catch (_) {}
          throw new Error(errorText);
        }

        const blob = await response.blob();

        const downloadUrl = window.URL.createObjectURL(blob);
        const link = document.createElement('a');
        const baseName = file.name.replace(/\.[^/.]+$/, '');
        link.href = downloadUrl;
        link.download = `${baseName}.pdf`;
        document.body.appendChild(link);
        link.click();
        link.remove();
        window.URL.revokeObjectURL(downloadUrl);

        const uploadResult = await uploadPdfToStorage(blob, file.name);
        await saveHistoryRow(file, 'success', uploadResult.ok ? uploadResult.path : null, uploadResult.tooLarge ? uploadResult.message : null);

        await loadHistory();
        await loadPlanSummary();

        if (uploadResult.tooLarge) {
          els.uploadMessage.textContent = uploadResult.message;
        } else {
          els.uploadMessage.textContent = 'PDF generated successfully. Download started and saved to history.';
        }
        els.uploadMessage.className = 'message success';
      } catch (err) {
        await saveHistoryRow(file, 'failed', null, err.message || 'Something went wrong during conversion.');
        await loadHistory();
        await loadPlanSummary();

        els.uploadMessage.textContent = err.message || 'Something went wrong during conversion.';
        els.uploadMessage.className = 'message error';
      } finally {
        els.convertBtn.disabled = false;
      }
    }

    async function loadUser() {
      try {
        const result = await supabase.auth.getUser();
        const user = result?.data?.user;

        if (!user) {
          window.location.href = '/auth.html?mode=login';
          return;
        }

        currentUser = user;
        els.userEmail.textContent = `Logged in as: ${user.email}`;
        await verifyCheckoutIfNeeded();
        await loadPlanSummary();
        await loadHistory();
      } catch (err) {
        els.userEmail.textContent = 'Could not load session.';
        setPlanMessage(err.message || 'Dashboard failed to load.', 'error');
      }
    }

    els.logoutBtn.addEventListener('click', async function () {
      await supabase.auth.signOut();
      window.location.href = '/';
    });

    els.refreshPlanBtn.addEventListener('click', refreshPlan);
    els.upgradeBtn.addEventListener('click', startCheckout);
    els.billingBtn.addEventListener('click', openBillingPortal);
    els.checkBtn.addEventListener('click', validateSelectedFile);
    els.convertBtn.addEventListener('click', convertFileToPdf);
    els.clearBtn.addEventListener('click', resetFileView);

    els.chatFile.addEventListener('change', function () {
      els.uploadMessage.textContent = 'File selected. Click Validate File.';
      els.uploadMessage.className = 'message muted';
    });

    window.addEventListener('error', function (event) {
      setPlanMessage(`Page error: ${event.message}`, 'error');
    });

    loadUser();
  </script>
</body>
</html>
