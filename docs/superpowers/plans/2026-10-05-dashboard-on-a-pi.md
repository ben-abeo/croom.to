# Crystal Meet dashboard on a Raspberry Pi: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Package the Crystal Meet dashboard so one command installs it on a fourth Raspberry Pi under Docker Compose, with a first admin, a fixed address, nightly backups, a fourth guide, and the backend changes production needs.

**Architecture:** `deploy/dashboard/` holds a compose file (Postgres plus a dashboard image built from the repo), the env example, the backup script and its systemd units; `installer/install-dashboard.sh` installs Docker, clones the fork under `/opt/croom-dashboard`, writes the secrets once, builds and starts the stack and installs the backup timer. The backend gains a production mode: tables created on start, the first admin from the environment, a locked register route, the built web app served from the same origin with a content security policy that works over plain HTTP, and logs on standard output.

**Tech Stack:** Docker Engine with the compose plugin, `node:20-bookworm-slim`, `postgres:16-alpine`, Express 4 and Sequelize 6 (TypeScript), Jest 29 with ts-jest, Vite 5 React web app, bash installer tested with pytest, guides rendered with Playwright Chromium.

**Spec:** `docs/superpowers/specs/2026-10-05-dashboard-on-a-pi-design.md`

## Global Constraints

- Internal names stay `croom`; only what a person sees says Crystal Meet. Container project name `croom-dashboard`, install dir `/opt/croom-dashboard`, backups in `/var/backups/croom-dashboard`.
- No secret ever enters the repo: `deploy/dashboard/.env.example` carries `REPLACE_WITH_...` placeholders only; the real `.env` is written on the Pi with mode 600 and is gitignored.
- The dashboard serves the API, the WebSocket and the web app on one port, 3001; port 80 is published as a second mapping to the same port. Plain HTTP, no TLS.
- 64-bit only (`aarch64` or `x86_64`), Debian-family with apt; refuse below 2 GB of memory, warn below 4 GB.
- Backend `engines.node >= 18`; the image uses Node 20. Express stays at 4.x (`app.get('*')` syntax).
- There are no database migrations: production runs `sequelize.sync()` (create missing tables, never alter); development keeps `sync({ alter: true })`.
- Every commit message is plain; no attribution lines.
- Python tests run with `.venv/bin/pytest -q -p no:cacheprovider <path>`; backend tests with `cd src/croom-dashboard/backend && npx jest`. Before the final commit run the full gate: `bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh dashboard-pi` and read `grep "GATE:"` (81 upstream failures is the baseline).
- The frontend's `npm run build` runs `tsc` first, but the frontend has no `tsconfig.json`, so `tsc` fails; the image and the tasks build the web app with `vite build`.

## Review Focus

Failure modes the spec implies that a person would hit first; each has its test in the owning task:

1. Running the installer a second time with `--admin-email` must leave the env file byte-identical and only warn (Task 7, `test_second_run_keeps_the_env_file`).
2. Generated secrets must be safe inside an env file and compose interpolation: hex only, no `$`, `#`, quotes or spaces (Task 7, `test_write_env_generates_distinct_hex_secrets_with_mode_600`).
3. A request to a page route with a JSON `Accept` header (a device hitting a wrong path) must get the JSON 404, never the HTML shell (Task 3, `non-HTML requests fall through to the JSON 404`).
4. A failed `pg_dump` must not leave a truncated backup behind or exit 0 (Task 6, `test_backup_leaves_no_partial_file_when_the_dump_fails`).
5. `ADMIN_EMAIL` with surrounding whitespace must create the admin with the trimmed address so the sign-in form matches it (Task 1, `trims the email`).

---

### Task 1: Backend test harness, first-run admin, production table sync

**Files:**
- Create: `src/croom-dashboard/backend/jest.config.js`
- Create: `src/croom-dashboard/backend/src/bootstrap.ts`
- Create: `src/croom-dashboard/backend/src/__tests__/bootstrap.test.ts`
- Modify: `src/croom-dashboard/backend/tsconfig.json` (exclude the tests from the build)
- Modify: `src/croom-dashboard/backend/src/models/index.ts:229-243` (`initDatabase`)
- Modify: `src/croom-dashboard/backend/src/index.ts:33-36` (call `ensureAdmin`)

**Interfaces:**
- Produces: `ensureAdmin(users: UserStore, env: AdminEnv, log?: Log): Promise<'created' | 'exists' | 'skipped'>` and `syncOptions(nodeEnv: string | undefined): { alter?: boolean }` in `bootstrap.ts`; `UserStore` has `findOne({ where: { email } })`, `count()`, `create({ email, passwordHash, name, role: 'admin' })`.

- [ ] **Step 1: Add the Jest config and keep tests out of the build**

Create `src/croom-dashboard/backend/jest.config.js`:

```js
/** Jest runs the TypeScript tests under src/__tests__ directly through ts-jest. */
module.exports = {
  preset: 'ts-jest',
  testEnvironment: 'node',
  roots: ['<rootDir>/src'],
  testMatch: ['**/__tests__/**/*.test.ts'],
};
```

In `src/croom-dashboard/backend/tsconfig.json` change the exclude line to:

```json
  "exclude": ["node_modules", "dist", "src/__tests__"]
```

- [ ] **Step 2: Write the failing tests**

Create `src/croom-dashboard/backend/src/__tests__/bootstrap.test.ts`:

```ts
import bcrypt from 'bcrypt';
import { ensureAdmin, syncOptions, UserStore } from '../bootstrap';

type Created = { email: string; passwordHash: string; name: string; role: string };

function fakeUsers(existing: string[] = []): UserStore & { created: Created[] } {
  const store = {
    created: [] as Created[],
    async findOne({ where }: { where: { email: string } }) {
      return existing.includes(where.email) || store.created.some((u) => u.email === where.email) ? { email: where.email } : null;
    },
    async count() {
      return existing.length + store.created.length;
    },
    async create(values: Created) {
      store.created.push(values);
      return values;
    },
  };
  return store;
}

const quiet = { info: jest.fn(), warn: jest.fn() };

describe('ensureAdmin', () => {
  beforeEach(() => jest.clearAllMocks());

  it('creates the admin from the environment when no such user exists', async () => {
    const users = fakeUsers();
    const outcome = await ensureAdmin(users, { ADMIN_EMAIL: 'ben@crystalpm.com', ADMIN_PASSWORD: 'first-secret' }, quiet);
    expect(outcome).toBe('created');
    expect(users.created).toHaveLength(1);
    const [admin] = users.created;
    expect(admin).toMatchObject({ email: 'ben@crystalpm.com', name: 'Administrator', role: 'admin' });
    expect(await bcrypt.compare('first-secret', admin.passwordHash)).toBe(true);
    expect(quiet.info).toHaveBeenCalledWith('Created admin user ben@crystalpm.com');
  });

  it('leaves an existing user alone so a changed password survives restarts', async () => {
    const users = fakeUsers(['ben@crystalpm.com']);
    const outcome = await ensureAdmin(users, { ADMIN_EMAIL: 'ben@crystalpm.com', ADMIN_PASSWORD: 'anything' }, quiet);
    expect(outcome).toBe('exists');
    expect(users.created).toHaveLength(0);
  });

  it('trims the email', async () => {
    const users = fakeUsers();
    await ensureAdmin(users, { ADMIN_EMAIL: '  ben@crystalpm.com \n', ADMIN_PASSWORD: 'first-secret' }, quiet);
    expect(users.created[0].email).toBe('ben@crystalpm.com');
  });

  it('warns when nothing is configured and nobody could sign in', async () => {
    const outcome = await ensureAdmin(fakeUsers(), {}, quiet);
    expect(outcome).toBe('skipped');
    expect(quiet.warn).toHaveBeenCalledWith('No users exist and ADMIN_EMAIL/ADMIN_PASSWORD are not set: nobody can sign in');
  });

  it('stays quiet when nothing is configured but users exist', async () => {
    const outcome = await ensureAdmin(fakeUsers(['someone@crystalpm.com']), { ADMIN_EMAIL: 'x@y', ADMIN_PASSWORD: '' }, quiet);
    expect(outcome).toBe('skipped');
    expect(quiet.warn).not.toHaveBeenCalled();
  });
});

describe('syncOptions', () => {
  it('never alters tables in production', () => {
    expect(syncOptions('production')).toEqual({});
  });

  it('alters tables to match the models in development', () => {
    expect(syncOptions('development')).toEqual({ alter: true });
    expect(syncOptions(undefined)).toEqual({ alter: true });
  });
});
```

- [ ] **Step 3: Run the tests to verify they fail**

Run: `cd src/croom-dashboard/backend && npx jest src/__tests__/bootstrap.test.ts`
Expected: FAIL, "Cannot find module '../bootstrap'".

- [ ] **Step 4: Write `bootstrap.ts`**

Create `src/croom-dashboard/backend/src/bootstrap.ts`:

```ts
/**
 * First-run bootstrap (spec 2026-10-05 dashboard on a Pi, section 4.4).
 *
 * A fresh dashboard needs someone who can sign in: the first admin comes from
 * ADMIN_EMAIL and ADMIN_PASSWORD and is created only when that user does not
 * exist yet, so a password changed in the app survives restarts. Production
 * creates missing tables and never alters existing ones; there are no migrations.
 */

import bcrypt from 'bcrypt';
import { logger as defaultLogger } from './services/logger';

export interface UserStore {
  findOne(options: { where: { email: string } }): Promise<unknown | null>;
  count(): Promise<number>;
  create(values: { email: string; passwordHash: string; name: string; role: 'admin' }): Promise<unknown>;
}

export interface AdminEnv {
  ADMIN_EMAIL?: string;
  ADMIN_PASSWORD?: string;
}

type Log = { info(message: string): unknown; warn(message: string): unknown };

export type AdminOutcome = 'created' | 'exists' | 'skipped';

export async function ensureAdmin(users: UserStore, env: AdminEnv, log: Log = defaultLogger): Promise<AdminOutcome> {
  const email = (env.ADMIN_EMAIL || '').trim();
  const password = env.ADMIN_PASSWORD || '';
  if (!email || !password) {
    if ((await users.count()) === 0) {
      log.warn('No users exist and ADMIN_EMAIL/ADMIN_PASSWORD are not set: nobody can sign in');
    }
    return 'skipped';
  }
  if (await users.findOne({ where: { email } })) {
    return 'exists';
  }
  const passwordHash = await bcrypt.hash(password, 12);
  await users.create({ email, passwordHash, name: 'Administrator', role: 'admin' });
  log.info(`Created admin user ${email}`);
  return 'created';
}

export function syncOptions(nodeEnv: string | undefined): { alter?: boolean } {
  return nodeEnv === 'production' ? {} : { alter: true };
}
```

- [ ] **Step 5: Run the tests to verify they pass**

Run: `cd src/croom-dashboard/backend && npx jest src/__tests__/bootstrap.test.ts`
Expected: PASS, 7 tests.

- [ ] **Step 6: Wire it into start-up**

In `src/croom-dashboard/backend/src/models/index.ts` add the import at the top, after the logger import:

```ts
import { syncOptions } from '../bootstrap';
```

and replace the body of `initDatabase` (lines 229-243) with:

```ts
export async function initDatabase(): Promise<void> {
  try {
    await sequelize.authenticate();
    logger.info('Database connection established');

    // No migrations exist: production creates missing tables and never alters them.
    const options = syncOptions(process.env.NODE_ENV);
    await sequelize.sync(options);
    logger.info(options.alter ? 'Database synchronized (tables altered to match the models)' : 'Database tables created where missing');
  } catch (error) {
    logger.error('Database initialization failed:', error);
    throw error;
  }
}
```

In `src/croom-dashboard/backend/src/index.ts` change the models import and add the bootstrap import:

```ts
import { initDatabase, User } from './models';
import { ensureAdmin } from './bootstrap';
```

and after `logger.info('Database initialized');` add:

```ts
  await ensureAdmin(User, process.env);
```

