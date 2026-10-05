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
