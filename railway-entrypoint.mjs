import { spawn, execFileSync } from 'node:child_process';
import { createHmac } from 'node:crypto';
import { mkdirSync, readdirSync, readlinkSync, unlinkSync } from 'node:fs';
import { createServer } from 'node:http';
import { join } from 'node:path';

const password = process.env.ACCESS_PASSWORD;
const username = process.env.ACCESS_USERNAME ?? 'admin';
if (!/^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}$/.test(username)) {
  console.error('ACCESS_USERNAME must be 1-64 letters, numbers, underscores, dots or hyphens, starting with a letter or number.');
  process.exit(1);
}
if (!password || !/^[a-zA-Z0-9]{32,}$/.test(password)) {
  console.error('ACCESS_PASSWORD is required: use at least 32 letters and numbers.');
  process.exit(1);
}
const port = process.env.PORT || '8080';
if (!/^\d+$/.test(port) || Number(port) < 1 || Number(port) > 65535 || [3001, 3002].includes(Number(port))) {
  console.error('PORT must be an integer between 1 and 65535, except internal ports 3001 and 3002.');
  process.exit(1);
}
process.chdir('/app');
execFileSync(process.execPath, ['/opt/railway/build-fingerprint.mjs'], { stdio: 'inherit' });
if (readlinkSync('/app/.wrangler/state') !== '/data/state') {
  throw new Error('Mount the persistent volume at /data. Do not mount over /app/.wrangler.');
}
mkdirSync('/data/state', { recursive: true });
// Preview must load current runtime bindings even if an image update leaves
// a generated build-time snapshot in the container filesystem.
function clearBuildBindings(directory) {
  for (const entry of readdirSync(directory, { withFileTypes: true })) {
    const file = join(directory, entry.name);
    if (entry.isDirectory()) clearBuildBindings(file);
    else if (entry.isFile() && entry.name.startsWith('.dev.vars')) unlinkSync(file);
  }
}
clearBuildBindings('/app/dist');
const appEnv = {
  ...process.env,
  ALLOWED_HOST: process.env.ALLOWED_HOST || process.env.RAILWAY_PUBLIC_DOMAIN || 'localhost',
  PORT: '3001',
};
// Credentials are consumed by Caddy and never forwarded to OpenSEO.
delete appEnv.ACCESS_PASSWORD;
delete appEnv.ACCESS_USERNAME;

const children = new Set();
const healthServer = createServer(async (request, response) => {
  if (request.url !== '/healthz') {
    response.writeHead(404).end('Not found');
    return;
  }
  try {
    const upstream = await fetch('http://127.0.0.1:3001/api/health', { signal: AbortSignal.timeout(5000) });
    const setup = await upstream.json();
    if (!upstream.ok || setup.checks?.database?.status !== 'ok') throw new Error('Not ready');
    response.writeHead(200, { 'Content-Type': 'text/plain' }).end('ok');
  } catch {
    response.writeHead(503, { 'Content-Type': 'text/plain' }).end('Not ready');
  }
});
let stopping = false;
let stopTimer;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  process.exitCode = code;
  healthServer.close();
  for (const child of children) child.kill('SIGTERM');
  stopTimer = setTimeout(() => {
    for (const child of children) child.kill('SIGKILL');
  }, 8000);
  stopTimer.unref();
}
process.on('SIGTERM', () => stop());
process.on('SIGINT', () => stop());
function launch(command, args, env, service = false) {
  const child = spawn(command, args, { env, stdio: 'inherit' });
  children.add(child);
  return new Promise((resolve, reject) => {
    child.once('error', reject);
    child.once('exit', (code, signal) => {
      children.delete(child);
      if (!children.size && stopTimer) clearTimeout(stopTimer);
      if (service && !stopping) {
        console.error(`[railway] ${command} stopped unexpectedly.`);
        stop(code || 1);
      }
      if (code === 0 || stopping) resolve();
      else reject(new Error(`${command} exited with ${code ?? signal}.`));
    });
  });
}

try {
  await launch('pnpm', ['exec', 'tsx', 'scripts/selfhost-preflight.ts'], appEnv);
  if (!stopping) await launch('pnpm', ['run', 'db:migrate:local'], appEnv);
  if (!stopping) {
    await new Promise((resolve, reject) => {
      healthServer.once('error', reject);
      healthServer.listen(3002, '127.0.0.1', resolve);
    });
    const gatewayEnv = {
      ...process.env,
      PORT: port,
      ACCESS_USERNAME: username,
      PASSWORD_HASH: execFileSync('caddy', ['hash-password', '--plaintext', password], { encoding: 'utf8' }).trim(),
      GATE_COOKIE: createHmac('sha256', password).update(`openseo-browser-session-v1:${username}`).digest('hex'),
    };
    execFileSync('caddy', ['validate', '--config', '/opt/railway/Caddyfile', '--adapter', 'caddyfile'], { env: gatewayEnv, stdio: 'inherit' });
    console.log('[railway] Starting prebuilt OpenSEO, its scheduler, and Caddy. No startup build.');
    await Promise.all([
      launch(process.execPath, ['scripts/selfhost-scheduler.mjs'], appEnv, true),
      launch('caddy', ['run', '--config', '/opt/railway/Caddyfile', '--adapter', 'caddyfile'], gatewayEnv, true),
    ]);
  }
} catch (error) {
  console.error(`[railway] ${error.message}`);
  stop(1);
}
