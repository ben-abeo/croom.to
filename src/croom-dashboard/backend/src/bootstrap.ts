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
