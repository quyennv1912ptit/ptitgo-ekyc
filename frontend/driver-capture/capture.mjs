import { confirmedNameCopy, expiryOption, mismatchCode, mayAutofill } from './workflow.mjs';
import { setupDemo } from './submission.mjs';
import { steps, quality } from './quality.mjs';

import { canAdvance, capturesReady, faceWarning, fields, manualFaceWarning, manualFields,
  missingFields, replaceCapture, technicalIssue, workflowStatus } from './workflow.mjs';

const el = id => document.getElementById(id);
const video = document.querySelector('video');
const preview = document.querySelector('img');
const captures = new Map();
const messages = {
  FACE_MANUAL_CHECK: manualFaceWarning,
  FACE_COUNT_UNCONFIRMED: 'Chưa xác định được đúng một khuôn mặt. Hãy xem lại ảnh.',
  SMALL: 'Ảnh có kích thước nhỏ. Thử chụp gần hơn.',
  DARK: 'Ảnh khá tối. Thử tăng ánh sáng.',
  BRIGHT: 'Ảnh có nhiều vùng trắng sáng. Kiểm tra xem chữ có bị lóa.',
  LOW_DETAIL: 'Ảnh ít chi tiết. Kiểm tra độ nét và lấy nét lại nếu cần.',
};
let current = 0, stream, ticket = 0, opening = false;
const errors = new Map();
let reading = false;
function stopCamera() {
  ticket++;
  opening = false;
  reading = false;
  stream?.getTracks().forEach(track => track.stop());
  stream = undefined;
  video.srcObject = null;
}
function render() {
  const step = steps[current], capture = captures.get(current);
  el('title').textContent = step.label;
  el('guide').textContent = step.selfie
    ? 'Đặt một khuôn mặt rõ trong khung. Nhìn thẳng và tránh ngược sáng.'
    : 'Đặt giấy tờ trong khung. Kiểm tra đủ bốn góc, chữ rõ và không bị lóa.'
      + (step.optional ? ' Chụp mặt này khi cần QR hoặc thông tin bổ sung.' : '');
  el('frame').classList.toggle('selfie', Boolean(step.selfie));
  el('frame').classList.toggle('gplx', current === 2 || current === 3);
  el('view').hidden = !stream && !capture;
  video.hidden = !stream;
  preview.hidden = Boolean(stream) || !capture;
  if (capture) preview.src = capture.url;
  else preview.removeAttribute('src');
  el('frame').hidden = !stream;
  el('shot').hidden = !stream;
  el('close').hidden = !stream && !opening;
  el('camera').disabled = Boolean(stream) || opening;
  el('remove').hidden = !capture;
  const last = current === steps.length - 1;
  const blocked = errors.has(current) || reading || opening || Boolean(stream);
  el('next').disabled = !canAdvance(step, capture, blocked);
  el('next').textContent = last ? 'Nhập thông tin giấy tờ'
    : step.optional && !capture ? 'Bỏ qua mặt sau GPLX' : 'Tiếp theo';
  el('keep').hidden = !errors.has(current) || (!capture && !step.optional);
  el('keep').textContent = capture ? 'Giữ ảnh trước' : 'Bỏ ảnh lỗi và bỏ qua mặt sau GPLX';
  el('ack-label').hidden = !capture?.warning_codes.length;
  el('ack').checked = Boolean(capture?.warning_acknowledged);
  el('warnings').replaceChildren(...(capture?.warning_codes ?? []).map(code => {
    const li = document.createElement('li'); li.textContent = messages[code]; return li;
  }));
  const count = steps.filter((item, i) => !item.optional && captures.has(i)).length;
  el('progress').textContent = count === 4
    ? 'Đã hoàn tất bước chụp (4/4 ảnh bắt buộc). Bạn có thể quay lại từng mục để xem hoặc thay ảnh.'
    : `Đã có ${count}/4 ảnh bắt buộc.`;
  [...el('steps').children].forEach((button, i) => {
    button.textContent = (i + 1) + '. ' + steps[i].label + (captures.has(i) ? ' ✓' : '');
    if (i === current) button.setAttribute('aria-current', 'step');
    else button.removeAttribute('aria-current');
  });
}
function select(i) {
  stopCamera(); current = i; showPanel('capture-panel');
  el('message').textContent = errors.get(i) ?? ''; render();
}
steps.forEach((step, i) => {
  const button = document.createElement('button');
  button.type = 'button'; button.onclick = () => select(i); el('steps').append(button);
});
el('ack').onchange = () => {
  const capture = captures.get(current);
  if (capture) capture.warning_acknowledged = el('ack').checked;
  invalidate();
};
el('keep').onclick = () => { errors.delete(current); select(current); };
el('next').onclick = () => {
  const capture = captures.get(current), step = steps[current];
  if (!canAdvance(step, capture, el('next').disabled)) return;
  if (capture?.warning_codes.length && !capture.warning_acknowledged) {
    el('message').textContent = 'Hãy xem lại cảnh báo và xác nhận hoặc thay ảnh.';
    el('ack').focus(); return;
  }
  if (current < steps.length - 1) { select(current + 1); return; }
  const unresolved = steps.findIndex((item, i) => errors.has(i)
    || (!item.optional && !captures.has(i))
    || (captures.get(i)?.warning_codes.length && !captures.get(i).warning_acknowledged));
  if (unresolved !== -1) {
    select(unresolved);
    el('message').textContent = errors.get(unresolved) ?? 'Hãy thêm ảnh hoặc xác nhận cảnh báo ở bước này.';
    return;
  }
  showPanel('review');
};
el('close').onclick = () => { stopCamera(); render(); };
el('camera').onclick = async () => {
  stopCamera();
  const request = ticket;
  opening = true; render();
  el('message').textContent = 'Đang mở camera…';
  try {
    const candidate = await navigator.mediaDevices.getUserMedia({
      audio: false, video: { facingMode: { ideal: steps[current].selfie ? 'user' : 'environment' },
        width: { ideal: 1280 }, height: { ideal: 720 } },
    });
    if (request !== ticket) { candidate.getTracks().forEach(track => track.stop()); return; }
    stream = candidate;
    video.srcObject = stream;
    await video.play();
    if (request === ticket) el('message').textContent = '';
  } catch {
    if (request === ticket) {
      stopCamera(); el('message').textContent = 'Không mở được camera. Bạn có thể chọn ảnh từ thiết bị.';
    }
  } finally {
    if (request === ticket) opening = false;
    render();
  }
};
async function accept(blob) {
  stopCamera();
  const request = ticket, slot = current;
  reading = true;
  el('message').textContent = 'Đang xem ảnh…';
  render();
  const url = URL.createObjectURL(blob);
  let saved = false;
  try {
    const image = new Image(); image.src = url; await image.decode();
    if (request !== ticket) return;
    const w = image.naturalWidth, h = image.naturalHeight;
    if (technicalIssue(w, h)) {
      errors.set(slot, 'Kích thước ảnh không hợp lệ hoặc vượt 40 megapixel.');
      el('message').textContent = errors.get(slot); return;
    }
    const canvas = document.createElement('canvas');
    const scale = Math.min(1, 224 / Math.max(w, h));
    canvas.width = Math.max(1, Math.round(w * scale));
    canvas.height = Math.max(1, Math.round(h * scale));
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    ctx.drawImage(image, 0, 0, canvas.width, canvas.height);
    const result = quality(ctx.getImageData(0, 0, canvas.width, canvas.height).data,
      canvas.width, canvas.height, w, h);
    if (steps[slot].selfie) {
      let count;
      if (window.FaceDetector) {
        try {
          const detector = new window.FaceDetector({ maxDetectedFaces: 2 });
          count = (await detector.detect(canvas)).length;
        } catch { /* Optional browser capability. */ }
      }
      const warning = faceWarning(count);
      if (warning) result.push(warning);
    }
    if (request !== ticket) return;
    staleSuggestions();
    replaceCapture(captures, slot, { url, blob, warning_codes: result }, URL.revokeObjectURL);
    errors.delete(slot);
    invalidate();
    saved = true;
    el('message').textContent = 'Đã giữ ảnh để xem lại.';
  } catch {
    if (request === ticket) {
      errors.set(slot, 'Không đọc được ảnh. Hãy chọn ảnh khác.');
      el('message').textContent = errors.get(slot);
    }
  } finally {
    if (request === ticket) reading = false;
    if (!saved) URL.revokeObjectURL(url);
    render();
  }
}
el('file').onchange = event => {
  const file = event.target.files[0]; event.target.value = '';
  if (!file) return;
  if (file.size > 20 * 1024 * 1024) {
    stopCamera();
    errors.set(current, 'Ảnh lớn hơn 20 MB. Hãy chọn ảnh nhỏ hơn.');
    el('message').textContent = errors.get(current);
    render(); return;
  }
  void accept(file);
};
el('shot').onclick = () => {
  if (!video.videoWidth) { el('message').textContent = 'Camera đang khởi động. Hãy thử lại.'; return; }
  const request = ticket, canvas = document.createElement('canvas');
  canvas.width = video.videoWidth; canvas.height = video.videoHeight;
  canvas.getContext('2d').drawImage(video, 0, 0);
  canvas.toBlob(blob => { if (blob && request === ticket) void accept(blob); }, 'image/jpeg', .92);
};
el('remove').onclick = () => {
  stopCamera();
  staleSuggestions();
  replaceCapture(captures, current, null, URL.revokeObjectURL);
  errors.delete(current); invalidate(); el('message').textContent = ''; render();
};
function showPanel(panel) {
  for (const id of ['capture-panel', 'review', 'summary']) el(id).hidden = id !== panel;
  if (panel !== 'capture-panel') el(panel + '-title').focus();
}
function invalidate() {
  el('confirm-label').hidden = false;
  el('complete').hidden = false;
  el('submit-status').textContent = '';
  el('confirm').checked = false;
  el('complete').disabled = true;
  el('ready').textContent = '';
  el('summary').dataset.status = 'DRAFT';
  el('summary-photos').replaceChildren();
  el('submit-application').hidden = true;
  el('demo-consent-label').hidden = true;
  el('demo-consent').checked = false;
}
const editedFields = new Set();
let nameCopy = null;
for (const [key, label, kind] of fields) {
  const wrapper = document.createElement('label');
  const input = document.createElement(kind === 'gender' ? 'select' : 'input');
  input.id = key;
  input.autocomplete = 'off';
  if (kind === 'gender') {
    for (const [value, text] of [['', 'Chưa chọn'], ['MALE', 'Nam'], ['FEMALE', 'Nữ']]) {
      input.add(new Option(text, value));
    }
  } else input.type = kind === 'date' ? 'date' : 'text';
  if (key === 'cccd_id') input.inputMode = 'numeric';
  input.oninput = () => { if (key.endsWith("_name")) nameCopy = null; editedFields.add(key); invalidate(); consistency(); };
  wrapper.append(label + (kind === 'gender' ? '' : ' *'), input);
  const hints = document.createElement('div');
  hints.id = key + '-suggestions'; hints.className = 'field-suggestions';
  input.setAttribute('aria-describedby', hints.id);
  el(key.startsWith('cccd_') ? 'cccd-fields' : 'gplx-fields').append(wrapper, hints);
}
function records() {
  return manualFields(Object.fromEntries(fields.map(([key]) => [key, el(key).value])), el('no-expiry').checked);
}
el('no-expiry').onchange = () => { editedFields.add('gplx_expiry'); if (el('no-expiry').checked) { el('gplx_expiry').value = ''; el('gplx_expiry-suggestions').replaceChildren(); } el('gplx_expiry').disabled = el('no-expiry').checked; invalidate(); };
el('review-back').onclick = () => select(steps.length - 1);
el('edit-info').onclick = () => { invalidate(); showPanel('review'); };
el('summarize').onclick = () => {
  const data = records(), missing = missingFields(data);
  if (missing.length) {
    el('review-error').textContent = 'Hãy điền tất cả trường bắt buộc trước khi xem tổng hợp.';
    el(missing[0]).focus(); return;
  }
  el('review-error').textContent = '';
  invalidate();
  for (const [index, step] of steps.entries()) {
    const card = document.createElement('div'), capture = captures.get(index);
    const button = document.createElement('button');
    button.type = 'button'; button.textContent = 'Sửa ảnh ' + step.label;
    button.onclick = () => { invalidate(); select(index); };
    if (capture) {
      const img = document.createElement('img'); img.src = capture.url; img.alt = step.label;
      card.append(img);
      for (const code of capture.warning_codes) {
        const note = document.createElement('p'); note.textContent = messages[code]; card.append(note);
      }
    } else card.append('GPLX sau: đã bỏ qua. ');
    card.append(button); el('summary-photos').append(card);
  }
  el('summary-fields').replaceChildren();
  for (const [key, label] of fields) {
    const term = document.createElement('dt'), value = document.createElement('dd');
    term.textContent = (key.startsWith('cccd_') ? 'CCCD · ' : 'GPLX · ') + label.replace(' (cách nhau bằng dấu phẩy)', '');
    const confirmed = data[key].confirmed_value;
    value.textContent = key === 'gplx_expiry' ? (confirmed.no_expiry ? 'Không thời hạn' : confirmed.expiry_date)
      : ({ MALE: 'Nam', FEMALE: 'Nữ' })[key === 'cccd_gender' ? confirmed : '']
      ?? (Array.isArray(confirmed) ? confirmed.join(', ') : confirmed || 'Chưa nhập');
    el('summary-fields').append(term, value);
  }
  showPanel('summary');
};
el('confirm').onchange = () => {
  el('complete').disabled = !el('confirm').checked;
  el('summary').dataset.status = 'DRAFT'; el('ready').textContent = '';
  el('submit-application').hidden = true;
  el('demo-consent-label').hidden = true;
  el('demo-consent').checked = false;
};
el('complete').onclick = () => {
  if (errors.size || workflowStatus(el('confirm').checked, captures, records()) !== 'READY_TO_SUBMIT') return;
  el('summary').dataset.status = 'READY_TO_SUBMIT';
  el('submit-application').hidden = false;
  el('demo-consent-label').hidden = false;
  el('ready').textContent = 'Sẵn sàng nộp hồ sơ. Thông tin đang giữ trong tab, chưa được gửi.';
};
window.addEventListener('pagehide', () => { stopCamera(); render(); });
render();

