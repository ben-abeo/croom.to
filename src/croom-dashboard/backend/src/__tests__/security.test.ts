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
