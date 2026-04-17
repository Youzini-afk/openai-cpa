console.log('[NW机组] Content script loaded on', location.href);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (
    message.type === 'NW_STEP_REGISTER_FILL' ||
    message.type === 'NW_STEP_REGISTER_SUBMIT' ||
    message.type === 'NW_STEP_LOGIN' ||
    message.type === 'NW_STEP_CREATE_KEY'
  ) {
    resetStopState();
    handleNwCommand(message).then((result) => {
      sendResponse({ ok: true, ...(result || {}) });
    }).catch(err => {
      if (isStopError(err)) {
        log(`[NW机组迫降] 任务已强行终止。`, 'warn');
        sendResponse({ stopped: true, error: err.message });
        return;
      }
      log(`[NW机组异常] ${err.message}`, 'error');
      sendResponse({ error: err.message });
    });
    return true;
  }
  if (message.type === 'PING') {
    sendResponse({ ok: true, source: 'nw-register' });
    return true;
  }
  if (message.type === 'CHECK_HEALTH') {
    const errorText = getNwErrorText();
    if (errorText) {
      sendResponse({ healthy: false, reason: `页面错误: ${errorText}` });
      return true;
    }
    sendResponse({ healthy: true });
    return true;
  }
});

async function handleNwCommand(message) {
  switch (message.type) {
    case 'NW_STEP_REGISTER_FILL':
      return await nwStepRegisterFill(message.payload);
    case 'NW_STEP_REGISTER_SUBMIT':
      return await nwStepRegisterSubmit();
    case 'NW_STEP_LOGIN':
      return await nwStepLogin(message.payload);
    case 'NW_STEP_CREATE_KEY':
      return await nwStepCreateKey();
    default:
      throw new Error(`[NW系统错误] 未知指令: ${message.type}`);
  }
}

const NW_REGISTER_ERROR_PATTERNS = [
  /already\s+(exists|registered)/i,
  /email.*already/i,
  /已注册/i,
  /已经存在/i,
  /invalid\s+turnstile/i,
  /验证失败/i,
];

function getNwErrorText() {
  const selectors = [
    '[class*="error"]',
    '[class*="alert"]',
    '[role="alert"]',
    '.text-red-500',
    '.text-red-600',
    '.text-danger',
    '[class*="danger"]',
  ];
  for (const selector of selectors) {
    const els = document.querySelectorAll(selector);
    for (const el of els) {
      if (!isVisibleElement(el)) continue;
      const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (text && NW_REGISTER_ERROR_PATTERNS.some(p => p.test(text))) {
        return text;
      }
    }
  }
  return '';
}

function isVisibleElement(el) {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== 'none'
    && style.visibility !== 'hidden'
    && rect.width > 0
    && rect.height > 0;
}

function isTurnstileReady() {
  const iframe = document.querySelector('iframe[src*="challenges.cloudflare.com"]');
  const responseInput = document.querySelector('[name="cf-turnstile-response"]');
  if (responseInput && responseInput.value && responseInput.value.length > 20) {
    return { ready: true, token: responseInput.value };
  }
  const successMark = document.querySelector('.cf-turnstile[data-success="true"]');
  if (successMark) {
    return { ready: true, token: responseInput?.value || '' };
  }
  if (iframe) {
    return { ready: false, hasIframe: true };
  }
  return { ready: false, hasIframe: false };
}

async function waitForTurnstile(timeout = 30000) {
  const start = Date.now();
  let lastLogTime = 0;
  while (Date.now() - start < timeout) {
    throwIfStopped();
    const status = isTurnstileReady();
    if (status.ready) {
      log('[NW机组] Turnstile 已通过');
      return status;
    }
    const now = Date.now();
    if (now - lastLogTime > 5000) {
      lastLogTime = now;
      log(`[NW机组] 等待 Turnstile 验证... (${Math.round((now - start) / 1000)}s)`);
    }
    await sleep(1000);
  }
  throw new Error('turnstile_blocked: Turnstile 验证超时，未能在真实浏览器中自动通过');
}

