#!/usr/bin/env node
/**
 * TypeScript SentenceChunker shim for cross-SDK parity tests.
 *
 * Reads JSON from stdin: { "tokens": ["...", "..."] }
 * Writes JSON to stdout: ["sentence1", "sentence2", ...]
 * On error: { "error": "message" } and exits non-zero.
 *
 * Requires sdk-ts/dist/index.js to exist (run `npm --prefix sdk-ts run build`).
 */

const fs = require('fs');
const path = require('path');

function main() {
  let payload;
  try {
    const stdin = fs.readFileSync('/dev/stdin', 'utf8');
    payload = JSON.parse(stdin);
  } catch (e) {
    process.stdout.write(JSON.stringify({ error: `stdin parse: ${e.message}` }));
    process.exit(1);
  }

  const tokens = payload.tokens;
  if (!Array.isArray(tokens)) {
    process.stdout.write(JSON.stringify({ error: 'payload.tokens must be an array' }));
    process.exit(1);
  }

  const sdkPath = path.resolve(__dirname, '../../sdk-ts/dist/index.js');
  let sdk;
  try {
    sdk = require(sdkPath);
  } catch (e) {
    process.stdout.write(
      JSON.stringify({
        error: `cannot load sdk-ts dist (${e.message}); run: npm --prefix sdk-ts run build`,
      }),
    );
    process.exit(1);
  }

  const SentenceChunker = sdk.SentenceChunker;
  if (typeof SentenceChunker !== 'function') {
    process.stdout.write(JSON.stringify({ error: 'SentenceChunker not exported from sdk-ts' }));
    process.exit(1);
  }

  const chunker = new SentenceChunker();
  const emitted = [];
  for (const token of tokens) {
    for (const sentence of chunker.push(token)) emitted.push(sentence);
  }
  for (const sentence of chunker.flush()) emitted.push(sentence);

  process.stdout.write(JSON.stringify(emitted));
}

main();
