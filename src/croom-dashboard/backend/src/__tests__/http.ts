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
