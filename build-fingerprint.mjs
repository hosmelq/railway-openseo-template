import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync } from 'node:fs';

const path = '/opt/railway/build-env.sha256';
const values = Object.entries(process.env)
  .filter(([key]) => /^(VITE_|AUTH_MODE|BYPASS_EMAIL_VERIFICATION|POSTHOG_PUBLIC_KEY|POSTHOG_HOST|TURNSTILE_SITE_KEY|POSTHOG_SOURCEMAPS)/.test(key))
  .sort(([a], [b]) => a < b ? -1 : a > b ? 1 : 0)
  .map(([key, value]) => `${key}=${value}\n`).join('');
const hash = createHash('sha256').update(values).digest('hex');
if (process.argv[2] === 'write') {
  writeFileSync(path, hash);
} else if (readFileSync(path, 'utf8') !== hash) {
  throw new Error('Build settings differ from this prebuilt image. Remove overrides for AUTH_MODE, VITE_*, BYPASS_EMAIL_VERIFICATION, POSTHOG_PUBLIC_KEY, POSTHOG_HOST, TURNSTILE_SITE_KEY and POSTHOG_SOURCEMAPS. This image never rebuilds at startup.');
}
