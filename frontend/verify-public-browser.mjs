// The public-corpus browser verification includes the required login flow.
import './verify-auth-browser.mjs';
import { mkdir, copyFile } from 'node:fs/promises';
await mkdir('../.local/rag', { recursive: true });
await copyFile('../.local/auth/live-browser-verification.json', '../.local/rag/public-browser-verification.json');
