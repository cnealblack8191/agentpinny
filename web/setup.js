// Set-password page, opened from a one-time link (docs/training-site.md section 1).
// The token is in the URL fragment, which the browser never sends to the server.
import { api } from './api.js';

const $ = (id) => document.getElementById(id);
const token = new URLSearchParams(location.hash.slice(1)).get('token') || '';
// Keep the token out of history and out of anything copied from the address bar.
history.replaceState(null, '', location.pathname);

function fail(message) {
  $('status').className = 'status error';
  $('status').textContent = message;
}

async function start() {
  try {
    const { email } = await api.setupCheck(token);
    $('who').textContent = `Account: ${email}`;
    $('setup-form').hidden = false;
    $('password').focus();
  } catch (err) {
    $('who').textContent = '';
    fail(err.message);
  }
}

$('setup-form').onsubmit = async (e) => {
  e.preventDefault();
  if ($('password').value !== $('password2').value) {
    fail("The two passwords don't match.");
    return;
  }
  try {
    await api.setup(token, $('password').value);
    location.replace('/');
  } catch (err) {
    fail(err.message);
  }
};

start();