If TypeScript rejects `User` as a `UserStore` (Sequelize's static method types are wide), pass `User as unknown as UserStore` and import the type: `import { ensureAdmin, UserStore } from './bootstrap';`.

- [ ] **Step 7: Build the backend to prove it still compiles**

Run: `cd src/croom-dashboard/backend && npx tsc --noEmit`
Expected: no output, exit 0.

- [ ] **Step 8: Commit**

```bash
git add src/croom-dashboard/backend/jest.config.js src/croom-dashboard/backend/tsconfig.json src/croom-dashboard/backend/src/bootstrap.ts src/croom-dashboard/backend/src/__tests__/bootstrap.test.ts src/croom-dashboard/backend/src/models/index.ts src/croom-dashboard/backend/src/index.ts
git commit -m "feat(dashboard): first admin from the environment and production table sync"
```

---

### Task 2: The register route needs a signed-in admin

**Files:**
- Modify: `src/croom-dashboard/backend/src/routes/auth.ts:60-61`
- Create: `src/croom-dashboard/backend/src/__tests__/http.ts` (test helper, reused by Task 3)
- Create: `src/croom-dashboard/backend/src/__tests__/register.test.ts`

**Interfaces:**
- Produces: test helper `withServer(app, fn)` that listens on an ephemeral port and passes `(baseUrl)` to `fn`.

- [ ] **Step 1: Write the HTTP helper and the failing test**

Create `src/croom-dashboard/backend/src/__tests__/http.ts`:

```ts
import http from 'http';
import { AddressInfo } from 'net';
import { Express } from 'express';

export interface Reply {
  status: number;
  headers: http.IncomingHttpHeaders;
  text: string;
  json(): any;
}

/** One HTTP request with Node's own client, so the tests need no global fetch. */
export function request(url: string, options: { method?: string; headers?: Record<string, string>; body?: string } = {}): Promise<Reply> {
  return new Promise((resolve, reject) => {
    const req = http.request(url, { method: options.method || 'GET', headers: options.headers }, (res) => {
      let text = '';
      res.setEncoding('utf8');
      res.on('data', (chunk) => (text += chunk));
      res.on('end', () => resolve({ status: res.statusCode || 0, headers: res.headers, text, json: () => JSON.parse(text) }));
    });
    req.on('error', reject);
    if (options.body) req.write(options.body);
    req.end();
  });
}

/** Listen on an ephemeral port for the duration of fn, then close. */
export async function withServer(app: Express, fn: (baseUrl: string) => Promise<void>): Promise<void> {
  const server = app.listen(0);
  await new Promise<void>((resolve) => server.once('listening', resolve));
  const { port } = server.address() as AddressInfo;
  try {
    await fn(`http://127.0.0.1:${port}`);
  } finally {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  }
}
```

Create `src/croom-dashboard/backend/src/__tests__/register.test.ts`:

```ts
import express from 'express';
import { authRouter } from '../routes/auth';
import { generateToken } from '../middleware/auth';
import { request, withServer } from './http';

function app() {
  const a = express();
  a.use(express.json());
  a.use('/api/auth', authRouter);
  return a;
}

const body = JSON.stringify({ email: 'new@crystalpm.com', password: 'pw', name: 'New', role: 'admin' });

describe('POST /api/auth/register', () => {
  it('refuses without a token', async () => {
    await withServer(app(), async (base) => {
      const res = await request(`${base}/api/auth/register`, { method: 'POST', headers: { 'content-type': 'application/json' }, body });
      expect(res.status).toBe(401);
    });
  });

  it('refuses a signed-in viewer', async () => {
    const token = generateToken({ id: 'u1', email: 'viewer@crystalpm.com', role: 'viewer' });
    await withServer(app(), async (base) => {
      const res = await request(`${base}/api/auth/register`, {
        method: 'POST',
        headers: { 'content-type': 'application/json', authorization: `Bearer ${token}` },
        body,
      });
      expect(res.status).toBe(403);
    });
  });
});
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `cd src/croom-dashboard/backend && npx jest src/__tests__/register.test.ts`
Expected: FAIL. Without a token the open route reaches `User.findOne` and answers 500 (no database) instead of 401.

- [ ] **Step 3: Lock the route**

In `src/croom-dashboard/backend/src/routes/auth.ts` change the import and the route head:

```ts
import { generateToken, AuthRequest, authMiddleware, requireRole } from '../middleware/auth';
```

```ts
// Register: only a signed-in admin may create accounts.
authRouter.post('/register', authMiddleware, requireRole('admin'), async (req: Request, res: Response) => {
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `cd src/croom-dashboard/backend && npx jest src/__tests__/register.test.ts`
Expected: PASS, 2 tests.

- [ ] **Step 5: Commit**

```bash
git add src/croom-dashboard/backend/src/routes/auth.ts src/croom-dashboard/backend/src/__tests__/http.ts src/croom-dashboard/backend/src/__tests__/register.test.ts
git commit -m "fix(dashboard): creating accounts needs a signed-in admin"
```

---

### Task 3: Serve the web app from the backend, a CSP for plain HTTP, logs on stdout

**Files:**
- Create: `src/croom-dashboard/backend/src/webapp.ts`
- Create: `src/croom-dashboard/backend/src/security.ts`
- Modify: `src/croom-dashboard/backend/src/services/logger.ts:26-30`
- Modify: `src/croom-dashboard/backend/src/index.ts` (helmet options, web app wiring)
- Create: `src/croom-dashboard/backend/src/__tests__/webapp.test.ts`
- Create: `src/croom-dashboard/backend/src/__tests__/security.test.ts`
- Create: `src/croom-dashboard/backend/src/__tests__/logger.test.ts`

**Interfaces:**
- Consumes: `withServer` from Task 2.
- Produces: `serveWebApp(app: Express, dir: string): void`, `webAppDir(env?: NodeJS.ProcessEnv): string | null`, `helmetOptions(): HelmetOptions`.

- [ ] **Step 1: Write the failing tests**

Create `src/croom-dashboard/backend/src/__tests__/webapp.test.ts`:

```ts
import express from 'express';
import { mkdtempSync, mkdirSync, writeFileSync } from 'fs';
import { tmpdir } from 'os';
import path from 'path';
import { serveWebApp, webAppDir } from '../webapp';
import { request, withServer } from './http';

function site(): string {
  const dir = mkdtempSync(path.join(tmpdir(), 'webapp-'));
  writeFileSync(path.join(dir, 'index.html'), '<!doctype html><title>Crystal Meet</title><div id="root"></div>');
  mkdirSync(path.join(dir, 'assets'));
  writeFileSync(path.join(dir, 'assets', 'app.js'), 'console.log("app")');
  return dir;
}

function app(dir: string) {
  const a = express();
  a.get('/health', (_req, res) => res.json({ status: 'healthy' }));
  a.get('/api/devices', (_req, res) => res.json({ devices: [] }));
  serveWebApp(a, dir);
  a.use((_req, res) => res.status(404).json({ error: 'Not found' }));
  return a;
}

const html = { accept: 'text/html,application/xhtml+xml' };
const json = { accept: 'application/json' };

describe('serveWebApp', () => {
  it('serves index.html at the root', async () => {
    await withServer(app(site()), async (base) => {
      const res = await request(base + '/', { headers: html });
      expect(res.status).toBe(200);
      expect(res.headers['content-type']).toContain('text/html');
      expect(res.text).toContain('Crystal Meet');
    });
  });

  it('serves index.html for a browser route so the app can take over', async () => {
    await withServer(app(site()), async (base) => {
      const res = await request(base + '/devices/abc', { headers: html });
      expect(res.status).toBe(200);
      expect(res.text).toContain('id="root"');
    });
  });

  it('serves asset files', async () => {
    await withServer(app(site()), async (base) => {
      const res = await request(base + '/assets/app.js');
      expect(res.status).toBe(200);
      expect(res.headers['content-type']).toContain('javascript');
    });
  });

  it('leaves the API, the health check and unknown API paths alone', async () => {
    await withServer(app(site()), async (base) => {
      expect((await request(base + '/api/devices', { headers: html })).json()).toEqual({ devices: [] });
      expect((await request(base + '/health', { headers: html })).json()).toEqual({ status: 'healthy' });
      const missing = await request(base + '/api/nope', { headers: html });
      expect(missing.status).toBe(404);
      expect(missing.json()).toEqual({ error: 'Not found' });
      const ws = await request(base + '/ws', { headers: html });
      expect(ws.status).toBe(404);
    });
  });

  it('non-HTML requests fall through to the JSON 404', async () => {
    await withServer(app(site()), async (base) => {
      const res = await request(base + '/devices/abc', { headers: json });
      expect(res.status).toBe(404);
      expect(res.json()).toEqual({ error: 'Not found' });
    });
  });
});

describe('webAppDir', () => {
  it('is null when STATIC_DIR is unset or has no index.html', () => {
    expect(webAppDir({})).toBeNull();
    expect(webAppDir({ STATIC_DIR: mkdtempSync(path.join(tmpdir(), 'empty-')) })).toBeNull();
  });

  it('is the directory when it holds an index.html', () => {
    const dir = site();
    expect(webAppDir({ STATIC_DIR: dir })).toBe(dir);
  });
});
```

Create `src/croom-dashboard/backend/src/__tests__/security.test.ts`:

```ts
import express from 'express';
import helmet from 'helmet';
import { helmetOptions } from '../security';
import { request, withServer } from './http';

describe('helmetOptions', () => {
  it('keeps helmet defaults but drops upgrade-insecure-requests for a plain-HTTP site', async () => {
    const app = express();
    app.use(helmet(helmetOptions()));
    app.get('/', (_req, res) => res.send('ok'));
    await withServer(app, async (base) => {
      const res = await request(base + '/');
      const csp = String(res.headers['content-security-policy'] || '');
      expect(csp).toContain("default-src 'self'");
      expect(csp).not.toContain('upgrade-insecure-requests');
      expect(res.headers['x-content-type-options']).toBe('nosniff');
    });
  });
});
```

Create `src/croom-dashboard/backend/src/__tests__/logger.test.ts`:

```ts
import { mkdtempSync } from 'fs';
import { tmpdir } from 'os';
import path from 'path';

function loadLogger(env: Record<string, string | undefined>) {
  const saved = { ...process.env };
  for (const [key, value] of Object.entries(env)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value;
  }
  let logger: any;
  jest.isolateModules(() => {
    logger = require('../services/logger').logger;
  });
  process.env = saved;
  return logger;
}

describe('logger transports', () => {
  it('writes only to the console when LOG_DIR is unset, even in production', () => {
    const logger = loadLogger({ NODE_ENV: 'production', LOG_DIR: undefined });
    expect(logger.transports).toHaveLength(1);
    expect(logger.transports[0].constructor.name).toBe('Console');
  });

  it('adds the two files under LOG_DIR when it is set', () => {
    const dir = mkdtempSync(path.join(tmpdir(), 'logs-'));
    const logger = loadLogger({ LOG_DIR: dir });
    const files = logger.transports.filter((t: any) => t.constructor.name === 'File').map((t: any) => t.filename);
    expect(files.sort()).toEqual(['combined.log', 'error.log']);
    expect(logger.transports.filter((t: any) => t.constructor.name === 'File').every((t: any) => t.dirname === dir)).toBe(true);
  });
});
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `cd src/croom-dashboard/backend && npx jest src/__tests__/webapp.test.ts src/__tests__/security.test.ts src/__tests__/logger.test.ts`
Expected: FAIL. `webapp` and `security` cannot be found; the logger test's production case finds three transports.

- [ ] **Step 3: Write `webapp.ts` and `security.ts`, fix the logger**

Create `src/croom-dashboard/backend/src/webapp.ts`:

```ts
/**
 * Serve the built web app from the same origin as the API (spec 2026-10-05,
 * section 4.4). Browser routes fall back to index.html so the app's router can
 * take over; API, WebSocket and health paths, and anything that does not want
 * HTML, fall through to the JSON 404.
 */

import { existsSync } from 'fs';
import path from 'path';
import express, { Express, NextFunction, Request, Response } from 'express';

const RESERVED = ['/api', '/ws', '/health'];

function isReserved(requestPath: string): boolean {
  return RESERVED.some((prefix) => requestPath === prefix || requestPath.startsWith(prefix + '/'));
}

export function serveWebApp(app: Express, dir: string): void {
  const index = path.join(dir, 'index.html');
  app.use(express.static(dir, { index: 'index.html' }));
  app.get('*', (req: Request, res: Response, next: NextFunction) => {
    if (isReserved(req.path) || !req.accepts('html')) {
      next();
      return;
    }
    res.sendFile(index);
  });
}

/** The directory to serve, or null when STATIC_DIR is unset or holds no index.html. */
export function webAppDir(env: NodeJS.ProcessEnv = process.env): string | null {
  const dir = env.STATIC_DIR;
  return dir && existsSync(path.join(dir, 'index.html')) ? dir : null;
}
```

Create `src/croom-dashboard/backend/src/security.ts`:

```ts
/**
 * Helmet options for a dashboard served over plain HTTP on the office network.
 * Helmet's default content security policy carries upgrade-insecure-requests,
 * which makes browsers fetch the page's own assets over HTTPS and show a blank
 * page; everything else stays at helmet's defaults.
 */

import helmet, { HelmetOptions } from 'helmet';

export function helmetOptions(): HelmetOptions {
  const directives = {
    ...helmet.contentSecurityPolicy.getDefaultDirectives(),
    'upgrade-insecure-requests': null,
  };
  return { contentSecurityPolicy: { directives } };
}
```