async function nwStepRegisterFill(payload) {
  const { email, password, name } = payload;
  if (!email) throw new Error('NW机组：邮箱为空');

  log(`[NW注册] 开始填写注册表单: ${email}`);

  const nameInput = await waitForElement('#name, input[name="name"], input[id="name"]', 10000);
  await humanPause(400, 900);
  fillInput(nameInput, name || 'Test User');
  log('[NW注册] 名称已填写');

  const emailInput = await waitForElement('#email, input[name="email"], input[id="email"], input[type="email"]', 10000);
  await humanPause(400, 900);
  fillInput(emailInput, email);
  log('[NW注册] 邮箱已填写');

  const passwordInput = await waitForElement('#password, input[name="password"], input[id="password"], input[type="password"]', 10000);
  await humanPause(400, 900);
  fillInput(passwordInput, password);
  log('[NW注册] 密码已填写');

  const confirmInput = document.querySelector('#confirm_password, input[name="confirm_password"], input[id="confirm_password"]');
  if (confirmInput) {
    await humanPause(300, 700);
    fillInput(confirmInput, password);
    log('[NW注册] 确认密码已填写');
  }

  const companyInput = document.querySelector('#company, input[name="company"], input[id="company"]');
  if (companyInput) {
    await humanPause(300, 600);
    fillInput(companyInput, '');
    log('[NW注册] 公司名已留空');
  }

  const termsCheckbox = document.querySelector('#terms, input[name="terms"], input[type="checkbox"][id="terms"]');
  if (termsCheckbox && !termsCheckbox.checked) {
    await humanPause(300, 600);
    termsCheckbox.click();
    termsCheckbox.dispatchEvent(new Event('change', { bubbles: true }));
    log('[NW注册] 条款已勾选');
  }

  log('[NW注册] 表单填写完毕，等待 Turnstile...');
  const turnstileStatus = await waitForTurnstile(30000);
  log(`[NW注册] Turnstile 状态: 就绪=${turnstileStatus.ready}`);

  return { filled: true, turnstileReady: turnstileStatus.ready };
}

async function nwStepRegisterSubmit() {
  log('[NW注册] 正在提交注册表单...');

  const submitBtn = document.querySelector('button[type="submit"]')
    || await waitForElementByText('button', /create\s*account|注册|sign\s*up/i, 5000).catch(() => null);

  if (!submitBtn) {
    throw new Error('未找到注册提交按钮');
  }

  await humanPause(500, 1200);
  simulateClick(submitBtn);
  log('[NW注册] 提交按钮已点击');

  await sleep(3000);

  const errorText = getNwErrorText();
  if (errorText) {
    if (/turnstile/i.test(errorText)) {
      throw new Error('turnstile_blocked: ' + errorText);
    }
    if (/already/i.test(errorText) || /已注册/i.test(errorText)) {
      throw new Error('register_failed: 邮箱已注册 - ' + errorText);
    }
    throw new Error('register_failed: ' + errorText);
  }

  const currentUrl = location.href;
  const pageText = (document.body?.innerText || '').toLowerCase();

  if (currentUrl.includes('/auth/verify') || currentUrl.includes('/auth/check-email')) {
    log('[NW注册] 注册成功，需要邮箱验证');
    return { submitted: true, needsVerify: true };
  }

  if (currentUrl.includes('/dashboard') || currentUrl.includes('/auth/login')) {
    log('[NW注册] 注册成功，已跳转');
    return { submitted: true, needsVerify: currentUrl.includes('/auth/login') };
  }

  if (pageText.includes('check your email') || pageText.includes('验证') || pageText.includes('verify')) {
    log('[NW注册] 注册成功，需要邮箱验证 (页面文本判断)');
    return { submitted: true, needsVerify: true };
  }

  log('[NW注册] 表单已提交，状态待确认');
  return { submitted: true, needsVerify: true };
}

