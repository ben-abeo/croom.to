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
