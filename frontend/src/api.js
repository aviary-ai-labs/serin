const API = '';

// App lock: the session cookie does the real work for the web UI; the token
// mirror in localStorage covers dev setups where the Vite proxy strips
// cookies, and is what the mobile pairing QR embeds.
const TOKEN_KEY = 'serin_token';

export function getAuthToken() {
  try {
    return localStorage.getItem(TOKEN_KEY) || '';
  } catch {
    return '';
  }
}

export function setAuthToken(token) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
    // Private-mode storage failures are fine — the cookie still works.
  }
}

export async function api(path, options = {}) {
  const headers = options.body instanceof FormData ? {} : { 'Content-Type': 'application/json' };
  const token = getAuthToken();
  if (token) headers.Authorization = `Bearer ${token}`;
  const response = await fetch(`${API}${path}`, {
    ...options,
    headers: { ...headers, ...(options.headers || {}) },
  });
  if (response.status === 401) {
    // Tell the app shell to show the lock screen; callers still get an error.
    window.dispatchEvent(new CustomEvent('serin:locked'));
  }
  if (!response.ok) {
    const text = await response.text();
    const error = new Error(extractErrorMessage(text) || `${response.status} ${response.statusText}`);
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function extractErrorMessage(text) {
  if (!text) return '';
  try {
    const parsed = JSON.parse(text);
    if (typeof parsed.detail === 'string') return parsed.detail;
    if (Array.isArray(parsed.detail)) {
      return parsed.detail.map(item => item.msg || JSON.stringify(item)).join('; ');
    }
    return text;
  } catch {
    return text;
  }
}
