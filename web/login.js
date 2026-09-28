// Sign-in page (docs/training-site.md section 1).
import { api } from './api.js';

const $ = (id) => document.getElementById(id);

// Only a path on this site, never another origin ("//evil" or "https:").
export function safeNext(raw) {
  return typeof raw === 'string' && /^\/(?![\/\\])/.test(raw) ? raw : '/';
}

const next = safeNext(new URLSearchParams(location.search).get('next'));

$('login-form').onsubmit = async (e) => {
  e.preventDefault();
  const status = $('status');
  status.className = 'status';
  status.textContent = 'Signing in…';
  try {
    await api.login($('email').value, $('password').value);
    location.replace(next);
  } catch (err) {
    status.className = 'status error';
    status.textContent = err.message;
    $('password').value = '';
    $('password').focus();
  }
};