setupDemo({
  captures, records, showSuggestions, clearSuggestions,
  nameCopyConfirmation: () => nameCopy,
  ready: () => !errors.size && el('summary').dataset.status === 'READY_TO_SUBMIT'
    && workflowStatus(el('confirm').checked, captures, records()) === 'READY_TO_SUBMIT',
  restore(details, images) {
    editedFields.clear();
    staleSuggestions();
    el('extract-consent').checked = false;
    for (let i = 0; i < steps.length; i++) {
      const image = images.get(i);
      replaceCapture(captures, i, image ? { ...image, url: URL.createObjectURL(image.blob) } : null,
        URL.revokeObjectURL);
    }
    for (const [key] of fields) {
      const value = key === 'gplx_expiry' ? details.expiry_date : details[key];
      el(key).value = Array.isArray(value) ? value.join(', ') : value === 'NO_EXPIRY' ? '' : value ?? '';
    }
    el('no-expiry').checked = details.no_expiry === true;
    el('gplx_expiry').disabled = el('no-expiry').checked;
    errors.clear(); invalidate(); consistency(); select(0);
  },
});

function staleSuggestions() {
  nameCopy = null;
  el("name-checked").checked = false;
  el("copy-name").disabled = true;
  if (el('extract-status').textContent) {
    el('extract-status').textContent = 'Ảnh đã thay đổi. Hãy đọc lại nếu cần. Thông tin đã nhập vẫn được giữ.';
  }
  clearSuggestions();
}
function consistency() {
  const code = mismatchCode(Object.fromEntries(
    ['cccd_name', 'gplx_name', 'cccd_dob', 'gplx_dob'].map(key => [key, el(key).value]),
  ));
  el('consistency').textContent = {
    DIACRITIC_MISMATCH: 'Họ tên khác dấu tiếng Việt. OCR có thể đã đọc sai dấu.',
    NAME_MISMATCH: 'Họ tên trên CCCD và GPLX chưa khớp. Hãy đối chiếu hai giấy tờ.',
    DOB_MISMATCH: 'Ngày sinh trên CCCD và GPLX chưa khớp. Hãy đối chiếu hai giấy tờ.',
    NAME_AND_DOB_MISMATCH: 'Họ tên và ngày sinh trên CCCD và GPLX chưa khớp. Hãy đối chiếu hai giấy tờ.',
  }[code] ?? '';
  const dobA = el('cccd_dob').value, dobB = el('gplx_dob').value;
  if (code === 'DIACRITIC_MISMATCH' && dobA && dobB && dobA !== dobB) {
    el('consistency').textContent += ' Ngày sinh trên hai giấy tờ cũng chưa khớp.';
  }
  el('name-comparison').hidden = code !== 'DIACRITIC_MISMATCH';
  el('name-values').textContent = code === 'DIACRITIC_MISMATCH'
    ? 'CCCD · ' + el('cccd_name').value + ' / GPLX · ' + el('gplx_name').value : '';
  el('name-checked').checked = false;
  el('copy-name').disabled = true;

}
el('name-checked').onchange = () => { el('copy-name').disabled = !el('name-checked').checked; };
el('copy-name').onclick = () => {
  const choice = confirmedNameCopy(el('cccd_name').value, el('gplx_name').value, el('name-checked').checked);
  if (!choice) return;
  nameCopy = choice.previous;
  el('gplx_name').value = choice.value;
  editedFields.add('gplx_name');
  invalidate();
  consistency();
  el('gplx_name-suggestions').textContent = 'Đã dùng họ tên CCCD theo xác nhận của bạn.';
};
function clearSuggestions() {
  for (const [key] of fields) el(key + '-suggestions').replaceChildren();
}
function showSuggestions(observations) {
  clearSuggestions();
  for (const [key] of fields) {
    const row = el(key + '-suggestions'), input = el(key);
    const values = new Map();
    for (const observation of observations) {
      for (const source of ['qr', 'ocr']) {
        const value = observation[source]?.values[key];
        if (value == null) continue;
        const display = key === 'gplx_expiry' ? expiryOption(value)
          : Array.isArray(value) ? value.join(', ') : value;
        if (display === null) continue;
        if (!values.has(display)) values.set(display, new Set());
        values.get(display).add(source.toUpperCase());
      }
    }
    if (!values.size) {
      if (input.value) row.textContent = 'Thông tin đã nhập.';
      if (key === 'gplx_expiry' && !input.value && !el('no-expiry').checked) {
        const issue = observations.find(item => item.slot === 'gplx_front')?.ocr?.field_statuses?.[key];
        const explanation = {
          OCR_MISSING: 'OCR chưa nhận ra nhãn hạn GPLX.',
          PARSE_FAILED: 'Có nhãn hạn GPLX nhưng chưa ghép được giá trị.',
          VALIDATION_FAILED: 'Giá trị hạn GPLX đọc được chưa đúng định dạng.',
        }[issue] ?? 'Chưa đọc được hạn GPLX.';
        row.textContent = explanation + ' Nhập ngày hoặc chọn Không thời hạn theo giấy phép.';
      }
      continue;
    }
    const apply = value => {
      if (key.endsWith('_name')) nameCopy = null;
      if (key === 'gplx_expiry') {
        el('no-expiry').checked = value === 'NO_EXPIRY';
        input.disabled = el('no-expiry').checked;
      }
      input.value = key === 'gplx_expiry' && value === 'NO_EXPIRY' ? '' : value;
      invalidate();
      consistency();
    };
    if (!values.has('NO_EXPIRY') && mayAutofill(values, input.value, editedFields.has(key),
      key === 'gplx_expiry' && el('no-expiry').checked)) {
      const [value, sources] = values.entries().next().value;
      apply(value);
      if (input.value || (key === 'gplx_expiry' && el('no-expiry').checked)) {
        row.textContent = 'Đã điền từ ' + [...sources].join('/') + ' · hãy đối chiếu giấy tờ.';
        continue;
      }
    }
    if (values.size === 1 && values.has('NO_EXPIRY')) row.append('Hãy xác nhận theo giấy phép. ');
    if (values.size > 1) row.append('Nguồn đọc cho kết quả khác nhau. Hãy đối chiếu ảnh. ');
    for (const [value, sources] of values) {
      const button = document.createElement('button');
      button.type = 'button';
      const display = key === 'gplx_expiry' && value === 'NO_EXPIRY' ? 'Không thời hạn'
        : key === 'cccd_gender' ? ({ MALE: 'Nam', FEMALE: 'Nữ' })[value] ?? value : value;
      button.textContent = 'Dùng ' + [...sources].join('/') + ' · ' + display;
      button.onclick = () => {
        editedFields.add(key); apply(value);
        row.textContent = 'Đã chọn từ ' + [...sources].join('/') + ' · hãy đối chiếu giấy tờ.';
      };
      row.append(button);
    }
  }
  consistency();
}
