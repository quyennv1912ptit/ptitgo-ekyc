import { sameExtractionImages } from './workflow.mjs';
const slots = ['cccd_front', 'cccd_back', 'gplx_front', 'gplx_back', 'selfie'];
const labels = ['CCCD trước', 'CCCD sau', 'GPLX trước', 'GPLX sau', 'Selfie'];
const base = '/api/applications';
const extractionMessages = {
  QR_NOT_FOUND: 'Không tìm thấy QR',
  QR_DECODE_FAILED: 'Không giải mã được QR',
  QR_FOUND_UNSUPPORTED: 'Định dạng QR chưa được hỗ trợ',
  QR_FOUND_SUPPORTED: 'Đọc được QR',
  OCR_NO_TEXT: 'EasyOCR không trả về dòng chữ',
  OCR_NO_FIELDS: 'EasyOCR trả về chữ nhưng parser chưa lấy được trường',
  OCR_EMPTY: 'OCR chưa trích xuất được trường',
  OCR_UNSUPPORTED: 'Mặt này chưa hỗ trợ OCR',
  OCR_READ: 'OCR đã tạo đề xuất',
  OCR_TIMEOUT: 'OCR quá thời gian xử lý',
  OCR_FAILED: 'OCR gặp lỗi khi xử lý',
};

