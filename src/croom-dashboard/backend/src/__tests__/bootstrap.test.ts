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
