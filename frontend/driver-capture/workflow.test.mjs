import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { steps } from './quality.mjs';
import { canAdvance, capturesReady, faceWarning, manualFaceWarning, manualFields,
  missingFields, replaceCapture, technicalIssue, workflowStatus } from './workflow.mjs';

function photos() {
  return new Map([0, 1, 2, 4].map(i => [i, { url: 'blob:' + i, warning_codes: [] }]));
}

test('technical failures and missing required images block; advisory does not disable next', () => {
  assert.equal(canAdvance(steps[0], null, false), false);
  assert.equal(canAdvance(steps[0], photos().get(0), true), false);
  assert.equal(technicalIssue(1, 400), true);
  assert.equal(technicalIssue(320, 240), false);
  assert.equal(canAdvance(steps[0], { warning_codes: ['DARK'] }, false), true);
});

test('optional back can be skipped; warnings need acknowledgment', () => {
  const captures = photos();
  assert.equal(canAdvance(steps[3], undefined, false), true);
  assert.equal(capturesReady(captures), true);
  captures.get(4).warning_codes = ['FACE_MANUAL_CHECK'];
  assert.equal(capturesReady(captures), false);
  captures.get(4).warning_acknowledged = true;
  assert.equal(capturesReady(captures), true);
});

test('unsupported face detection has a manual instruction, not a positive result', () => {
  assert.equal(faceWarning(undefined), 'FACE_MANUAL_CHECK');
  assert.match(manualFaceWarning, /tự kiểm tra/);
  assert.equal(faceWarning(2), 'FACE_COUNT_UNCONFIRMED');
  assert.equal(faceWarning(1), null);
});

test('replacing or removing an image releases its URL and resets acknowledgment', () => {
  const captures = photos(), revoked = [];
  replaceCapture(captures, 0, { url: 'blob:new', warning_codes: ['DARK'] }, url => revoked.push(url));
  assert.deepEqual(revoked, ['blob:0']);
  assert.equal(captures.get(0).warning_acknowledged, false);
  replaceCapture(captures, 0, null, url => revoked.push(url));
  assert.deepEqual(revoked, ['blob:0', 'blob:new']);
  assert.equal(capturesReady(captures), false);
});

test('manual edits, leading zeros, several classes and no-expiry survive local confirmation', () => {
  const values = { cccd_name: 'Tên mẫu', cccd_id: '001', cccd_dob: '1990-01-01',
    gplx_number: '002', gplx_name: 'Tên mẫu', gplx_dob: '1990-01-01', gplx_classes: 'a1, B2, A1' };
  const draft = manualFields(values, false);
  assert.deepEqual(missingFields(draft), ['gplx_expiry']);
  assert.equal(workflowStatus(true, photos(), draft), 'DRAFT');
  const updated = manualFields({ ...values, cccd_name: 'Tên đã sửa', gplx_number: '003' }, true);
  assert.equal(updated.cccd_name.confirmed_value, 'Tên đã sửa');
  assert.equal(updated.cccd_id.confirmed_value, '001');
  assert.equal(updated.gplx_number.confirmed_value, '003');
  assert.deepEqual(updated.gplx_classes.confirmed_value, ['A1', 'B2']);
  assert.deepEqual(updated.gplx_expiry.confirmed_value, { no_expiry: true, expiry_date: null });
  assert.equal(updated.gplx_number.extracted_value, null);
  assert.equal(updated.gplx_number.extracted_source, 'manual');
  assert.equal(workflowStatus(false, photos(), updated), 'DRAFT');
  assert.equal(workflowStatus(true, photos(), updated), 'READY_TO_SUBMIT');
});

test('page limits outgoing connections and form submission; completion copy describes a local draft', () => {
  const html = readFileSync(new URL('./index.html', import.meta.url), 'utf8');
  const js = readFileSync(new URL('./capture.mjs', import.meta.url), 'utf8');
  assert.match(html, /connect-src 'self'/);
  assert.match(html, /form-action 'none'/);
  assert.doesNotMatch(js, /fetch\(|XMLHttpRequest|sendBeacon|localStorage|sessionStorage|Đã gửi admin/i);
  assert.match(js, /Thông tin đang giữ trong tab, chưa được gửi/);
});

test('autofill preserves edits and defers conflicts', async () => {
  const { mayAutofill } = await import('./workflow.mjs');
  assert.equal(mayAutofill(new Set(['Trần Ánh Dương']), '', false), true);
  assert.equal(mayAutofill(new Set(['Trần Ánh Dương']), '', true), false);
  assert.equal(mayAutofill(new Set(['A', 'B']), '', false), false);
  assert.equal(mayAutofill(new Set(['2030-12-31']), '', false, true), false);
});

test('replacing an image invalidates in-flight results', async () => {
  const { sameExtractionImages } = await import('./workflow.mjs');
  const original = {}, captures = new Map([[0, original]]);
  assert.equal(sameExtractionImages([[0, original]], captures), true);
  captures.set(0, {});
  assert.equal(sameExtractionImages([[0, original]], captures), false);
});

test('document name comparison keeps Vietnamese diacritics', async () => {
  const { documentConflicts } = await import('./workflow.mjs');
  const data = { cccd_name: 'Trần Ánh Dương', gplx_name: 'Trần Anh Dương', cccd_dob: '', gplx_dob: '' };
  assert.deepEqual(documentConflicts(data), ['name']);
  assert.deepEqual(documentConflicts({ ...data, gplx_name: data.cccd_name.normalize('NFD') }), []);
});

test('expiry options distinguish an explicit no-expiry suggestion from missing and invalid', async () => {
  const { expiryOption } = await import('./workflow.mjs');
  assert.equal(expiryOption({ no_expiry: true, expiry_date: null }), 'NO_EXPIRY');
  assert.equal(expiryOption({ no_expiry: false, expiry_date: '2030-12-31' }), '2030-12-31');
  assert.equal(expiryOption(undefined), null);
  assert.equal(expiryOption({ no_expiry: true, expiry_date: '2030-12-31' }), null);
});

test('reports name and birth date mismatches separately', async () => {
  const { mismatchCode } = await import('./workflow.mjs');
  const equal = { cccd_name: 'Tên Mẫu', gplx_name: '  TÊN MẪU ', cccd_dob: '1990-01-01', gplx_dob: '1990-01-01' };
  assert.equal(mismatchCode(equal), null);
  assert.equal(mismatchCode({ ...equal, gplx_name: 'Ten Mau' }), 'DIACRITIC_MISMATCH');
  assert.equal(mismatchCode({ ...equal, gplx_dob: '1990-01-02' }), 'DOB_MISMATCH');
  assert.equal(mismatchCode({ ...equal, gplx_name: 'Ten Khac', gplx_dob: '1990-01-02' }), 'NAME_AND_DOB_MISMATCH');
});

test('name comparison preserves accents and copies only on explicit confirmation', async () => {
  const { compareNames, confirmedNameCopy } = await import('./workflow.mjs');
  const left = 'Đỗ Ánh', right = 'Do Anh';
  assert.equal(compareNames(left, '  ĐỖ   ÁNH '.normalize('NFD')), 'EXACT_MATCH');
  assert.equal(compareNames(left, right), 'DIACRITIC_MISMATCH');
  assert.equal(compareNames(left, 'Đỗ An'), 'NAME_MISMATCH');
  assert.equal(confirmedNameCopy(left, right, false), null);
  assert.equal(confirmedNameCopy(left, 'Đỗ An', true), null);
  assert.deepEqual(confirmedNameCopy(left, right, true), { value: left, previous: right });
});