If TypeScript rejects the spread with `null`, declare it as `const directives: Record<string, Iterable<string> | null> = { ... }`.

In `src/croom-dashboard/backend/src/services/logger.ts` add `import path from 'path';` at the top and replace the block starting `// Add file transport in production` with:

```ts
// Log files only when asked for; under Docker everything stays on stdout.
const logDir = process.env.LOG_DIR;
if (logDir) {
  logger.add(new winston.transports.File({ filename: path.join(logDir, 'error.log'), level: 'error' }));
  logger.add(new winston.transports.File({ filename: path.join(logDir, 'combined.log') }));
}
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `cd src/croom-dashboard/backend && npx jest src/__tests__/webapp.test.ts src/__tests__/security.test.ts src/__tests__/logger.test.ts`
Expected: PASS, 10 tests. If the `File` transport reports `filename` as the full path rather than the base name, assert on `path.basename(t.filename)` instead.

- [ ] **Step 5: Wire both into `index.ts`**

In `src/croom-dashboard/backend/src/index.ts` add the imports:

```ts
import { serveWebApp, webAppDir } from './webapp';
import { helmetOptions } from './security';
```

change `app.use(helmet());` to `app.use(helmet(helmetOptions()));`, and between the API routes and `// Error handling` add:

```ts
  // The built web app, when the image or the environment provides one.
  const webApp = webAppDir();
  if (webApp) {
    serveWebApp(app, webApp);
    logger.info(`Serving the web app from ${webApp}`);
  }
```

- [ ] **Step 6: Type-check and run every backend test**

Run: `cd src/croom-dashboard/backend && npx tsc --noEmit && npx jest`
Expected: exit 0; all suites pass (bootstrap, register, webapp, security, logger).

- [ ] **Step 7: Commit**

```bash
git add src/croom-dashboard/backend/src/webapp.ts src/croom-dashboard/backend/src/security.ts src/croom-dashboard/backend/src/services/logger.ts src/croom-dashboard/backend/src/index.ts src/croom-dashboard/backend/src/__tests__/webapp.test.ts src/croom-dashboard/backend/src/__tests__/security.test.ts src/croom-dashboard/backend/src/__tests__/logger.test.ts
git commit -m "feat(dashboard): serve the web app from the backend with a plain-HTTP CSP; logs on stdout"
```

---

### Task 4: Change-password form on Settings; drop the sign-in hint

**Files:**
- Modify: `src/croom-dashboard/frontend/src/services/api.ts:19-29` (`authApi`)
- Modify: `src/croom-dashboard/frontend/src/pages/Settings.tsx`
- Modify: `src/croom-dashboard/frontend/src/pages/Login.tsx:88-90` (remove the "Default: admin@croom.local / admin" paragraph)
- Modify: `src/croom-dashboard/frontend/package.json` (`build` script)

The frontend has no test runner; this task's check is the Vite build and the browser acceptance in Task 10.

- [ ] **Step 1: Add the API call**

In `src/croom-dashboard/frontend/src/services/api.ts` extend `authApi`:

```ts
export const authApi = {
  login: async (email: string, password: string) => {
    const { data } = await api.post('/auth/login', { email, password });
    return data;
  },
  me: async () => {
    const { data } = await api.get('/auth/me');
    return data;
  },
  changePassword: async (currentPassword: string, newPassword: string) => {
    const { data } = await api.post('/auth/change-password', { currentPassword, newPassword });
    return data;
  },
};
```

- [ ] **Step 2: Replace the dead button with a form**

In `src/croom-dashboard/frontend/src/pages/Settings.tsx` change the imports to:

```tsx
import { FormEvent, useState } from 'react';
import { useAuthStore } from '../store/auth';
import { authApi } from '../services/api';
```

add this component above `export default function Settings()`:

```tsx
const inputClass = 'w-full px-4 py-3 bg-gray-700 border border-gray-600 rounded-lg text-white focus:outline-none focus:border-blue-500';

function ChangePasswordForm() {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [again, setAgain] = useState('');
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  const submit = async (event: FormEvent) => {
    event.preventDefault();
    setMessage(null);
    setError(null);
    if (next.length < 8) {
      setError('Use at least 8 characters.');
      return;
    }
    if (next !== again) {
      setError('The new passwords do not match.');
      return;
    }
    setSaving(true);
    try {
      await authApi.changePassword(current, next);
      setMessage('Password changed.');
      setCurrent('');
      setNext('');
      setAgain('');
    } catch (err: any) {
      setError(err?.response?.data?.error || 'Could not change the password.');
    } finally {
      setSaving(false);
    }
  };

  return (
    <form onSubmit={submit} className="mt-6 space-y-4 max-w-md" aria-label="Change password">
      <h3 className="font-medium">Change password</h3>
      {error && <div className="bg-red-900/50 border border-red-500 text-red-200 px-4 py-3 rounded">{error}</div>}
      {message && <div className="bg-green-900/50 border border-green-500 text-green-200 px-4 py-3 rounded">{message}</div>}
      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">Current password</label>
        <input type="password" className={inputClass} value={current} onChange={(e) => setCurrent(e.target.value)} autoComplete="current-password" required />
      </div>
      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">New password</label>
        <input type="password" className={inputClass} value={next} onChange={(e) => setNext(e.target.value)} autoComplete="new-password" required />
      </div>
      <div>
        <label className="block text-sm font-medium text-gray-300 mb-2">New password again</label>
        <input type="password" className={inputClass} value={again} onChange={(e) => setAgain(e.target.value)} autoComplete="new-password" required />
      </div>
      <button type="submit" disabled={saving} className="px-4 py-2 bg-blue-600 hover:bg-blue-700 text-white rounded-lg transition-colors disabled:opacity-50">
        {saving ? 'Saving…' : 'Change password'}
      </button>
    </form>
  );
}
```

and replace the `<button ...>Change Password</button>` inside the Account card with `<ChangePasswordForm />`.

- [ ] **Step 3: Remove the hint on the sign-in page and fix the build script**

In `src/croom-dashboard/frontend/src/pages/Login.tsx` delete:

```tsx
          <p className="mt-6 text-center text-sm text-gray-500">
            Default: admin@croom.local / admin
          </p>
```

In `src/croom-dashboard/frontend/package.json` change `"build": "tsc && vite build"` to `"build": "vite build"` (there is no `tsconfig.json` for `tsc` to read).

- [ ] **Step 4: Build**

Run: `cd src/croom-dashboard/frontend && npm run build`
Expected: "✓ built in" and `dist/index.html` present. `dist/` is gitignored.

- [ ] **Step 5: Look at it once in the dev server**

Run: with the dev dashboard already running on this machine (backend :3001, frontend :3000), open `http://localhost:3000/settings` in the venv's Chromium and sign in as the dev admin:

```bash
.venv/bin/python - <<'EOF'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    b = p.chromium.launch(); page = b.new_page()
    page.goto("http://localhost:3000/login"); page.fill("input[type=email]", "admin@croom.local")
    page.fill("input[type=password]", open("/home/cpm_ssh/.config/croom/dashboard-admin-password.txt").read().strip())
    page.click("button[type=submit]"); page.wait_for_url("**/")
    page.goto("http://localhost:3000/settings"); page.wait_for_selector("form[aria-label='Change password']")
    page.fill("input[autocomplete=current-password]", "wrong"); page.fill("input[autocomplete=new-password] >> nth=0", "longenough1"); page.fill("input[autocomplete=new-password] >> nth=1", "longenough1")
    page.click("text=Change password >> nth=1"); page.wait_for_selector("text=Invalid current password")
    print("form works; wrong current password is reported"); b.close()
EOF
```

Expected: the printed line. If the dev dashboard is not running, skip this step; Task 10 covers it against the built image.

- [ ] **Step 6: Commit**

```bash
git add src/croom-dashboard/frontend/src/services/api.ts src/croom-dashboard/frontend/src/pages/Settings.tsx src/croom-dashboard/frontend/src/pages/Login.tsx src/croom-dashboard/frontend/package.json
git commit -m "feat(dashboard): change-password form on Settings; no default credentials hint"
```

---

### Task 5: Compose file, Dockerfile, env example, dockerignore, operator notes

**Files:**
- Create: `deploy/dashboard/docker-compose.yml`
- Create: `deploy/dashboard/Dockerfile`
- Create: `deploy/dashboard/.env.example`
- Create: `deploy/dashboard/README.md`
- Create: `.dockerignore` (repo root)
- Test: `tests/unit/deploy/test_dashboard_compose.py`

**Interfaces:**
- Produces: the compose file at `deploy/dashboard/docker-compose.yml` that Task 6's backup script and Task 7's installer run with `docker compose -f`; env keys `DB_NAME`, `DB_USER`, `DB_PASSWORD`, `JWT_SECRET`, `ADMIN_EMAIL`, `ADMIN_PASSWORD`, `BASE_URL`, `WS_URL`, `LOG_LEVEL`, optional `DASHBOARD_PORT` (3001) and `DASHBOARD_HTTP_PORT` (80).

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/deploy/test_dashboard_compose.py`:

```python
"""
The dashboard's compose file, image and env example are what the installer and
the fourth Pi run; check their shape here so a typo surfaces before a build.
"""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy" / "dashboard"
ENV_KEYS = ["DB_NAME", "DB_USER", "DB_PASSWORD", "JWT_SECRET", "ADMIN_EMAIL", "ADMIN_PASSWORD", "BASE_URL", "WS_URL", "LOG_LEVEL"]


def compose():
    return yaml.safe_load((DEPLOY / "docker-compose.yml").read_text(encoding="utf-8"))


def test_two_services_and_a_named_volume():
    data = compose()
    assert data["name"] == "croom-dashboard"
    assert set(data["services"]) == {"db", "dashboard"}
    assert "pgdata" in data["volumes"]
    assert data["services"]["db"]["volumes"] == ["pgdata:/var/lib/postgresql/data"]


def test_database_is_private_and_both_restart():
    data = compose()
    assert "ports" not in data["services"]["db"]
    for service in data["services"].values():
        assert service["restart"] == "unless-stopped"


def test_dashboard_waits_for_a_healthy_database_and_serves_the_web_app():
    dashboard = compose()["services"]["dashboard"]
    assert dashboard["depends_on"] == {"db": {"condition": "service_healthy"}}
    assert dashboard["environment"]["STATIC_DIR"] == "/app/public"
    assert dashboard["environment"]["DB_HOST"] == "db"
    assert dashboard["environment"]["NODE_ENV"] == "production"
    assert dashboard["env_file"] == ".env"
    assert dashboard["ports"] == ["${DASHBOARD_PORT:-3001}:3001", "${DASHBOARD_HTTP_PORT:-80}:3001"]
    assert dashboard["build"] == {"context": "../..", "dockerfile": "deploy/dashboard/Dockerfile"}
    assert dashboard["logging"]["options"]["max-size"] == "10m"
    assert "healthcheck" in dashboard and "healthcheck" in compose()["services"]["db"]


def test_env_example_has_every_key_and_no_secret():
    lines = [l for l in (DEPLOY / ".env.example").read_text(encoding="utf-8").splitlines() if l and not l.startswith("#")]
    keys = {l.split("=", 1)[0]: l.split("=", 1)[1] for l in lines}
    for key in ENV_KEYS:
        assert key in keys, key
    for key in ("DB_PASSWORD", "JWT_SECRET", "ADMIN_PASSWORD", "ADMIN_EMAIL"):
        assert keys[key].startswith("REPLACE_WITH_"), key
    assert keys["BASE_URL"] == "http://REPLACE_WITH_DASHBOARD_ADDRESS:3001"
    assert keys["WS_URL"] == "ws://REPLACE_WITH_DASHBOARD_ADDRESS:3001"


def test_dockerfile_builds_both_halves_and_runs_as_node():
    text = (DEPLOY / "Dockerfile").read_text(encoding="utf-8")
    assert text.count("FROM node:20-bookworm-slim") == 3
    assert "AS frontend-build" in text and "AS backend-build" in text
    assert "vite build" in text and "npm run build" in text and "npm prune --omit=dev" in text
    assert "COPY --from=frontend-build /src/frontend/dist ./public" in text
    assert "USER node" in text and 'CMD ["node", "dist/index.js"]' in text
    assert "python3 make g++" in text  # bcrypt builds from source when no arm64 binary is available


def test_dockerignore_keeps_the_context_small():
    text = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert text[0] == "*"
    assert "!src/croom-dashboard/backend" in text and "!src/croom-dashboard/frontend" in text
    for excluded in ("src/croom-dashboard/backend/node_modules", "src/croom-dashboard/frontend/node_modules",
                     "src/croom-dashboard/backend/dist", "src/croom-dashboard/frontend/dist", "src/croom-dashboard/backend/.env"):
        assert excluded in text, excluded


