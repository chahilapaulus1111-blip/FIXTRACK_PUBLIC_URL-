import { readFile, writeFile } from 'node:fs/promises';
import { fileURLToPath } from 'node:url';

const configPath = fileURLToPath(new URL('./capacitor.config.json', import.meta.url));
const publicUrl = process.env.FIXTRACK_PUBLIC_URL?.trim();

if (!publicUrl) {
  throw new Error('Set FIXTRACK_PUBLIC_URL to the deployed HTTPS FixTrack URL before building mobile apps.');
}

const parsedUrl = new URL(publicUrl);
if (parsedUrl.protocol !== 'https:') {
  throw new Error('FIXTRACK_PUBLIC_URL must use HTTPS for Android and iOS builds.');
}

const config = JSON.parse(await readFile(configPath, 'utf8'));
config.server = {
  ...config.server,
  url: parsedUrl.origin,
  allowNavigation: [...new Set([...(config.server.allowNavigation || []), parsedUrl.host])]
};
await writeFile(configPath, `${JSON.stringify(config, null, 2)}\n`);
console.log(`Configured FixTrack mobile shell for ${parsedUrl.origin}`);
