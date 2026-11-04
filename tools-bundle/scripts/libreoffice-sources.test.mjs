import test from 'node:test';
import assert from 'node:assert/strict';
import { parseDownloadList } from './collect-libreoffice-sources.mjs';

const hash = 'a'.repeat(64);
test('LibreOffice external sources use the release checksum and expanded filename', () => {
  const entries = parseDownloadList(`FOO_MINOR := 2\nFOO_TARBALL := foo-1.$(FOO_MINOR).tar.xz\nFOO_SHA256SUM := ${hash}\n`);
  assert.deepEqual(entries, [{ id: 'FOO', filename: 'foo-1.2.tar.xz', sha256: hash, url: 'https://dev-www.libreoffice.org/src/foo-1.2.tar.xz' }]);
});

test('an incomplete or unsafe upstream source list cannot become a source catalog', () => {
  for (const text of [
    '', 'FOO_TARBALL := foo.tar.gz',
    `FOO_TARBALL := ../foo.tar.gz\nFOO_SHA256SUM := ${hash}`,
    `FOO_TARBALL := $(MISSING)\nFOO_SHA256SUM := ${hash}`,
    `FOO_TARBALL := $(LOOP)\nLOOP := $(LOOP)\nFOO_SHA256SUM := ${hash}`,
    `FOO_TARBALL := $(shell command)\nFOO_SHA256SUM := ${hash}`,
  ]) assert.throws(() => parseDownloadList(text));
});
