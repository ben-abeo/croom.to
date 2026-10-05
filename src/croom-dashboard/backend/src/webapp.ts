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
