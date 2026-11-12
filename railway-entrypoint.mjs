import { execFileSync, spawn } from "node:child_process";
import {
  existsSync,
  lchownSync,
  mkdirSync,
  readFileSync,
  statSync,
  writeFileSync,
} from "node:fs";
import { setTimeout as delay } from "node:timers/promises";

function fail(message) {
  console.error(`[railway] ${message}`);
  process.exit(1);
}

if (statSync("/data").dev === statSync("/").dev)
  fail("Mount a Railway volume at /data.");
if (existsSync("/data/state"))
  fail(
    "This volume holds data from the previous OpenSEO image (/data/state). Keep that service on ghcr.io/hosmelq/railway-openseo-template:latest, or deploy this edition as a new service with its own volume.",
  );
const storageKeys = [
  "CELLD_BUCKET",
  "S3_ENDPOINT",
  "AWS_REGION",
  "AWS_ACCESS_KEY_ID",
  "AWS_SECRET_ACCESS_KEY",
];
for (const key of storageKeys)
  if (!process.env[key])
    fail(`${key} is required: reference it from the Railway Bucket.`);
const port = process.env.PORT || "3000";
if (
  !/^\d+$/.test(port) ||
  Number(port) < 1024 ||
  Number(port) > 65535 ||
  [8080, 8081, 8090].includes(Number(port))
)
  fail(
    "PORT must be an integer between 1024 and 65535, except internal ports 8080, 8081 and 8090.",
  );
const publicUrl =
  process.env.BETTER_AUTH_URL ||
  (process.env.RAILWAY_PUBLIC_DOMAIN &&
    `https://${process.env.RAILWAY_PUBLIC_DOMAIN}`);
if (!publicUrl)
  fail("Generate a Railway domain or set BETTER_AUTH_URL to the public URL.");
if ((process.env.AUTH_MODE || "password") !== "password")
  fail("AUTH_MODE must be password: a Railway domain is public.");

for (const directory of ["/data", "/data/config", "/data/celld"]) {
  mkdirSync(directory, { recursive: true, mode: 0o700 });
  lchownSync(directory, 1000, 1000);
}
process.setgroups([1000]);
process.setgid(1000);
process.setuid(1000);

const storageEnv = {
  ...process.env,
  AUTH_MODE: "password",
  BETTER_AUTH_URL: publicUrl,
  HOME: "/home/node",
  PORT: port,
};
delete storageEnv.ADMIN_PASSWORD;
const env = { ...storageEnv };
for (const key of storageKeys) delete env[key];
const run = (command, args, options = {}) =>
  execFileSync(command, args, { env, stdio: "inherit", ...options });

const children = new Set();
let stopping = false;
function stop(code = 0) {
  if (stopping) return;
  stopping = true;
  process.exitCode = code;
  for (const child of children) child.kill("SIGTERM");
  setTimeout(() => {
    for (const child of children) child.kill("SIGKILL");
  }, 50_000).unref();
}
process.on("SIGTERM", () => stop());
process.on("SIGINT", () => stop());
function launch(name, command, args, childEnv = env) {
  const child = spawn(command, args, { env: childEnv, stdio: "inherit" });
  children.add(child);
  child.once("exit", (code, signal) => {
    children.delete(child);
    if (!stopping) {
      console.error(`[railway] ${name} stopped (${code ?? signal}).`);
      stop(code || 1);
    }
  });
}
async function retry(description, attempts, interval, action) {
  for (let attempt = 1; attempt <= attempts; attempt++) {
    if (stopping) throw new Error("Stopped during startup.");
    try {
      return await action();
    } catch (error) {
      if (attempt === attempts)
        throw new Error(`${description}: ${error.message}`);
    }
    await delay(interval);
  }
}
async function post(url) {
  const response = await fetch(url, { method: "POST" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
}

try {
  launch(
    "celld",
    "celld",
    ["--listen", "127.0.0.1:8080", "--internal-listen", "127.0.0.1:8081"],
    storageEnv,
  );
  run("node", ["/opt/standalone/configure.mjs"]);
  const wranglerFile = "/config/app/wrangler.json";
  const config = JSON.parse(readFileSync(wranglerFile, "utf8"));
  config.vars.CRAWL_GATEWAY_URL = "http://127.0.0.1:8090";
  writeFileSync(wranglerFile, JSON.stringify(config), { mode: 0o600 });
  launch("crawl gateway", "node", ["/opt/standalone/crawl-gateway.mjs"]);

  await retry("Deployment failed", 30, 2000, () =>
    run("celld", ["deploy", "/config/app"], { env: storageEnv }),
  );
  await retry("Runtime did not load the deployment", 90, 1000, () =>
    post("http://127.0.0.1:8081/reload"),
  );
  await retry("Database migrations did not apply", 30, 2000, () =>
    run(
      "celld",
      ["d1", "migrations", "apply", "openseo-standalone", "/config/app"],
      { env: storageEnv },
    ),
  );
  await post("http://127.0.0.1:8081/reload");
  await retry("OpenSEO did not become ready", 90, 1000, async () => {
    const response = await fetch("http://127.0.0.1:8080/api/health");
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
  });

  const users = run(
    "celld",
    [
      "d1",
      "execute",
      "openseo-standalone",
      "--command",
      "SELECT 'users=' || count(*) FROM user",
      "/config/app",
    ],
    { env: storageEnv, stdio: ["ignore", "pipe", "inherit"], encoding: "utf8" },
  );
  const count = Number(/users=(\d+)/.exec(users)?.[1]);
  if (!Number.isInteger(count)) throw new Error(`Unexpected user count: ${users}`);
  if (count === 0) {
    const email = process.env.ADMIN_EMAIL;
    const password = process.env.ADMIN_PASSWORD;
    if (!email || !password)
      throw new Error(
        "Set ADMIN_EMAIL and ADMIN_PASSWORD to create the administrator account.",
      );
    run(
      "node",
      ["/opt/standalone/admin.mjs", "create-user", email, process.env.ADMIN_NAME || "Admin"],
      { input: `${password}\n`, stdio: ["pipe", "inherit", "inherit"] },
    );
  }

  console.log("[railway] OpenSEO is ready. Starting Caddy.");
  launch("caddy", "caddy", [
    "run",
    "--config",
    "/opt/railway/Caddyfile",
    "--adapter",
    "caddyfile",
  ]);
} catch (error) {
  console.error(`[railway] ${error.message}`);
  stop(1);
}