def test_real_env_file_is_ignored_by_git():
    result = subprocess.run(["git", "check-ignore", "-q", "deploy/dashboard/.env"], cwd=REPO)
    assert result.returncode == 0


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not installed")
def test_compose_config_resolves(tmp_path):
    if subprocess.run(["docker", "compose", "version"], capture_output=True).returncode != 0:
        pytest.skip("docker compose plugin not installed")
    # A copy in tmp_path with a full .env beside it: the real .env never exists in the repo.
    shutil.copy(DEPLOY / "docker-compose.yml", tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text("DB_NAME=croom\nDB_USER=croom\nDB_PASSWORD=x\nJWT_SECRET=y\nADMIN_EMAIL=a@b.c\n"
                                   "ADMIN_PASSWORD=z\nBASE_URL=http://h:3001\nWS_URL=ws://h:3001\nLOG_LEVEL=info\n")
    result = subprocess.run(["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "config"],
                            capture_output=True, text=True, cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "croom-dashboard:local" in result.stdout
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/deploy/test_dashboard_compose.py`
Expected: FAIL, missing `deploy/dashboard/docker-compose.yml`.

- [ ] **Step 3: Write the compose file**

Create `deploy/dashboard/docker-compose.yml`:

```yaml
# Crystal Meet dashboard: Postgres plus the dashboard image built from this repo.
# Secrets come from .env next to this file (written by installer/install-dashboard.sh).
name: croom-dashboard

services:
  db:
    image: postgres:16-alpine
    restart: unless-stopped
    environment:
      POSTGRES_DB: ${DB_NAME:-croom}
      POSTGRES_USER: ${DB_USER:-croom}
      POSTGRES_PASSWORD: ${DB_PASSWORD:?set DB_PASSWORD in .env}
    volumes:
      - pgdata:/var/lib/postgresql/data
    healthcheck:
      test: ["CMD-SHELL", "pg_isready -U ${DB_USER:-croom} -d ${DB_NAME:-croom}"]
      interval: 10s
      timeout: 5s
      retries: 10

  dashboard:
    build:
      context: ../..
      dockerfile: deploy/dashboard/Dockerfile
    image: croom-dashboard:local
    restart: unless-stopped
    env_file: .env
    environment:
      NODE_ENV: production
      HOST: 0.0.0.0
      PORT: "3001"
      DB_HOST: db
      DB_PORT: "5432"
      STATIC_DIR: /app/public
    ports:
      - "${DASHBOARD_PORT:-3001}:3001"
      - "${DASHBOARD_HTTP_PORT:-80}:3001"
    depends_on:
      db:
        condition: service_healthy
    healthcheck:
      test: ["CMD", "node", "-e", "fetch('http://127.0.0.1:3001/health').then(r => process.exit(r.ok ? 0 : 1)).catch(() => process.exit(1))"]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 30s
    logging:
      driver: json-file
      options:
        max-size: "10m"
        max-file: "3"

volumes:
  pgdata:
```

- [ ] **Step 4: Write the Dockerfile and the dockerignore**

Create `deploy/dashboard/Dockerfile`:

```dockerfile
# syntax=docker/dockerfile:1
# Crystal Meet dashboard image: the web app and the backend, built from the repo root.

FROM node:20-bookworm-slim AS frontend-build
WORKDIR /src/frontend
COPY src/croom-dashboard/frontend/package.json src/croom-dashboard/frontend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/croom-dashboard/frontend/ ./
RUN npx vite build

FROM node:20-bookworm-slim AS backend-build
# bcrypt ships prebuilt binaries for arm64 and x86_64; the toolchain is the fallback.
RUN apt-get update && apt-get install -y --no-install-recommends python3 make g++ && rm -rf /var/lib/apt/lists/*
WORKDIR /src/backend
COPY src/croom-dashboard/backend/package.json src/croom-dashboard/backend/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY src/croom-dashboard/backend/ ./
RUN npm run build && npm prune --omit=dev

FROM node:20-bookworm-slim
ENV NODE_ENV=production
WORKDIR /app
COPY --from=backend-build /src/backend/dist ./dist
COPY --from=backend-build /src/backend/node_modules ./node_modules
COPY --from=backend-build /src/backend/package.json ./package.json
COPY --from=frontend-build /src/frontend/dist ./public
USER node
EXPOSE 3001
CMD ["node", "dist/index.js"]
```

Create `.dockerignore` at the repo root:

```
*
!src/croom-dashboard/backend
!src/croom-dashboard/frontend
src/croom-dashboard/backend/node_modules
src/croom-dashboard/backend/dist
src/croom-dashboard/backend/.env
src/croom-dashboard/frontend/node_modules
src/croom-dashboard/frontend/dist
```

- [ ] **Step 5: Write the env example and the operator notes**

Create `deploy/dashboard/.env.example`:

```
# Crystal Meet dashboard settings. installer/install-dashboard.sh writes the real
# .env next to docker-compose.yml with generated secrets; never commit that file.
DB_NAME=croom
DB_USER=croom
DB_PASSWORD=REPLACE_WITH_A_LONG_RANDOM_STRING
JWT_SECRET=REPLACE_WITH_ANOTHER_LONG_RANDOM_STRING
ADMIN_EMAIL=REPLACE_WITH_THE_FIRST_ADMIN_EMAIL
ADMIN_PASSWORD=REPLACE_WITH_THE_FIRST_ADMIN_PASSWORD
BASE_URL=http://REPLACE_WITH_DASHBOARD_ADDRESS:3001
WS_URL=ws://REPLACE_WITH_DASHBOARD_ADDRESS:3001
LOG_LEVEL=info
# Optional: host ports (defaults 3001 and 80), useful on a development machine.
# DASHBOARD_PORT=3101
# DASHBOARD_HTTP_PORT=8081
```

Create `deploy/dashboard/README.md`:

```markdown
# Crystal Meet dashboard under Docker Compose

What runs on the dashboard host (a fourth Raspberry Pi, or any 64-bit Docker
host): `docker-compose.yml` starts Postgres 16 and the dashboard image built
from this repo by `Dockerfile`. The dashboard serves the web app, the API and
the devices' WebSocket on port 3001; port 80 is published to the same port so
people can open it without a port number.

`installer/install-dashboard.sh` does the install and every later update:

    sudo bash installer/install-dashboard.sh --admin-email you@crystalpm.com

It writes `.env` next to the compose file on the first run (mode 600; the keys
are listed in `.env.example`), never changes it afterwards, builds and starts
the stack, and installs a nightly backup timer. The first admin comes from
`ADMIN_EMAIL` and `ADMIN_PASSWORD`; the account is created only when it does
not exist, so a password changed on the Settings page sticks.

Day to day, from `/opt/croom-dashboard/deploy/dashboard`:

    docker compose ps                    # both services should be Up (healthy)
    docker compose logs -f dashboard     # the backend's log
    sudo bash ../../installer/install-dashboard.sh   # update: pull, rebuild, restart

Backups: `croom-dashboard-backup.timer` runs `backup.sh` nightly at 02:30 and
keeps 14 days of `croom-dashboard-YYYY-MM-DD.sql.gz` in
`/var/backups/croom-dashboard`. Copy a file off the Pi with `scp`. Restore one
onto a running stack (it drops and recreates the tables):

    docker compose stop dashboard
    gunzip -c /var/backups/croom-dashboard/croom-dashboard-2026-10-05.sql.gz | docker compose exec -T db psql -U croom -d croom
    docker compose start dashboard

Forgotten admin password: delete the user, and the next start recreates it from
`.env`:

    docker compose exec db psql -U croom -d croom -c "DELETE FROM users WHERE email = 'you@crystalpm.com';"
    docker compose restart dashboard

There are no database migrations: the backend creates missing tables at start
and never alters existing ones. Nothing here holds a real secret; `.env` is
gitignored.
```

- [ ] **Step 6: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/deploy/test_dashboard_compose.py`
Expected: PASS, 8 tests (the last one runs because Docker is installed here).

- [ ] **Step 7: Build the image once on this machine**

Run: `docker build -f deploy/dashboard/Dockerfile -t croom-dashboard:local .`
Expected: the three stages complete and the image exists (`docker image ls croom-dashboard`). Takes a few minutes the first time.

- [ ] **Step 8: Commit**

```bash
git add deploy/dashboard/docker-compose.yml deploy/dashboard/Dockerfile deploy/dashboard/.env.example deploy/dashboard/README.md .dockerignore tests/unit/deploy/test_dashboard_compose.py
git commit -m "feat(deploy): Docker Compose packaging for the Crystal Meet dashboard"
```

---

### Task 6: Nightly backup script and timer units

**Files:**
- Create: `deploy/dashboard/backup.sh`
- Create: `deploy/dashboard/croom-dashboard-backup.service`
- Create: `deploy/dashboard/croom-dashboard-backup.timer`
- Test: `tests/unit/deploy/test_dashboard_backup.py`

**Interfaces:**
- Produces: `backup.sh DEPLOY_DIR BACKUP_DIR [KEEP_DAYS]`, installed by Task 7 as `/usr/local/bin/croom-dashboard-backup`; the service unit carries `__BIN_DIR__`, `__INSTALL_DIR__` and `__BACKUP_DIR__` placeholders that Task 7 fills with `sed`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/deploy/test_dashboard_backup.py`:

```python
"""
The backup script dumps the dashboard database through docker compose, keeps
14 days and never leaves a half-written file; the units run it nightly.
"""

import os
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
DEPLOY = REPO / "deploy" / "dashboard"
SCRIPT = DEPLOY / "backup.sh"


def fake_docker(tmp_path, body):
    """A docker stand-in on PATH; body is the shell that runs for `docker compose ... exec ...`."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "docker").write_text("#!/bin/bash\necho \"$@\" > " + str(tmp_path / "docker-args") + "\n" + body + "\n")
    (bin_dir / "docker").chmod(0o755)
    return bin_dir


def run(tmp_path, bin_dir, *args):
    deploy = tmp_path / "deploy"
    deploy.mkdir(exist_ok=True)
    (deploy / ".env").write_text("DB_NAME=croom\nDB_USER=croom\nDB_PASSWORD=secret\n")
    env = dict(os.environ, PATH=f"{bin_dir}:{os.environ['PATH']}")
    return subprocess.run(["bash", str(SCRIPT), str(deploy), str(tmp_path / "backups"), *args], capture_output=True, text=True, env=env)


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_backup_writes_a_dated_private_gzip_and_prunes_old_files(tmp_path):
    bin_dir = fake_docker(tmp_path, 'echo "-- PostgreSQL database dump"')
    backups = tmp_path / "backups"
    backups.mkdir()
    old = backups / "croom-dashboard-2000-01-01.sql.gz"
    old.write_bytes(b"old")
    os.utime(old, (time.time() - 20 * 86400, time.time() - 20 * 86400))
    recent = backups / "croom-dashboard-2026-10-04.sql.gz"
    recent.write_bytes(b"recent")
    result = run(tmp_path, bin_dir)
    assert result.returncode == 0, result.stderr
    today = time.strftime("%Y-%m-%d")
    out = backups / f"croom-dashboard-{today}.sql.gz"
    assert out.is_file() and oct(out.stat().st_mode & 0o777) == "0o600"
    assert subprocess.run(["gunzip", "-c", str(out)], capture_output=True, text=True).stdout.startswith("-- PostgreSQL")
    assert not old.exists() and recent.exists()
    args = (tmp_path / "docker-args").read_text()
    assert "compose -f" in args and "exec -T db pg_dump --clean --if-exists -U croom croom" in args
    assert oct(backups.stat().st_mode & 0o777) == "0o700"


def test_backup_leaves_no_partial_file_when_the_dump_fails(tmp_path):
    bin_dir = fake_docker(tmp_path, 'echo "half a dump"; exit 1')
    result = run(tmp_path, bin_dir)
    assert result.returncode != 0
    assert list((tmp_path / "backups").glob("*.sql.gz")) == []


def test_units_run_nightly_and_catch_up_after_downtime():
    timer = (DEPLOY / "croom-dashboard-backup.timer").read_text(encoding="utf-8")
    assert "OnCalendar=*-*-* 02:30:00" in timer and "Persistent=true" in timer and "WantedBy=timers.target" in timer
    service = (DEPLOY / "croom-dashboard-backup.service").read_text(encoding="utf-8")
    assert "Type=oneshot" in service
    assert "ExecStart=__BIN_DIR__/croom-dashboard-backup __INSTALL_DIR__/deploy/dashboard __BACKUP_DIR__" in service
    assert "After=docker.service" in service
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/deploy/test_dashboard_backup.py`
Expected: FAIL, `backup.sh` missing.

- [ ] **Step 3: Write the script and the units**

Create `deploy/dashboard/backup.sh`:

```bash
#!/bin/bash
#
# Nightly dump of the Crystal Meet dashboard database.
# Usage: backup.sh DEPLOY_DIR BACKUP_DIR [KEEP_DAYS]
#   DEPLOY_DIR holds docker-compose.yml and .env; BACKUP_DIR receives
#   croom-dashboard-YYYY-MM-DD.sql.gz (mode 600); older files are deleted.
#
set -euo pipefail

DEPLOY_DIR="${1:?usage: backup.sh DEPLOY_DIR BACKUP_DIR [KEEP_DAYS]}"
BACKUP_DIR="${2:?usage: backup.sh DEPLOY_DIR BACKUP_DIR [KEEP_DAYS]}"
KEEP_DAYS="${3:-14}"

set -a
# shellcheck disable=SC1091
. "$DEPLOY_DIR/.env"
set +a

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"
umask 077

OUT="$BACKUP_DIR/croom-dashboard-$(date +%F).sql.gz"
PART="$OUT.part"
trap 'rm -f "$PART"' EXIT

docker compose -f "$DEPLOY_DIR/docker-compose.yml" exec -T db \
    pg_dump --clean --if-exists -U "${DB_USER:-croom}" "${DB_NAME:-croom}" | gzip > "$PART"
mv "$PART" "$OUT"

find "$BACKUP_DIR" -name 'croom-dashboard-*.sql.gz' -mtime +"$KEEP_DAYS" -delete
echo "wrote $OUT"
```

Create `deploy/dashboard/croom-dashboard-backup.service`:

```ini
[Unit]
Description=Nightly backup of the Crystal Meet dashboard database
After=docker.service
Requires=docker.service

[Service]
Type=oneshot
ExecStart=__BIN_DIR__/croom-dashboard-backup __INSTALL_DIR__/deploy/dashboard __BACKUP_DIR__
```

Create `deploy/dashboard/croom-dashboard-backup.timer`:

```ini
[Unit]
Description=Nightly backup of the Crystal Meet dashboard database

[Timer]
OnCalendar=*-*-* 02:30:00
Persistent=true

[Install]
WantedBy=timers.target
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/deploy/test_dashboard_backup.py`
Expected: PASS, 4 tests.

- [ ] **Step 5: Commit**

```bash
git add deploy/dashboard/backup.sh deploy/dashboard/croom-dashboard-backup.service deploy/dashboard/croom-dashboard-backup.timer tests/unit/deploy/test_dashboard_backup.py
git commit -m "feat(deploy): nightly dashboard database backup with a systemd timer"
```

---

### Task 7: The installer

**Files:**
- Create: `installer/install-dashboard.sh`
- Test: `tests/unit/installer/test_install_dashboard_script.py`

**Interfaces:**
- Consumes: `deploy/dashboard/docker-compose.yml`, `backup.sh` and the two units from Tasks 5 and 6.
- Produces: `sudo bash installer/install-dashboard.sh --admin-email EMAIL [--address HOST]`; functions `check_root`, `check_platform`, `install_docker`, `fetch_source`, `write_env`, `start_stack`, `write_backup_units`, `enable_backup_timer`, `print_completion`; variables `INSTALL_DIR`, `BACKUP_DIR`, `SYSTEMD_DIR`, `BIN_DIR`, `MEMORY_KB`, `CROOM_REPO`, `CROOM_BRANCH`, `ADMIN_EMAIL`, `ADDRESS`, `FIRST_RUN`.

- [ ] **Step 1: Write the failing tests**

Create `tests/unit/installer/test_install_dashboard_script.py`:

```python
"""
Tests for installer/install-dashboard.sh that need neither root nor Docker:
argument handling, the platform checks, the env file, the units and the
completion message, with the script sourced.
"""

import os
import re
import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
SCRIPT = REPO / "installer" / "install-dashboard.sh"


def run_bash(snippet, env=None):
    merged = dict(os.environ)
    merged.update(env or {})
    return subprocess.run(["bash", "-c", snippet], capture_output=True, text=True, env=merged, cwd=REPO)


def env_values(path):
    return dict(line.split("=", 1) for line in path.read_text().splitlines() if line and not line.startswith("#"))


def test_script_parses():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_help_names_both_options():
    result = run_bash(f"bash {SCRIPT} --help")
    assert result.returncode == 0
    assert "--admin-email EMAIL" in result.stdout and "--address HOST" in result.stdout


def test_admin_email_must_look_like_an_address():
    result = run_bash(f"bash {SCRIPT} --admin-email nope")
    assert result.returncode == 1
    assert "email" in (result.stdout + result.stderr).lower()


def test_unknown_option_is_refused():
    assert run_bash(f"bash {SCRIPT} --bogus").returncode == 1


def test_first_install_without_admin_email_is_refused(tmp_path):
    result = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; write_env")
    assert result.returncode == 1
    assert "--admin-email" in result.stdout + result.stderr
    assert not (tmp_path / "deploy" / "dashboard" / ".env").exists()


def test_write_env_generates_distinct_hex_secrets_with_mode_600(tmp_path):
    result = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; ADMIN_EMAIL=ben@crystalpm.com; ADDRESS=10.0.0.50; write_env")
    assert result.returncode == 0, result.stderr
    env_file = tmp_path / "deploy" / "dashboard" / ".env"
    assert oct(env_file.stat().st_mode & 0o777) == "0o600"
    values = env_values(env_file)
    assert values["DB_NAME"] == "croom" and values["DB_USER"] == "croom"
    assert re.fullmatch(r"[0-9a-f]{32}", values["DB_PASSWORD"])
    assert re.fullmatch(r"[0-9a-f]{64}", values["JWT_SECRET"])
    assert re.fullmatch(r"[0-9a-f]{16}", values["ADMIN_PASSWORD"])
    assert len({values["DB_PASSWORD"], values["JWT_SECRET"], values["ADMIN_PASSWORD"]}) == 3
    assert values["ADMIN_EMAIL"] == "ben@crystalpm.com"
    assert values["BASE_URL"] == "http://10.0.0.50:3001" and values["WS_URL"] == "ws://10.0.0.50:3001"
    assert values["LOG_LEVEL"] == "info"


def test_second_run_keeps_the_env_file(tmp_path):
    env_file = tmp_path / "deploy" / "dashboard" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("DB_PASSWORD=keep-me\nADMIN_EMAIL=old@crystalpm.com\n")
    result = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; ADMIN_EMAIL=new@crystalpm.com; write_env")
    assert result.returncode == 0, result.stderr
    assert env_file.read_text() == "DB_PASSWORD=keep-me\nADMIN_EMAIL=old@crystalpm.com\n"
    assert "Keeping" in result.stdout and "ignored" in result.stdout


def test_platform_refuses_32_bit():
    result = run_bash(f"source {SCRIPT}; uname() {{ echo armv7l; }}; check_platform")
    assert result.returncode == 1
    assert "64-bit" in result.stdout + result.stderr


def test_platform_refuses_less_than_2gb_and_warns_below_4gb():
    result = run_bash(f"source {SCRIPT}; check_platform", env={"MEMORY_KB": "1000000"})
    assert result.returncode == 1 and "2 GB" in result.stdout + result.stderr
    result = run_bash(f"source {SCRIPT}; check_platform", env={"MEMORY_KB": "2000000"})
    assert result.returncode == 0 and "4 GB" in result.stdout
    result = run_bash(f"source {SCRIPT}; check_platform", env={"MEMORY_KB": "8000000"})
    assert result.returncode == 0 and "4 GB" not in result.stdout


def test_backup_units_are_written_with_the_real_paths(tmp_path):
    result = run_bash(
        f"source {SCRIPT}; INSTALL_DIR={REPO}; SYSTEMD_DIR={tmp_path / 'units'}; BIN_DIR={tmp_path / 'bin'}; "
        f"BACKUP_DIR={tmp_path / 'backups'}; mkdir -p {tmp_path / 'units'} {tmp_path / 'bin'}; write_backup_units"
    )
    assert result.returncode == 0, result.stderr
    service = (tmp_path / "units" / "croom-dashboard-backup.service").read_text()
    assert f"ExecStart={tmp_path / 'bin'}/croom-dashboard-backup {REPO}/deploy/dashboard {tmp_path / 'backups'}" in service
    assert "__" not in service
    timer = (tmp_path / "units" / "croom-dashboard-backup.timer").read_text()
    assert "OnCalendar=*-*-* 02:30:00" in timer
    script = tmp_path / "bin" / "croom-dashboard-backup"
    assert script.is_file() and os.access(script, os.X_OK)
    assert oct((tmp_path / "backups").stat().st_mode & 0o777) == "0o700"


def test_completion_shows_the_password_only_on_the_first_run(tmp_path):
    env_file = tmp_path / "deploy" / "dashboard" / ".env"
    env_file.parent.mkdir(parents=True)
    env_file.write_text("ADMIN_EMAIL=ben@crystalpm.com\nADMIN_PASSWORD=abc123\nBASE_URL=http://10.0.0.50:3001\n")
    first = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; FIRST_RUN=yes; print_completion")
    assert first.returncode == 0, first.stderr
    assert "http://10.0.0.50:3001" in first.stdout and "http://10.0.0.50" in first.stdout
    assert "ben@crystalpm.com" in first.stdout and "abc123" in first.stdout
    assert "install-dashboard.sh" in first.stdout and "/var/backups/croom-dashboard" in first.stdout
    assert "Fixed IP" in first.stdout and "Provisioning" in first.stdout
    later = run_bash(f"source {SCRIPT}; INSTALL_DIR={tmp_path}; print_completion")
    assert "abc123" not in later.stdout and "ben@crystalpm.com" in later.stdout


def test_main_runs_the_steps_in_order():
    body = SCRIPT.read_text().split("main() {")[1].split("\n}\n")[0]
    steps = [line.strip() for line in body.splitlines() if line.strip() and not line.strip().startswith(("echo", "#"))]
    assert steps == ["check_root", "check_platform", "install_docker", "fetch_source", "write_env", "start_stack",
                     "write_backup_units", "enable_backup_timer", "print_completion"]


def test_docker_comes_from_dockers_repository_and_the_fork_is_the_default_source():
    text = SCRIPT.read_text()
    assert "download.docker.com/linux/" in text and "docker-compose-plugin" in text
    assert 'CROOM_REPO="${CROOM_REPO:-https://github.com/ben-abeo/croom.to.git}"' in text
    assert "git pull --quiet --ff-only" in text
    assert "/health" in text and "--build" in text
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/installer/test_install_dashboard_script.py`
Expected: FAIL, the script does not exist.

- [ ] **Step 3: Write the installer**

Create `installer/install-dashboard.sh`:

```bash
#!/bin/bash
#
# Crystal Meet dashboard installer
#
# Runs the dashboard on a Raspberry Pi (or any 64-bit Debian-family machine)
# under Docker Compose. Run it once to install and again to update:
#   sudo bash installer/install-dashboard.sh --admin-email you@example.com
#

set -e

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

CROOM_REPO="${CROOM_REPO:-https://github.com/ben-abeo/croom.to.git}"
CROOM_BRANCH="${CROOM_BRANCH:-main}"
INSTALL_DIR="${INSTALL_DIR:-/opt/croom-dashboard}"
BACKUP_DIR="${BACKUP_DIR:-/var/backups/croom-dashboard}"
SYSTEMD_DIR="${SYSTEMD_DIR:-/etc/systemd/system}"
BIN_DIR="${BIN_DIR:-/usr/local/bin}"
MEMORY_KB="${MEMORY_KB:-$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)}"
ADMIN_EMAIL=""
ADDRESS=""
FIRST_RUN="no"

log() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1" >&2; exit 1; }

deploy_dir() { echo "$INSTALL_DIR/deploy/dashboard"; }
env_file() { echo "$(deploy_dir)/.env"; }
compose() { docker compose -f "$(deploy_dir)/docker-compose.yml" "$@"; }

check_root() {
    if [[ $EUID -ne 0 ]]; then
        error "Run with sudo: sudo bash $0 --admin-email you@example.com"
    fi
}

check_platform() {
    local arch
    arch="$(uname -m)"
    case "$arch" in
        aarch64|x86_64) ;;
        *) error "This needs a 64-bit system (found $arch). Flash Raspberry Pi OS Lite 64-bit." ;;
    esac
    if ! command -v apt-get >/dev/null 2>&1; then
        error "This installer needs a Debian-based system with apt (Raspberry Pi OS, Debian, Ubuntu)."
    fi
    if (( MEMORY_KB < 2000000 )); then
        error "At least 2 GB of memory is needed to build the dashboard image (found $((MEMORY_KB / 1024)) MB)."
    fi
    if (( MEMORY_KB < 3800000 )); then
        warn "Less than 4 GB of memory: building the image takes longer."
    fi
}

install_docker() {
    if docker compose version >/dev/null 2>&1; then
        log "Docker Compose is already installed"
        return
    fi
    log "Installing Docker Engine and the compose plugin from download.docker.com"
    apt-get update -qq
    apt-get install -y -qq ca-certificates curl git
    install -m 0755 -d /etc/apt/keyrings
    # shellcheck disable=SC1091
    . /etc/os-release
    curl -fsSL "https://download.docker.com/linux/${ID}/gpg" -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/${ID} ${VERSION_CODENAME} stable" \
        > /etc/apt/sources.list.d/docker.list
    apt-get update -qq
    apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin
    systemctl enable --now docker
    if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != "root" ]]; then
        usermod -aG docker "$SUDO_USER"
        log "Added $SUDO_USER to the docker group (takes effect at the next login)"
    fi
}

fetch_source() {
    if ! command -v git >/dev/null 2>&1; then
        apt-get update -qq
        apt-get install -y -qq git
    fi
    if [[ -d "$INSTALL_DIR/.git" ]]; then
        log "Updating $INSTALL_DIR from $CROOM_BRANCH"
        git -C "$INSTALL_DIR" fetch --quiet origin
        git -C "$INSTALL_DIR" checkout --quiet "$CROOM_BRANCH"
        git -C "$INSTALL_DIR" pull --quiet --ff-only origin "$CROOM_BRANCH"
    else
        log "Cloning $CROOM_REPO ($CROOM_BRANCH) into $INSTALL_DIR"
        git clone --quiet --branch "$CROOM_BRANCH" "$CROOM_REPO" "$INSTALL_DIR"
    fi
}

random_hex() { openssl rand -hex "$1"; }

write_env() {
    local file
    file="$(env_file)"
    if [[ -f "$file" ]]; then
        log "Keeping the existing settings in $file"
        if [[ -n "$ADMIN_EMAIL" ]]; then
            warn "--admin-email is ignored: the first admin is already recorded in $file"
        fi
        return
    fi
    if [[ -z "$ADMIN_EMAIL" ]]; then
        error "First install: pass --admin-email you@example.com (the first dashboard admin)"
    fi
    if [[ -z "$ADDRESS" ]]; then
        ADDRESS="$(hostname -I 2>/dev/null | awk '{print $1}')"
    fi
    if [[ -z "$ADDRESS" ]]; then
        error "Could not find this machine's address; pass --address"
    fi
    FIRST_RUN="yes"
    mkdir -p "$(dirname "$file")"
    (
        umask 077
        cat > "$file" <<ENVEOF
# Crystal Meet dashboard settings, written by install-dashboard.sh. Keep private.
DB_NAME=croom
DB_USER=croom
DB_PASSWORD=$(random_hex 16)
JWT_SECRET=$(random_hex 32)
ADMIN_EMAIL=$ADMIN_EMAIL
ADMIN_PASSWORD=$(random_hex 8)
BASE_URL=http://$ADDRESS:3001
WS_URL=ws://$ADDRESS:3001
LOG_LEVEL=info
ENVEOF
    )
    chmod 600 "$file"
    if [[ $EUID -eq 0 ]]; then
        chown root:root "$file"
    fi
    log "Wrote $file"
}

start_stack() {
    log "Building and starting the dashboard (the first build takes about ten minutes on a Pi)"
    compose up -d --build
    local i
    for i in $(seq 1 90); do
        if curl -fs http://127.0.0.1:3001/health >/dev/null 2>&1; then
            log "The dashboard is answering"
            return
        fi
        sleep 2
    done
    compose logs --tail 40 dashboard || true
    error "The dashboard did not answer on http://127.0.0.1:3001/health within three minutes"
}

write_backup_units() {
    install -m 755 "$(deploy_dir)/backup.sh" "$BIN_DIR/croom-dashboard-backup"
    mkdir -p "$BACKUP_DIR"
    chmod 700 "$BACKUP_DIR"
    sed -e "s|__BIN_DIR__|$BIN_DIR|g" -e "s|__INSTALL_DIR__|$INSTALL_DIR|g" -e "s|__BACKUP_DIR__|$BACKUP_DIR|g" \
        "$(deploy_dir)/croom-dashboard-backup.service" > "$SYSTEMD_DIR/croom-dashboard-backup.service"
    cp "$(deploy_dir)/croom-dashboard-backup.timer" "$SYSTEMD_DIR/croom-dashboard-backup.timer"
}

enable_backup_timer() {
    systemctl daemon-reload
    systemctl enable --now croom-dashboard-backup.timer
    log "Nightly backups at 02:30 into $BACKUP_DIR"
}

print_completion() {
    local file host admin_email admin_password address
    file="$(env_file)"
    host="$(hostname 2>/dev/null || echo crystal-meet)"
    admin_email="$(grep '^ADMIN_EMAIL=' "$file" | cut -d= -f2-)"
    admin_password="$(grep '^ADMIN_PASSWORD=' "$file" | cut -d= -f2-)"
    address="$(grep '^BASE_URL=' "$file" | sed -e 's|^BASE_URL=http://||' -e 's|:3001$||')"
    echo ""
    echo -e "${GREEN}The Crystal Meet dashboard is running.${NC}"
    echo ""
    echo "Open it:     http://$address  or  http://$address:3001  or  http://$host.local"
    echo "Rooms use:   http://$address:3001   (dashboard.url in each room config)"
    echo "Sign in as:  $admin_email"
    if [[ "$FIRST_RUN" == "yes" ]]; then
        echo "Password:    $admin_password   (shown once; change it on the Settings page)"
    else
        echo "Password:    unchanged (the first one is in $file if it was never changed)"
    fi
    echo ""
    echo "Next: give this machine a fixed address in your router (UniFi: Client Devices > Fixed IP),"
    echo "sign in, change the password, then create one token per room on Provisioning."
    echo ""
    echo "Backups:  $BACKUP_DIR (nightly at 02:30, 14 days kept)"
    echo "Logs:     cd $(deploy_dir) && docker compose logs -f"
    echo "Update:   sudo bash $INSTALL_DIR/installer/install-dashboard.sh"
    echo ""
}

main() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}  Crystal Meet dashboard installer${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""

    check_root
    check_platform
    install_docker
    fetch_source
    write_env
    start_stack
    write_backup_units
    enable_backup_timer
    print_completion
}

# Parse arguments and run only when executed, not when sourced by tests
run_installer() {
    while [[ $# -gt 0 ]]; do
        case $1 in
            --admin-email)
                ADMIN_EMAIL="${2:-}"
                if [[ "$ADMIN_EMAIL" != *@*.* ]]; then
                    error "--admin-email needs an email address (got '${ADMIN_EMAIL:-nothing}')"
                fi
                shift 2
                ;;
            --address)
                ADDRESS="${2:-}"
                if [[ -z "$ADDRESS" ]]; then
                    error "--address needs a host name or IP address"
                fi
                shift 2
                ;;
            --help)
                echo "Usage: sudo bash $0 --admin-email EMAIL [--address HOST]"
                echo ""
                echo "Options:"
                echo "  --admin-email EMAIL  The first dashboard admin (required on the first install)"
                echo "  --address HOST       This machine's address as the rooms will see it (default: its first IPv4)"
                echo "  --help               Show this help"
                echo ""
                echo "Environment:"
                echo "  CROOM_REPO    git source to install (default: this fork on GitHub)"
                echo "  CROOM_BRANCH  branch to install (default: main)"
                echo "  INSTALL_DIR   where the clone lives (default: /opt/croom-dashboard)"
                echo "  BACKUP_DIR    where nightly dumps go (default: /var/backups/croom-dashboard)"
                exit 0
                ;;
            *)
                error "Unknown option: $1"
                ;;
        esac
    done
    main
}

if [[ "${BASH_SOURCE[0]}" == "${0}" ]]; then
    run_installer "$@"
fi
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/installer/test_install_dashboard_script.py`
Expected: PASS, 13 tests.

- [ ] **Step 5: Commit**

```bash
git add installer/install-dashboard.sh tests/unit/installer/test_install_dashboard_script.py
git commit -m "feat(installer): one-command Docker install and update for the dashboard"
```

---

### Task 8: The dashboard guide

**Files:**
- Create: `docs/guides/crystal-meet-dashboard/index.html`
- Create: `docs/guides/crystal-meet-dashboard/build.py`
- Copy: `docs/guides/crystal-meet-dashboard/crystalpm-logo-white.svg`, `docs/guides/crystal-meet-dashboard/fonts/` from the Zoom guide folder
- Create: `docs/guides/crystal-meet-dashboard.pdf` (rendered)
- Test: `tests/unit/docs/test_dashboard_guide.py`

- [ ] **Step 1: Write the failing test**

Create `tests/unit/docs/test_dashboard_guide.py`:

```python
"""
The dashboard guide builds to a multi-page PDF, is self-contained, and quotes
the real commands, names and paths.
"""

import re
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
GUIDES = REPO / "docs" / "guides"
GUIDE = GUIDES / "crystal-meet-dashboard"

pytest.importorskip("playwright.sync_api")


def test_dashboard_guide_builds_to_a_multi_page_pdf(tmp_path):
    out = tmp_path / "guide.pdf"
    result = subprocess.run([sys.executable, str(GUIDE / "build.py"), str(out)], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    data = out.read_bytes()
    assert data.startswith(b"%PDF")
    counts = [int(m) for m in re.findall(rb"/Count (\d+)", data)]
    assert counts and max(counts) >= 3, counts


def test_dashboard_guide_is_self_contained():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert not re.search(r"""(src|href)=["']https?://""", html)
    assert "url(http" not in html and "@import" not in html
    assert "Crystal Meet" in html and "Croom " not in html
    for asset in ("crystalpm-logo-white.svg", "fonts/lexend-400.woff2", "fonts/lexend-600.woff2", "fonts/OFL.txt"):
        assert (GUIDE / asset).is_file(), asset


def test_dashboard_guide_quotes_the_real_commands_and_names():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    for needle in ("Raspberry Pi OS Lite (64-bit)", "crystal-meet", "Fixed IP", "hostname -I",
                   "install-dashboard.sh --admin-email", "/opt/croom-dashboard", "Settings",
                   "Provisioning", "dashboard.url", ":3001", "/var/backups/croom-dashboard",
                   "docker compose logs", "psql", "gunzip -c", "systemctl restart croom"):
        assert needle in html, needle


def test_dashboard_guide_uses_the_shared_renderer():
    assert "from render_guide import render" in (GUIDE / "build.py").read_text(encoding="utf-8")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs/test_dashboard_guide.py`
Expected: FAIL, the guide folder does not exist.

- [ ] **Step 3: Create the folder, assets and build script**

```bash
mkdir -p docs/guides/crystal-meet-dashboard
cp docs/guides/crystal-meet-zoom/crystalpm-logo-white.svg docs/guides/crystal-meet-dashboard/
cp -r docs/guides/crystal-meet-zoom/fonts docs/guides/crystal-meet-dashboard/
```

Create `docs/guides/crystal-meet-dashboard/build.py`:

```python
"""
Render the dashboard guide to PDF.

Usage: python build.py [output.pdf]   (default: ../crystal-meet-dashboard.pdf)
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
from render_guide import render  # noqa: E402

if __name__ == "__main__":
    render(HERE, Path(sys.argv[1]) if len(sys.argv) > 1 else HERE.parent / "crystal-meet-dashboard.pdf",
           "How-to guide · Set up the Crystal Meet dashboard")
```

- [ ] **Step 4: Write the guide**

Create `docs/guides/crystal-meet-dashboard/index.html` with the Zoom guide's `<head>` and stylesheet copied verbatim (lines 1-60 of `docs/guides/crystal-meet-zoom/index.html`, with the `<title>` changed to `Set up the Crystal Meet dashboard` and `.glance { grid-template-columns: repeat(6, 1fr) }` kept), then this body:

```html
<body>

<section class="banner">
  <img src="crystalpm-logo-white.svg" alt="Crystal PM">
  <p class="kicker top">Crystal PM</p>
  <p class="kicker">How-to guide</p>
  <h1>Set up the Crystal Meet dashboard</h1>
  <p>One small always-on Raspberry Pi on the office network runs the dashboard that every room reports to. One command installs it; the same command updates it.</p>
</section>

<p class="intro">Do this once, before the first room. Plan about 30 minutes, most of it waiting for the Pi to download and build. The dashboard is where you create a token for each room, see which rooms are online, and watch their meetings. Rooms keep joining calls even when the dashboard is off; they only show Offline until it is back.</p>

<div class="glance">
  <div class="card"><p class="kicker">Step 1</p><h3>Prepare the Pi</h3><p>Pi OS Lite 64-bit, hostname crystal-meet, wired.</p></div>
  <div class="card"><p class="kicker">Step 2</p><h3>Fix its address</h3><p>A DHCP reservation so the address never changes.</p></div>
  <div class="card"><p class="kicker">Step 3</p><h3>Install</h3><p>One command: Docker, the dashboard, backups.</p></div>
  <div class="card"><p class="kicker">Step 4</p><h3>Sign in</h3><p>The printed password, then a new one.</p></div>
  <div class="card"><p class="kicker">Step 5</p><h3>Rooms</h3><p>A token per room and the address in each config.</p></div>
  <div class="card"><p class="kicker">Step 6</p><h3>Keep it running</h3><p>Backups, updates, logs.</p></div>
</div>

<div class="callout warn">
  <p class="kicker">Before you begin</p>
  <p><strong>Hardware.</strong> A Raspberry Pi 4 or Pi 5 with 4 GB or more, its official power supply, an Ethernet cable to the office network, and a 32 GB microSD card. A USB SSD instead of the card is kinder to a database but not required. No screen or keyboard: you work over SSH from your computer.</p>
  <p><strong>Access.</strong> A computer with Raspberry Pi Imager, SSH, and the login to your router (UniFi) to reserve an address.</p>
  <p><strong>What this is not.</strong> It is not one of the room devices. Keep the dashboard on its own Pi: a room Pi is busy during calls and the first thing to be unplugged or reinstalled.</p>
</div>

<section class="block">
<div class="step"><span class="badge">1</span><h2>Prepare the Pi</h2></div>
<ul>
  <li><strong>Flash the card.</strong> In <span class="ui">Raspberry Pi Imager</span> choose your Pi model, then <span class="ui">Raspberry Pi OS (other) › Raspberry Pi OS Lite (64-bit)</span>, and the card or SSD. Lite has no desktop; the dashboard needs none.</li>
  <li><strong>Pre-fill the settings.</strong> When Imager offers customisation, set the hostname to <span class="chip">crystal-meet</span>, a username (for example <span class="chip">pi</span>) with a password you keep, your time zone, and turn on SSH with password authentication. Skip Wi-Fi: use the cable. Write the card.</li>
  <li><strong>Boot it.</strong> Put the card in, connect Ethernet and power, wait two minutes.</li>
  <li><strong>Sign in from your computer.</strong> Open a terminal and type <span class="chip">ssh pi@crystal-meet.local</span> (your username instead of pi). If the name does not resolve, find the Pi's address in your router's client list and use that.</li>
</ul>
<p class="see">You should now see a shell prompt on the Pi.</p>
</section>

<section class="block">
<div class="step"><span class="badge">2</span><h2>Give it a fixed address</h2></div>
<ul>
  <li><strong>Find the address.</strong> On the Pi type <span class="chip">hostname -I</span>; the first number, for example <span class="chip">10.0.0.50</span>, is its address.</li>
  <li><strong>Reserve it.</strong> In the UniFi Network app open <span class="ui">Client Devices</span>, pick <span class="chip">crystal-meet</span>, open its settings and turn on <span class="ui">Fixed IP Address</span> with that address. Any other router has the same thing under DHCP reservations.</li>
  <li><strong>Write it down.</strong> This address goes into every room's config in step 5.</li>
</ul>
<p class="see">You should now see the Pi listed in the router with a fixed address.</p>
</section>

<section class="block">
<div class="step"><span class="badge">3</span><h2>Install the dashboard</h2></div>
<ul>
  <li><strong>Get the code.</strong> On the Pi type <span class="chip">sudo apt install -y git</span>, then <span class="chip wrap">git clone https://github.com/ben-abeo/croom.to.git &amp;&amp; cd croom.to</span>.</li>
  <li><strong>Run the installer</strong> with the email address of the first admin, which is you: <span class="chip wrap">sudo bash installer/install-dashboard.sh --admin-email you@crystalpm.com</span>. It installs Docker, copies the code to <span class="chip">/opt/croom-dashboard</span>, writes the secrets to a private file, builds the dashboard image, starts it, and sets up nightly backups. The build takes about ten minutes on a Pi; the screen stays busy.</li>
  <li><strong>Read the last lines.</strong> They show the dashboard's address, the admin email, and a password shown this once. Copy the password into your password manager now.</li>
</ul>
<p class="see">You should now see "The Crystal Meet dashboard is running" and the address lines.</p>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>The secrets live only in <span class="chip">/opt/croom-dashboard/deploy/dashboard/.env</span> on the Pi, readable by root. The installer never rewrites that file, so running it again later is safe: it only updates the code and rebuilds.</p>
</div>
</section>

<section class="block">
<div class="step"><span class="badge">4</span><h2>Sign in and change the password</h2></div>
<ul>
  <li><strong>Open the dashboard</strong> from any computer on the office network at <span class="chip">http://crystal-meet.local</span> or <span class="chip">http://10.0.0.50</span> (your address). Sign in with the admin email and the printed password.</li>
  <li><strong>Change the password.</strong> Open <span class="ui">Settings</span>, fill in <span class="ui">Change password</span> with the printed password and a new one of at least 8 characters, press <span class="ui">Change password</span>.</li>
</ul>
<p class="see">You should now see the Devices page, empty for now, and "Password changed" on Settings.</p>
</section>

<section class="block">
<div class="step"><span class="badge">5</span><h2>Point the rooms at it</h2></div>
<ul>
  <li><strong>One token per room.</strong> On <span class="ui">Provisioning</span>, type the room's name and press <span class="ui">Generate Token</span>; each token works once. The room setup guide uses it in its step 2.</li>
  <li><strong>The address in each room config.</strong> In <span class="chip">deploy/rooms/room-N.yaml</span> set <span class="ui">dashboard.url</span> to <span class="chip">http://10.0.0.50:3001</span> (your address, port <span class="chip">3001</span>) before installing that room.</li>
  <li><strong>A room that is already installed.</strong> On that room's Pi edit <span class="chip">/etc/croom/config.yaml</span> with <span class="chip">sudo nano</span>, set the same <span class="ui">url</span> under <span class="ui">dashboard</span>, then type <span class="chip">sudo systemctl restart croom</span>.</li>
</ul>
<p class="see">You should now see each room appear on Devices as online within a minute of its install or restart.</p>
</section>

<section class="block">
<div class="step"><span class="badge">6</span><h2>Keep it running</h2></div>
<ul>
  <li><strong>Backups happen by themselves</strong> every night at 02:30 into <span class="chip">/var/backups/croom-dashboard</span>, 14 days kept. Copy one off the Pi now and then: from your computer, <span class="chip wrap">scp pi@crystal-meet.local:/var/backups/croom-dashboard/croom-dashboard-2026-10-05.sql.gz .</span> (the Pi's user may need sudo rights to read it; <span class="chip">sudo chmod 644</span> the file first if so).</li>
  <li><strong>Restore one</strong> onto a running dashboard, for example after swapping the card and reinstalling: on the Pi, <span class="chip">cd /opt/croom-dashboard/deploy/dashboard</span>, then <span class="chip">docker compose stop dashboard</span>, then <span class="chip wrap">gunzip -c /var/backups/croom-dashboard/croom-dashboard-2026-10-05.sql.gz | sudo docker compose exec -T db psql -U croom -d croom</span>, then <span class="chip">docker compose start dashboard</span>.</li>
  <li><strong>Update</strong> whenever the code changes: <span class="chip wrap">sudo bash /opt/croom-dashboard/installer/install-dashboard.sh</span>. It pulls, rebuilds and restarts; devices, users and tokens stay.</li>
  <li><strong>Logs.</strong> <span class="chip">cd /opt/croom-dashboard/deploy/dashboard</span> then <span class="chip">docker compose logs -f dashboard</span>. <span class="chip">docker compose ps</span> shows both parts as Up (healthy).</li>
  <li><strong>Power.</strong> Pull the plug and put it back: the dashboard comes back on its own within about a minute.</li>
</ul>
<p class="see">You should now see a dated file in the backup folder the morning after the install.</p>
</section>

<div class="page-break"></div>
<section class="block">
<p class="kicker">If something is off</p>
<h2>Troubleshooting</h2>
<div class="trouble">
  <div class="card"><h3>The installer stops while installing Docker</h3><p>The Pi could not reach the internet or apt is broken. Check <span class="chip">ping -c 3 download.docker.com</span>, then <span class="chip">sudo apt update</span>, and run the installer again.</p></div>
  <div class="card"><h3>The build fails or the Pi freezes</h3><p>Not enough memory: the installer refuses below 2 GB and warns below 4 GB. Use a 4 GB Pi, or add swap with <span class="chip">sudo dphys-swapfile swapoff</span>, set <span class="chip">CONF_SWAPSIZE=2048</span> in <span class="chip">/etc/dphys-swapfile</span>, <span class="chip">sudo dphys-swapfile setup &amp;&amp; sudo dphys-swapfile swapon</span>, and run the installer again.</p></div>
  <div class="card"><h3>The page is blank or does not load</h3><p>On the Pi, <span class="chip">cd /opt/croom-dashboard/deploy/dashboard</span> then <span class="chip">docker compose ps</span> and <span class="chip">docker compose logs dashboard</span>. "Database initialization failed" means Postgres is still starting; wait a minute. Anything else: run the installer again.</p></div>
  <div class="card"><h3>Rooms show Offline</h3><p>From a room's Pi type <span class="chip">curl http://10.0.0.50:3001/health</span> (your address). No answer means the address in that room's config is wrong or the dashboard Pi is off. An answer with the room still offline means the room's token was already used: make a new one on Provisioning.</p></div>
  <div class="card"><h3>Forgotten admin password</h3><p>Delete the admin and let the next start recreate it from the private file: <span class="chip wrap">docker compose exec db psql -U croom -d croom -c "DELETE FROM users WHERE email = 'you@crystalpm.com';"</span> then <span class="chip">docker compose restart dashboard</span>; the first password is on the <span class="chip">ADMIN_PASSWORD</span> line of <span class="chip">/opt/croom-dashboard/deploy/dashboard/.env</span> (<span class="chip">sudo cat</span>).</p></div>
  <div class="card"><h3>crystal-meet.local does not resolve</h3><p>Some networks and tablets do not resolve <span class="chip">.local</span> names. Use the fixed address instead; that is also why the room configs use the address, not the name.</p></div>
</div>
</section>
<div class="callout">
  <p class="kicker">Good to know</p>
  <p>Everything speaks plain HTTP on the office network; nothing is reachable from the internet unless you forward a port, which this guide never asks you to do.</p>
</div>

</body>
</html>
```

- [ ] **Step 5: Build the PDF and look at it**

Run: `.venv/bin/python docs/guides/crystal-meet-dashboard/build.py`
Expected: `wrote .../docs/guides/crystal-meet-dashboard.pdf`. Render page 1 and the last page to PNG with `pymupdf` as in earlier guides and check the six cards fit on one row and no card text overflows.

- [ ] **Step 6: Run the test to verify it passes**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs/test_dashboard_guide.py`
Expected: PASS, 4 tests.

- [ ] **Step 7: Commit**

```bash
git add docs/guides/crystal-meet-dashboard docs/guides/crystal-meet-dashboard.pdf tests/unit/docs/test_dashboard_guide.py
git commit -m "docs: dashboard guide for the fourth Raspberry Pi"
```

---

### Task 9: README, deploy notes, room setup guide, gitignore

**Files:**
- Modify: `README.md` (pieces table, guides list, dashboard section, notes table, limitations)
- Modify: `deploy/rooms/README.md:10-12`
- Modify: `docs/guides/crystal-meet-room-setup/index.html:81,99,148` and rebuild `docs/guides/crystal-meet-room-setup.pdf`
- Modify: `.gitignore`
- Modify: `tests/unit/docs/test_readme.py`, `tests/unit/docs/test_room_setup_guide.py`

- [ ] **Step 1: Update the tests first**

In `tests/unit/docs/test_readme.py` replace `"Follow the three guides",` with these three needles:

```python
        "Follow the four guides",
        "docs/guides/crystal-meet-dashboard.pdf",
        "install-dashboard.sh --admin-email",
        "deploy/dashboard/",
```

In `tests/unit/docs/test_room_setup_guide.py` add:

```python
def test_guide_sends_the_reader_to_the_dashboard_guide_for_the_address():
    html = (GUIDE / "index.html").read_text(encoding="utf-8")
    assert "Set up the Crystal Meet dashboard" in html
    assert "mirrored networking" not in html and "port 3000" not in html
```

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs/test_readme.py tests/unit/docs/test_room_setup_guide.py`
Expected: FAIL on the new needles.

- [ ] **Step 2: Edit the README**

Apply these replacements to `README.md` (python snippet, run from the repo root; each `old` must match exactly once):

```python
import pathlib
p = pathlib.Path("README.md"); s = p.read_text()
def sub(old, new):
    global s
    assert s.count(old) == 1, old[:50]; s = s.replace(old, new)

sub("| Dashboard, `src/croom-dashboard` | One server; this fork runs it on a Windows PC under WSL2 | Fleet overview, device status, enrollment tokens on the Provisioning page. |",
    "| Dashboard, `src/croom-dashboard` | A fourth Raspberry Pi (or any 64-bit Docker host) on the office network, from `deploy/dashboard/` | Fleet overview, device status, enrollment tokens on the Provisioning page. |")

sub("""Follow the three guides in this order:

1. [Set up a Crystal Meet room](docs/guides/crystal-meet-room-setup.pdf): prepare""",
    """Follow the four guides in this order:

1. [Set up the Crystal Meet dashboard](docs/guides/crystal-meet-dashboard.pdf): a
   fourth Raspberry Pi on the office network runs the dashboard under Docker;
   one install command, a fixed address, nightly backups.
2. [Set up a Crystal Meet room](docs/guides/crystal-meet-room-setup.pdf): prepare""")
sub("2. [Connect Crystal Meet rooms to Google Calendar]", "3. [Connect Crystal Meet rooms to Google Calendar]")
sub("3. [Connect Crystal Meet rooms to Zoom]", "4. [Connect Crystal Meet rooms to Zoom]")

sub("""## Run the dashboard

```bash
docker run""",
    """## Run the dashboard

In production the dashboard runs on its own Raspberry Pi, or any 64-bit Docker
host, from `deploy/dashboard/`: Postgres and the dashboard image built from
this repo, under Docker Compose. One command installs it and later updates it:

```bash
sudo bash installer/install-dashboard.sh --admin-email you@crystalpm.com
```

It installs Docker, clones this fork into `/opt/croom-dashboard`, writes the
secrets once to `deploy/dashboard/.env` there (mode 600, never committed),
builds and starts the stack on ports 3001 and 80, and adds a nightly `pg_dump`
to `/var/backups/croom-dashboard`. The first admin comes from `ADMIN_EMAIL` and
`ADMIN_PASSWORD` in that file and is created only when missing. There are no
database migrations: production creates missing tables at start-up and never
alters existing ones. The dashboard guide covers the Pi; `deploy/dashboard/README.md`
has the operator's commands.

For development on this machine:

```bash
docker run""")

sub("""Open `http://localhost:3000`, sign in with the admin account the backend
creates on first start, and use Provisioning to create one token per room.""",
    """Open `http://localhost:3000` and sign in; on an empty database the backend
creates the admin from `ADMIN_EMAIL` and `ADMIN_PASSWORD` in `backend/.env`.
Use Provisioning to create one token per room.""")

sub("| Zoom joins through the Meeting SDK: signature, per-room Zoom user's ZAK for outside hosts, loopback page, `croom --check-zoom`, the Zoom guide | [spec](docs/superpowers/specs/2026-09-25-zoom-meeting-sdk-design.md) | [plan](docs/superpowers/plans/2026-09-25-zoom-meeting-sdk.md) |",
    "| Zoom joins through the Meeting SDK: signature, per-room Zoom user's ZAK for outside hosts, loopback page, `croom --check-zoom`, the Zoom guide | [spec](docs/superpowers/specs/2026-09-25-zoom-meeting-sdk-design.md) | [plan](docs/superpowers/plans/2026-09-25-zoom-meeting-sdk.md) |\n| The dashboard on a Raspberry Pi: Docker Compose packaging, production mode in the backend, the dashboard installer and guide | [spec](docs/superpowers/specs/2026-10-05-dashboard-on-a-pi-design.md) | [plan](docs/superpowers/plans/2026-10-05-dashboard-on-a-pi.md) |")

sub("- The browser path and boot-ordering fixes in the installer are verified by tests of the generated unit files, not yet on a Pi.",
    "- The browser path and boot-ordering fixes in the installer are verified by tests of the generated unit files, not yet on a Pi.\n- The dashboard speaks plain HTTP on the office network, with no TLS, and has no database migrations; a model change that needs an altered table is a manual `psql` step in production.")
p.write_text(s); print("README updated")
```

- [ ] **Step 3: Edit the deploy notes, the room setup guide and the gitignore**

In `deploy/rooms/README.md` replace the `dashboard.url` bullet with:

```markdown
- `dashboard.url`: the dashboard's address on port 3001, which is the dashboard
  Pi's reserved address, for example `http://10.0.0.50:3001`. The guide "Set up
  the Crystal Meet dashboard" installs it and reserves the address.
```

In `docs/guides/crystal-meet-room-setup/index.html`:

- replace the whole `<p><strong>The dashboard address.</strong> ...</p>` paragraph (line 81) with:

```html
  <p><strong>The dashboard.</strong> The Crystal Meet dashboard must already be running on the office network; the guide "Set up the Crystal Meet dashboard" installs it on its own Raspberry Pi with a fixed address, for example <span class="chip">http://10.0.0.50</span>. Rooms talk to it on port <span class="chip">3001</span>.</p>
```

- change `<li><strong>Open the dashboard</strong> at its address on port 3000 and sign in.</li>` to `<li><strong>Open the dashboard</strong> at its address, for example <span class="chip">http://10.0.0.50</span> or <span class="chip">http://crystal-meet.local</span>, and sign in.</li>`
- in the "The device shows offline" card change `check the address in <span class="chip">~/room.yaml</span> and the dashboard PC's networking.` to `check the address in <span class="chip">~/room.yaml</span> and that the dashboard Pi is up (its guide has the checks).`

Rebuild its PDF: `.venv/bin/python docs/guides/crystal-meet-room-setup/build.py`.

Append to `.gitignore` under the credential files comment:

```
# The dashboard's generated settings live only on the dashboard host
deploy/dashboard/.env
```

- [ ] **Step 4: Run the docs tests**

Run: `.venv/bin/pytest -q -p no:cacheprovider tests/unit/docs tests/unit/deploy`
Expected: PASS (README, all four guides, compose, backup, room configs).

- [ ] **Step 5: Commit**

```bash
git add README.md deploy/rooms/README.md docs/guides/crystal-meet-room-setup/index.html docs/guides/crystal-meet-room-setup.pdf .gitignore tests/unit/docs/test_readme.py tests/unit/docs/test_room_setup_guide.py
git commit -m "docs: README and guides point rooms at the dashboard Pi"
```

---

### Task 10: Acceptance on this machine and the suite gate

**Files:**
- Create: `tests/unit/deploy/test_dashboard_stack.py` (opt-in browser test against a running stack)

- [ ] **Step 1: Write the opt-in stack test**

Create `tests/unit/deploy/test_dashboard_stack.py`:

```python
"""
Against a running dashboard stack (CROOM_DASHBOARD_URL plus the admin's email
and password in CROOM_DASHBOARD_EMAIL / CROOM_DASHBOARD_PASSWORD): the web app
loads from the backend, the admin signs in, a token enrolls a device, and the
register route is closed. Skipped unless the variables are set.
"""

import os
import uuid

import pytest
import requests

URL = os.environ.get("CROOM_DASHBOARD_URL")
EMAIL = os.environ.get("CROOM_DASHBOARD_EMAIL")
PASSWORD = os.environ.get("CROOM_DASHBOARD_PASSWORD")

pytestmark = pytest.mark.skipif(not (URL and EMAIL and PASSWORD), reason="no running dashboard stack configured")


def test_health_and_web_app_come_from_the_same_origin():
    assert requests.get(f"{URL}/health", timeout=5).json()["status"] == "healthy"
    page = requests.get(f"{URL}/", headers={"Accept": "text/html"}, timeout=5)
    assert page.status_code == 200 and "<title>Crystal Meet</title>" in page.text
    assert "upgrade-insecure-requests" not in page.headers.get("content-security-policy", "")
    route = requests.get(f"{URL}/settings", headers={"Accept": "text/html"}, timeout=5)
    assert route.status_code == 200 and 'id="root"' in route.text
    assert requests.get(f"{URL}/api/nope", headers={"Accept": "application/json"}, timeout=5).status_code == 404


def test_admin_signs_in_and_register_is_closed():
    login = requests.post(f"{URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=5)
    assert login.status_code == 200, login.text
    token = login.json()["token"]
    assert requests.post(f"{URL}/api/auth/register", json={"email": "x@y.z", "password": "p", "name": "x"}, timeout=5).status_code == 401
    me = requests.get(f"{URL}/api/auth/me", headers={"Authorization": f"Bearer {token}"}, timeout=5)
    assert me.json()["role"] == "admin" and me.json()["email"] == EMAIL


def test_a_token_enrolls_a_device():
    token = requests.post(f"{URL}/api/auth/login", json={"email": EMAIL, "password": PASSWORD}, timeout=5).json()["token"]
    created = requests.post(f"{URL}/api/provisioning/token", json={"roomName": f"Test {uuid.uuid4().hex[:6]}"},
                            headers={"Authorization": f"Bearer {token}"}, timeout=5)
    assert created.status_code == 201, created.text
    enrollment_token = created.json()["token"]
    enrolled = requests.post(f"{URL}/api/provisioning/enroll",
                             json={"token": enrollment_token, "deviceInfo": {"platform": "test", "softwareVersion": "0"}}, timeout=5)
    assert enrolled.status_code == 200, enrolled.text
    assert enrolled.json()["deviceId"] == created.json()["deviceId"]
    assert enrolled.json()["websocketUrl"].endswith("/ws")
```

The response shapes come from `routes/provisioning.ts` (token creation answers 201 with `token` and `deviceId`; enroll answers 200 with `deviceId` and `websocketUrl`) and `routes/auth.ts` (`/me` answers `{ id, email, name, role }`).

- [ ] **Step 2: Start the stack on spare ports**

```bash
cat > deploy/dashboard/.env <<'EOF'
DB_NAME=croom
DB_USER=croom
DB_PASSWORD=acceptance-db-secret
JWT_SECRET=acceptance-jwt-secret-0123456789abcdef
ADMIN_EMAIL=ben@crystalpm.com
ADMIN_PASSWORD=acceptance-admin-pw
BASE_URL=http://127.0.0.1:3101
WS_URL=ws://127.0.0.1:3101
LOG_LEVEL=info
DASHBOARD_PORT=3101
DASHBOARD_HTTP_PORT=8081
EOF
docker compose -f deploy/dashboard/docker-compose.yml up -d --build
for i in $(seq 1 60); do curl -fs http://127.0.0.1:3101/health && break; sleep 2; done
docker compose -f deploy/dashboard/docker-compose.yml logs dashboard | tail -20
```

Expected: the health JSON, and the log shows "Database tables created where missing", "Created admin user ben@crystalpm.com" and "Serving the web app from /app/public".

- [ ] **Step 3: Run the stack test and look at the page**

```bash
CROOM_DASHBOARD_URL=http://127.0.0.1:3101 CROOM_DASHBOARD_EMAIL=ben@crystalpm.com CROOM_DASHBOARD_PASSWORD=acceptance-admin-pw \
  .venv/bin/pytest -q -p no:cacheprovider tests/unit/deploy/test_dashboard_stack.py
```

Expected: 3 passed. Then with the venv's Playwright open `http://127.0.0.1:8081/` (the port-80 mapping), sign in, open Settings, change the password to `acceptance-admin-pw2` and back, and take a screenshot of the Devices page to confirm the app renders with its styles (a blank page means the CSP or the static path is wrong).

- [ ] **Step 4: Tear down and run the gate**

```bash
docker compose -f deploy/dashboard/docker-compose.yml down -v
rm deploy/dashboard/.env
git status --short     # must not list deploy/dashboard/.env
cd src/croom-dashboard/backend && npx jest && cd ../../..
bash /tmp/claude-1000/-home-cpm-ssh/f78b4b20-e6f0-4204-894b-193a8d8a2549/scratchpad/suite-gate.sh dashboard-pi | grep "GATE:"
```

Expected: all Jest suites pass; `GATE: PASSED` with no new failures beyond the 81 upstream ones.

- [ ] **Step 5: Commit**

```bash
git add tests/unit/deploy/test_dashboard_stack.py
git commit -m "test: opt-in acceptance against a running dashboard stack"
```

Then record in the final message what was proven on this machine (x86_64 image, admin bootstrap, web app, enrollment) and what waits for the Pi (arm64 build, Docker install from the repository, the backup timer, the guide walk-through).