async function nwStepLogin(payload) {
  const { email, password } = payload;
  if (!email) throw new Error('NW登录：邮箱为空');

  log(`[NW登录] 开始登录: ${email}`);

  if (!location.pathname.includes('/auth/login')) {
    log('[NW登录] 当前不在登录页，等待页面加载...');
  }

  const emailInput = await waitForElement('#email, input[name="email"], input[id="email"], input[type="email"]', 10000);
  await humanPause(400, 900);
  fillInput(emailInput, email);
  log('[NW登录] 邮箱已填写');

  const passwordInput = await waitForElement('#password, input[name="password"], input[id="password"], input[type="password"]', 10000);
  await humanPause(400, 900);
  fillInput(passwordInput, password);
  log('[NW登录] 密码已填写');

  const turnstileOnLogin = document.querySelector('.cf-turnstile, iframe[src*="challenges.cloudflare.com"]');
  if (turnstileOnLogin) {
    log('[NW登录] 检测到登录页 Turnstile，等待通过...');
    await waitForTurnstile(30000);
  }

  const submitBtn = document.querySelector('button[type="submit"]')
    || await waitForElementByText('button', /log\s*in|sign\s*in|登录/i, 5000).catch(() => null);

  if (submitBtn) {
    await humanPause(500, 1200);
    simulateClick(submitBtn);
    log('[NW登录] 登录按钮已点击');
  }

  await sleep(4000);

  const errorText = getNwErrorText();
  if (errorText) {
    throw new Error('login_failed: ' + errorText);
  }

  const currentUrl = location.href;
  if (currentUrl.includes('/dashboard') || currentUrl.includes('/keys')) {
    log('[NW登录] 登录成功，已进入仪表盘');
    return { loggedIn: true };
  }

  if (currentUrl.includes('/auth/verify') || currentUrl.includes('/auth/check-email')) {
    throw new Error('login_failed: 邮箱尚未验证');
  }

  if (currentUrl.includes('/auth/login')) {
    const pageText = (document.body?.innerText || '').toLowerCase();
    if (pageText.includes('invalid') || pageText.includes('incorrect') || pageText.includes('错误')) {
      throw new Error('login_failed: 邮箱或密码错误');
    }
  }

  log('[NW登录] 登录状态待确认');
  return { loggedIn: true, assumed: true };
}

async function nwStepCreateKey() {
  log('[NW Key] 开始创建 API Key...');

  if (!location.pathname.includes('/dashboard/keys') && !location.pathname.includes('/dashboard')) {
    log('[NW Key] 当前不在 Key 管理页，等待...');
    await sleep(2000);
  }

  await sleep(2000);

  let apiKey = '';

  const createInput = document.querySelector('#name, input[name="name"], input[id="name"], input[placeholder*="name" i], input[placeholder*="key" i]');
  if (createInput) {
    await humanPause(400, 900);
    fillInput(createInput, 'auto-reg');
    log('[NW Key] Key 名称已填写');

    const createBtn = document.querySelector('button[type="submit"]')
      || await waitForElementByText('button', /create|生成|添加|add/i, 5000).catch(() => null);

    if (createBtn) {
      await humanPause(500, 1200);
      simulateClick(createBtn);
      log('[NW Key] 创建按钮已点击');
    }

    await sleep(3000);

    const skMatch = document.body?.innerText?.match(/(sk-[a-zA-Z0-9]{20,})/);
    if (skMatch) {
      apiKey = skMatch[1];
      log(`[NW Key] API Key 已创建: ${apiKey.slice(0, 8)}...`);
      return { apiKey };
    }
  }

  const existingKeys = document.body?.innerText?.match(/(sk-[a-zA-Z0-9]{20,})/g);
  if (existingKeys && existingKeys.length > 0) {
    apiKey = existingKeys[0];
    log(`[NW Key] 使用现有 API Key: ${apiKey.slice(0, 8)}...`);
    return { apiKey };
  }

  const keyElements = document.querySelectorAll('code, [class*="key"], [class*="token"], td, span');
  for (const el of keyElements) {
    const match = (el.textContent || '').match(/(sk-[a-zA-Z0-9]{20,})/);
    if (match) {
      apiKey = match[1];
      log(`[NW Key] 从 DOM 中提取 API Key: ${apiKey.slice(0, 8)}...`);
      return { apiKey };
    }
  }

  log('[NW Key] 未能从页面提取 API Key');
  return { apiKey: '' };
}
