import { steps } from './quality.mjs';

export const manualFaceWarning = 'Hãy tự kiểm tra ảnh có đúng một khuôn mặt rõ.';

export function faceWarning(count) {
  if (count === undefined) return 'FACE_MANUAL_CHECK';
  return count === 1 ? null : 'FACE_COUNT_UNCONFIRMED';
}

export function technicalIssue(width, height) {
  return width < 2 || height < 2 || width * height > 40_000_000;
}

export function canAdvance(step, capture, blocked) {
  return !blocked && (Boolean(capture) || Boolean(step.optional));
}

export function capturesReady(captures) {
  return steps.every((step, i) => {
    const capture = captures.get(i);
    return capture ? !capture.warning_codes.length || capture.warning_acknowledged : step.optional;
  });
}

export function replaceCapture(captures, index, capture, revoke) {
  const existing = captures.get(index);
  if (existing) revoke(existing.url);
  if (capture) captures.set(index, { ...capture, warning_acknowledged: false });
  else captures.delete(index);
}

export const fields = [
  ['cccd_name', 'Họ và tên'],
  ['cccd_id', 'Số định danh cá nhân'],
  ['cccd_dob', 'Ngày sinh', 'date'],
  ['cccd_gender', 'Giới tính (tùy chọn)', 'gender'],
  ['gplx_number', 'Số GPLX'],
  ['gplx_name', 'Họ và tên'],
  ['gplx_dob', 'Ngày sinh', 'date'],
  ['gplx_classes', 'Hạng GPLX (cách nhau bằng dấu phẩy)'],
  ['gplx_expiry', 'Hạn GPLX', 'date'],
];

export function manualFields(values, noExpiry) {
  return Object.fromEntries(fields.map(([key]) => {
    let value = (values[key] ?? '').trim();
    if (key === 'gplx_classes') value = [...new Set(value.toUpperCase().split(/[,;\s]+/).filter(Boolean))];
    if (key === 'gplx_expiry') value = { no_expiry: noExpiry, expiry_date: noExpiry ? null : value || null };
    return [key, {
      extracted_value: null, extracted_source: 'manual', confirmed_value: value,
      edited_by_user: Array.isArray(value) ? value.length > 0 : Boolean(value), warning_codes: [],
    }];
  }));
}

export function missingFields(records) {
  return fields.map(([key]) => key).filter(key => {
    const value = records[key].confirmed_value;
    if (key === 'gplx_expiry') return !value.no_expiry && !value.expiry_date;
    return key !== 'cccd_gender' && (Array.isArray(value) ? !value.length : !value);
  });
}

export function workflowStatus(confirmed, captures, records) {
  return confirmed && capturesReady(captures) && !missingFields(records).length
    ? 'READY_TO_SUBMIT' : 'DRAFT';
}


export function mayAutofill(options, value, edited, noExpiry = false) {
  return options.size === 1 && !value && !edited && !noExpiry;
}

export function sameExtractionImages(selected, captures) {
  return selected.every(([key, original]) => captures.get(key) === original)
    && [...captures.keys()].filter(key => key < 4).length === selected.length;
}

export function documentConflicts(values) {
  const normalize = value => value.trim().normalize('NFC').replace(/\s+/g, ' ').toLocaleLowerCase('vi');
  return ['name', 'dob'].filter(key => {
    const left = normalize(values['cccd_' + key]), right = normalize(values['gplx_' + key]);
    return left && right && left !== right;
  });
}


export function compareNames(left, right) {
  const normalize = value => value.trim().normalize('NFC').replace(/\s+/g, ' ').toLowerCase();
  const a = normalize(left), b = normalize(right);
  if (!a || !b) return null;
  if (a === b) return 'EXACT_MATCH';
  const fold = value => value.normalize('NFD').replace(/\p{M}/gu, '').replace(/đ/g, 'd');
  return fold(a) === fold(b) ? 'DIACRITIC_MISMATCH' : 'NAME_MISMATCH';
}

export function confirmedNameCopy(cccd, gplx, checked) {
  return checked && compareNames(cccd, gplx) === 'DIACRITIC_MISMATCH'
    ? { value: cccd.trim(), previous: gplx.trim() } : null;
}

export function mismatchCode(values) {
  const name = compareNames(values.cccd_name, values.gplx_name);
  const dob = documentConflicts(values).includes('dob');
  if (name === 'NAME_MISMATCH' && dob) return 'NAME_AND_DOB_MISMATCH';
  if (name === 'DIACRITIC_MISMATCH' || name === 'NAME_MISMATCH') return name;
  return dob ? 'DOB_MISMATCH' : null;
}

export function expiryOption(value) {
  if (value?.no_expiry === true && value.expiry_date === null) return 'NO_EXPIRY';
  if (value?.no_expiry === false && /^\d{4}-\d{2}-\d{2}$/.test(value.expiry_date ?? '')) {
    return value.expiry_date;
  }
  return null;
}
