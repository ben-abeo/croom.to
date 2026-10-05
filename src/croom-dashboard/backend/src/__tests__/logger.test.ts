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
