import test from 'node:test';
import assert from 'node:assert/strict';
import { hasReviewedSourceBundle, sourceAssetName } from './lib.mjs';

function fixture() {
  const hash = 'a'.repeat(64);
  const entry = { sourceBundle: ['project', 'dependencies', 'recipes'], package: { upstream: 'binary' } };
  const config = { upstream: { binary: { sha256: hash } }, sources: Object.fromEntries(entry.sourceBundle.map(id => [id, {
    attach: true, sha256: 'b'.repeat(64), binaryArchives: [hash], includesBuildInstructions: id === 'recipes',
  }])) };
  return { hash, entry, config };
}

test('all split sources must be pinned, attached and reviewed for the exact binary', () => {
  const { hash, entry, config } = fixture();
  assert.equal(hasReviewedSourceBundle(entry, config), true);
  for (const patch of [{ attach: false }, { attach: 'true' }, { sha256: '' }, { binaryArchives: [] }, { binaryArchives: hash }, { binaryArchives: ['c'.repeat(64)] }]) {
    const changed = structuredClone(config);
    Object.assign(changed.sources.dependencies, patch);
    assert.equal(hasReviewedSourceBundle(entry, changed), false);
  }
  delete config.sources.dependencies;
  assert.equal(hasReviewedSourceBundle(entry, config), false);
});

test('source sets require build instructions and reject malformed or duplicate IDs', () => {
  const { entry, config } = fixture();
  for (const ids of [[], ['project', 'project'], ['project', 'missing'], ['project', 1], '']) {
    assert.equal(hasReviewedSourceBundle({ ...entry, sourceBundle: ids }, config), false);
  }
  config.sources.recipes.includesBuildInstructions = 'true';
  assert.equal(hasReviewedSourceBundle(entry, config), false);
  assert.equal(hasReviewedSourceBundle({ sourceBundle: 'recipes' }, config), false);
});

test('source asset names cannot escape the release directory', () => {
  for (const filename of ['../outside.tar.gz', '..\\outside.tar.gz', '.', '..', 'bad\nname']) {
    assert.throws(() => sourceAssetName('source', { filename }), /plain filenames/);
  }
  assert.equal(sourceAssetName('ffmpeg', { filename: 'ffmpeg-8.tar.xz' }), 'source-ffmpeg-8.tar.xz');
});
