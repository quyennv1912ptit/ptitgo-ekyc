import assert from 'node:assert/strict';
import test from 'node:test';
import { request } from './submission.mjs';

test('network failure rejects and preserves the retry request and body', async () => {
  const options = { method: 'POST', headers: { 'Idempotency-Key': 'retry' }, body: new FormData() };
  await assert.rejects(request('/api/applications/demo/submit', 'demo', options, async () => {
    throw new TypeError('Network error');
  }), /Network error/);
  const result = await request('/api/applications/demo/submit', 'demo', options, async (path, init) => {
    assert.match(path, /^\/api\//);
    assert.equal(init.body, options.body);
    assert.equal(init.headers['Idempotency-Key'], 'retry');
    assert.equal(init.headers.Authorization, 'Bearer demo');
    return Response.json({ status: 'PENDING_REVIEW' });
  });
  assert.equal((await result.json()).status, 'PENDING_REVIEW');
});

test('server rejection never becomes a success response', async () => {
  await assert.rejects(request('/api/me', 'demo', {}, async () =>
    Response.json({ detail: 'Denied' }, { status: 401 })), /Denied/);
});