export async function request(path, token, options = {}, transport = fetch) {
  const response = await transport(path, {
    ...options,
    headers: { ...options.headers, Authorization: 'Bearer ' + token },
    cache: 'no-store', redirect: 'error',
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new Error(typeof error.detail === 'string' ? error.detail : 'Máy chủ từ chối yêu cầu.');
  }
  return response;
}

export function setupDemo(workflow) {
  const el = id => document.getElementById(id);
  let token = '', role = '', applicationId, revision = 0, retryKey, sending = false;
  let view = 0, extracting = false;
  const detailUrls = [];
  const call = (path, options) => request(path, token, options);
  const json = async (path, options) => (await call(path, options)).json();
  const button = (label, run) => {
    const control = document.createElement('button');
    control.type = 'button'; control.textContent = label; control.onclick = run;
    return control;
  };
  const status = (message) => { el('account-status').textContent = message; };
  function clearDetail() {
    view++;
    for (const url of detailUrls.splice(0)) URL.revokeObjectURL(url);
    el('application-detail').replaceChildren();
  }
  async function refresh() {
    const rows = await json(base);
    el('applications').replaceChildren(...rows.map(row => button(
      row.id + ' · ' + row.status, () => void open(row.id).catch(error => status(error.message)),
    )));
  }
  async function open(id) {
    clearDetail();
    const generation = view, detail = await json(base + '/' + id);
    if (generation !== view) return;
    const panel = el('application-detail'), heading = document.createElement('h3');
    heading.textContent = detail.status + (detail.reason ? ' · ' + detail.reason : '');
    panel.append(heading);
    const text = document.createElement('pre');
    text.textContent = 'Thông tin tài xế tự nhập\n' + JSON.stringify(detail.details, null, 2);
    panel.append(text);
    if (role === 'admin' && detail.extraction_history?.length) {
      const section = document.createElement('details'), title = document.createElement('summary');
      title.textContent = 'Nguồn QR/OCR/manual, cảnh báo và lịch sử thay đổi';
      const audit = document.createElement('pre');
      audit.textContent = JSON.stringify(detail.extraction_history, null, 2);
      section.append(title, audit); panel.append(section);
    }
    const images = new Map();
    for (const [i, slot] of slots.entries()) {
      const quality = detail.captures[slot];
      if (!quality) continue;
      const blob = await (await call(base + '/' + id + '/captures/' + slot)).blob();
      if (generation !== view) return;
      images.set(i, { blob, warning_codes: quality.client_warning_codes ?? [] });
      const url = URL.createObjectURL(blob), image = document.createElement('img');
      detailUrls.push(url); image.src = url; image.alt = labels[i];
      const description = document.createElement('p');
      description.textContent = labels[i] + ' · ' + quality.width + '×' + quality.height
        + ' · ' + [...quality.warning_codes, ...quality.client_warning_codes].join(', ');
      panel.append(description, image);
    }
    if (role === 'driver' && ['DRAFT', 'NEEDS_RESUBMISSION'].includes(detail.status)) {
      panel.append(button('Chỉnh sửa và nộp lại', () => {
        if (sending || extracting) return;
        applicationId = id; revision = detail.revision; retryKey = undefined;
        workflow.restore(detail.details ?? {}, images);
        clearDetail();
      }));
    }
    if (role === 'admin' && detail.status === 'PENDING_REVIEW') {
      const reason = document.createElement('textarea');
      reason.placeholder = 'Lý do'; reason.setAttribute('aria-label', 'Lý do quyết định');
      reason.maxLength = 500; panel.append(reason);
      for (const [action, label] of [
        ['APPROVED', 'Duyệt'], ['NEEDS_RESUBMISSION', 'Yêu cầu bổ sung'], ['REJECTED', 'Từ chối'],
      ]) {
        panel.append(button(label, async event => {
          event.currentTarget.disabled = true;
          try {
            const result = await json(base + '/' + id + '/decision', {
              method: 'POST', headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ action, reason: reason.value, revision: detail.revision }),
            });
            status('Đã lưu quyết định ' + result.status);
            clearDetail(); await refresh();
          } catch (error) {
            status(error.message);
            for (const input of panel.querySelectorAll('button')) input.disabled = false;
          }
        }));
      }
    }
  }
  el('login').onclick = async () => {
    if (sending || extracting) return;
    const candidate = el('demo-token').value.trim();
    el('login').disabled = true;
    try {
      const identity = await (await request('/api/me', candidate)).json();
      const switching = Boolean(token) && token !== candidate;
      token = candidate; role = identity.role; el('demo-token').value = '';
      el('refresh').hidden = false;
      status(role === 'admin' ? 'Đã đăng nhập admin demo.' : 'Đã đăng nhập tài xế demo.');
      clearDetail();
      if (role === 'admin') {
        workflow.restore({}, new Map());
        for (const id of ['capture-panel', 'review', 'summary']) el(id).hidden = true;
      } else {
        applicationId = undefined; revision = 0; retryKey = undefined;
        if (switching) workflow.restore({}, new Map());
      }
      await refresh();
    } catch (error) { status(error.message); }
    finally { el('login').disabled = false; }
  };
  el('refresh').onclick = () => void refresh().catch(error => status(error.message));
  el('extract').onclick = async () => {
    if (sending || extracting) return;
    const status = el('extract-status');
    if (role !== 'driver' || !el('extract-consent').checked) {
      status.textContent = 'Đăng nhập tài xế và xác nhận xử lý ảnh tổng hợp trước khi đọc.';
      return;
    }
    applicationId ??= crypto.randomUUID();
    const path = base + '/' + applicationId;
    const selected = [...workflow.captures].filter(([index]) => index < 4);
    const observations = [], outcomes = [];
    workflow.clearSuggestions();
    extracting = true; el('extract').disabled = true;
    try {
      await json(path, { method: 'PUT' });
      for (const [index, capture] of selected) {
        status.textContent = 'Đang đọc ' + labels[index] + '…';
        const started = performance.now();
        try {
          const observation = await json(path + '/extract/' + slots[index], {
            method: 'POST', headers: { 'Content-Type': capture.blob.type,
              'X-Synthetic-Demo': 'confirmed' }, body: capture.blob,
          });
          observations.push({ ...observation, slot: slots[index] });
          const reasons = [observation.qr.status, observation.ocr.status]
            .map(code => extractionMessages[code] ?? code);
          outcomes.push(labels[index] + ': ' + reasons.join(' / '));
        } catch (error) {
          outcomes.push(labels[index] + ': ' + (error instanceof TypeError
            ? 'Không kết nối được máy chủ demo.' : error.message));
        }
        // Match the server's one-second interval between extraction starts.
        if (index !== selected.at(-1)[0]) {
          const delay = Math.max(0, 1100 - (performance.now() - started));
          if (delay) await new Promise(resolve => setTimeout(resolve, delay));
        }
        if (!sameExtractionImages(selected, workflow.captures)) {
          status.textContent = 'Ảnh đã thay đổi. Hãy đọc lại nếu cần.';
          return;
        }
      }
      workflow.showSuggestions(observations);
      status.textContent = outcomes.join(' · ')
        + '. Bạn có thể chọn đề xuất hoặc nhập tay.';
    } catch {
      status.textContent = 'Không đọc được ảnh. Bạn vẫn có thể nhập tay.';
    } finally { extracting = false; el('extract').disabled = false; }
  };
  el('submit-application').onclick = async () => {
    const message = el('submit-status');
    if (sending || extracting || !workflow.ready()) return;
    if (role !== 'driver') { message.textContent = 'Hãy đăng nhập tài xế demo trước khi nộp.'; return; }
    if (!el('demo-consent').checked) { message.textContent = 'Hãy xác nhận chỉ dùng dữ liệu tổng hợp.'; return; }
    const control = el('submit-application');
    sending = true; control.disabled = true;
    message.textContent = 'Đang nộp hồ sơ…';
    const data = new FormData(), warnings = {};
    for (const [i, capture] of workflow.captures) {
      data.append(slots[i], capture.blob, slots[i]);
      warnings[slots[i]] = capture.warning_codes;
    }
    const details = Object.fromEntries(Object.entries(workflow.records())
      .map(([key, row]) => [key, row.confirmed_value]));
    const { gplx_expiry: expiry, ...otherDetails } = details;
    data.set('data', JSON.stringify({ details: { ...otherDetails, ...expiry }, warnings, revision, confirmed: true, synthetic_only: true,
      name_copy_confirmation: workflow.nameCopyConfirmation(), remove_optional: !workflow.captures.has(3) }));
    applicationId ??= crypto.randomUUID();
    retryKey ??= crypto.randomUUID();
    const path = base + '/' + applicationId;
    try {
      await json(path, { method: 'PUT' });
      const result = await json(path + '/submit', {
        method: 'POST', headers: { 'Idempotency-Key': retryKey }, body: data,
      });
      message.textContent = 'Máy chủ đã lưu hồ sơ ' + result.id + ' · ' + result.status;
      control.hidden = true;
      el('demo-consent-label').hidden = true;
      el('confirm-label').hidden = true;
      el('complete').hidden = true;
      applicationId = undefined; retryKey = undefined; revision = 0;
      void refresh().catch(error => status(error.message));
    } catch (error) {
      message.textContent = 'Chưa xác nhận được kết quả nộp. Ảnh và thông tin vẫn trong tab. ' + error.message;
    } finally { sending = false; control.disabled = false; }
  };
  window.addEventListener('pagehide', clearDetail);
}
