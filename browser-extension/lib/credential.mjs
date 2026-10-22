// Sign-in, password, payment and verification pages always hand back to the person (Phase 22).
// Two signals, either one is enough: the address (before anything is read), and the page's own fields as the
// page script reports them (input types and autocomplete hints only; values are never read).
// The lists match backend/pebble/browser_bridge.py (pinned by test_browser_bridge_22.py).

// BEGIN CREDENTIAL LISTS
export const CREDENTIAL_LISTS = Object.freeze({
  "words": ["login", "log-in", "signin", "sign-in", "signon", "sign-on", "sso", "auth", "oauth", "oauth2", "authorize", "password", "passwd", "reset-password", "forgot-password", "2fa", "mfa", "otp", "two-factor", "verify", "verification", "challenge", "captcha", "checkout", "payment", "payments", "pay", "billing", "wallet", "session", "sessions"],
  "hosts": ["accounts.google.com", "login.microsoftonline.com", "login.live.com", "account.live.com", "appleid.apple.com", "idmsa.apple.com", "signin.aws.amazon.com", "www.paypal.com", "paypal.com", "checkout.stripe.com", "pay.google.com", "checkout.shopify.com", "secure.chase.com"],
  "prefixes": ["login.", "signin.", "auth.", "sso.", "accounts.", "checkout.", "pay.", "payments.", "billing.", "secure.", "idp."]
});
// END CREDENTIAL LISTS

const escape = word => word.replace(/[.*+?^${}()|[\]\\-]/g, '\\$&');
const WORDS = new RegExp('(?:^|[/_.\\-])(' + CREDENTIAL_LISTS.words.map(escape).join('|') + ')(?=$|[/_.\\-])', 'i');
const HOSTS = new Set(CREDENTIAL_LISTS.hosts);

// True when an address looks like a sign-in, password, payment or verification page.
export function credentialUrl(url) {
  let parsed;
  try { parsed = new URL(url); } catch { return false; }
  const host = parsed.hostname.toLowerCase();
  if (HOSTS.has(host) || CREDENTIAL_LISTS.prefixes.some(prefix => host.startsWith(prefix))) return true;
  return WORDS.test(parsed.pathname);
}

// The page facts lib/page.mjs reports: counts of fields by kind, never values.
export const FIELD_REASONS = Object.freeze(['password', 'one_time_code', 'payment', 'captcha']);

// Reasons a page is the person's turn, from its address and its reported facts ([] when Pebble may work there).
export function handbackReasons(url, facts) {
  const reasons = [];
  if (credentialUrl(url)) reasons.push('address');
  const counts = facts && typeof facts === 'object' ? facts : {};
  for (const reason of FIELD_REASONS) if (Number.isInteger(counts[reason]) && counts[reason] > 0) reasons.push(reason);
  return reasons;
}
