<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Billing Success | WAChatPrint</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: Arial, sans-serif;
      background: #f8fafc;
      color: #0f172a;
      min-height: 100vh;
      display: flex;
      align-items: center;
      justify-content: center;
      padding: 24px;
    }
    .card {
      width: 100%;
      max-width: 640px;
      background: white;
      border-radius: 20px;
      padding: 32px;
      border: 1px solid #e2e8f0;
      box-shadow: 0 10px 30px rgba(0,0,0,0.06);
      text-align: center;
    }
    h1 {
      font-size: 32px;
      margin-bottom: 12px;
    }
    p {
      color: #475569;
      line-height: 1.7;
      margin-bottom: 12px;
    }
    .status {
      margin-top: 14px;
      font-weight: 700;
      color: #2563eb;
    }
    .status.error {
      color: #dc2626;
    }
    .actions {
      margin-top: 22px;
      display: flex;
      gap: 12px;
      justify-content: center;
      flex-wrap: wrap;
    }
    .btn {
      display: inline-block;
      border: none;
      background: #2563eb;
      color: white;
      padding: 12px 18px;
      border-radius: 10px;
      text-decoration: none;
      font-weight: 600;
      cursor: pointer;
    }
    .btn-secondary {
      background: #e2e8f0;
      color: #0f172a;
    }
  </style>
</head>
<body>
  <div class="card">
    <h1>Payment received</h1>
    <p>Your checkout was completed successfully.</p>
    <p>We are verifying and activating your plan now.</p>

    <div id="status" class="status">Checking payment session...</div>

    <div class="actions">
      <a class="btn" href="/dashboard.html">Go to Dashboard</a>
      <a class="btn btn-secondary" href="/">Back to Home</a>
    </div>
  </div>

  <script type="module">
    import { createClient } from 'https://cdn.jsdelivr.net/npm/@supabase/supabase-js/+esm';

    const supabaseUrl = 'https://owlzekmxjuaqxyqbssit.supabase.co';
    const supabaseKey = 'sb_publishable_GjhNJ7BAtIG4Rk9qvGFk3w_7hVgqDJF';
    const RAILWAY_API_BASE = 'https://web-production-4854d.up.railway.app';

    const supabase = createClient(supabaseUrl, supabaseKey);
    const statusEl = document.getElementById('status');

    async function getAccessToken() {
      const { data: { session } } = await supabase.auth.getSession();
      return session?.access_token || null;
    }

    async function verifyPlan() {
      try {
        const params = new URLSearchParams(window.location.search);
        const sessionId = params.get('session_id');

        if (!sessionId) {
          statusEl.textContent = 'Missing checkout session id.';
          statusEl.classList.add('error');
          return;
        }

        const accessToken = await getAccessToken();
        if (!accessToken) {
          statusEl.textContent = 'Please log in again, then open dashboard.';
          statusEl.classList.add('error');
          return;
        }

        statusEl.textContent = 'Verifying checkout and activating plan...';

        const response = await fetch(`${RAILWAY_API_BASE}/verify-checkout-session`, {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'Authorization': `Bearer ${accessToken}`
          },
          body: JSON.stringify({ session_id: sessionId })
        });

        const data = await response.json();

        if (!response.ok || !data.success) {
          statusEl.textContent = data.detail || data.error || data.message || 'Plan verification did not complete yet.';
          statusEl.classList.add('error');
          return;
        }

        statusEl.textContent = 'Plan activated successfully. Redirecting to dashboard...';
        setTimeout(() => {
          window.location.href = '/dashboard.html';
        }, 1800);

      } catch (err) {
        statusEl.textContent = err.message || 'Something went wrong during verification.';
        statusEl.classList.add('error');
      }
    }

    verifyPlan();
  </script>
</body>
</html>
