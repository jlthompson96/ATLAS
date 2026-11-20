// Pairing page: redeems a one-time code. No inline script, no external requests (CSP default-src 'self').
// The code travels in a header, not the URL, so it never lands in a history entry or an access log.
const form = document.getElementById('pair');
const input = document.getElementById('code');
const msg = document.getElementById('msg');

const MESSAGES = {
  401: 'That code is wrong or has expired. Ask for a new one.',
  403: 'This address is not allowed to pair.',
  429: 'Too many attempts. Wait a few minutes and try again.'
};

form.addEventListener('submit', async (event) => {
  event.preventDefault();
  const code = input.value.trim();
  if (!code) return;
  msg.textContent = 'Checking...';
  try {
    const res = await fetch('/api/pair', {
      method: 'GET',
      headers: { 'X-Atlas-Pairing-Code': code },
      credentials: 'same-origin',
      cache: 'no-store'
    });
    if (res.ok) {
      msg.textContent = 'Paired. Opening ATLAS...';
      location.replace('/');
      return;
    }
    msg.textContent = MESSAGES[res.status] ?? 'Pairing failed.';
  } catch {
    msg.textContent = 'Could not reach ATLAS.';
  }
  input.select();
});
