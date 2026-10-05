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
