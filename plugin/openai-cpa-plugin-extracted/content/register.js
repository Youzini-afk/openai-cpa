console.log('[机组] Content script loaded on', location.href);

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (
    message.type === 'EXECUTE_STEP'
    || message.type === 'FILL_CODE'
    || message.type === 'STEP8_FIND_AND_CLICK'
    || message.type === 'PREPARE_LOGIN_CODE'
    || message.type === 'PREPARE_SIGNUP_VERIFICATION'
    || message.type === 'RESEND_VERIFICATION_CODE'
  ) {
    resetStopState();
    handleCommand(message).then((result) => {
      sendResponse({ ok: true, ...(result || {}) });
    }).catch(err => {
      if (isStopError(err)) {
        log(`[机组迫降] 任务已强行终止。`, 'warn');
        sendResponse({ stopped: true, error: err.message });
        return;
      }

      if (message.type === 'STEP8_FIND_AND_CLICK') {
        log(`[降落异常] ${err.message}`, 'error');
        sendResponse({ error: err.message });
        return;
      }

      reportError(message.step, err.message);
      sendResponse({ error: err.message });
    });
    return true;
  }
  if (message.type === 'PING') {
    sendResponse({ ok: true });
    return true;
  }
  if (message.type === 'CHECK_HEALTH') {
    const errorText = getVerificationErrorText();
    if (errorText) {
      sendResponse({ healthy: false, reason: `网页弹出致命错误: ${errorText}` });
      return true;
    }
    if (!isVerificationPageStillVisible()) {
      sendResponse({ healthy: false, reason: `验证码阵地已消失或网页被强制跳转` });
      return true;
    }
    sendResponse({ healthy: true });
    return true;
  }
  
});

async function handleCommand(message) {
  switch (message.type) {
    case 'EXECUTE_STEP':
      switch (message.step) {
        case 2: return await step2_clickRegister();
        case 3: return await step3_fillEmailPassword(message.payload);
        case 5: return await step5_fillNameBirthday(message.payload);
        case 6: return await step6_login(message.payload);
        case 8: return await step8_findAndClick();
        default: throw new Error(`[系统错误] 机组无法识别的指令步骤 ${message.step}`);
      }
    case 'FILL_CODE':
      // Step 4 = signup code, Step 7 = login code (same handler)
      return await fillVerificationCode(message.step, message.payload);
    case 'PREPARE_SIGNUP_VERIFICATION':
      return await prepareSignupVerificationFlow(message.payload);
    case 'PREPARE_LOGIN_CODE':
      return await prepareLoginCodeFlow();
    case 'RESEND_VERIFICATION_CODE':
      return await resendVerificationCode(message.step);
    case 'STEP8_FIND_AND_CLICK':
      return await step8_findAndClick();
  }
}

const VERIFICATION_CODE_INPUT_SELECTOR = [
  'input[name="code"]',
  'input[name="otp"]',
  'input[autocomplete="one-time-code"]',
  'input[type="text"][maxlength="6"]',
  'input[type="tel"][maxlength="6"]',
  'input[aria-label*="code" i]',
  'input[placeholder*="code" i]',
  'input[inputmode="numeric"]',
].join(', ');

const ONE_TIME_CODE_LOGIN_PATTERN = /使用一次性验证码登录|改用(?:一次性)?验证码(?:登录)?|使用验证码登录|一次性验证码|验证码登录|one[-\s]*time\s*(?:passcode|password|code)|use\s+(?:a\s+)?one[-\s]*time\s*(?:passcode|password|code)(?:\s+instead)?|use\s+(?:a\s+)?code(?:\s+instead)?|sign\s+in\s+with\s+(?:email|code)|email\s+(?:me\s+)?(?:a\s+)?code/i;

const RESEND_VERIFICATION_CODE_PATTERN = /重新发送(?:验证码)?|再次发送(?:验证码)?|重发(?:验证码)?|未收到(?:验证码|邮件)|resend(?:\s+code)?|send\s+(?:a\s+)?new\s+code|send\s+(?:it\s+)?again|request\s+(?:a\s+)?new\s+code|didn'?t\s+receive/i;

function isVisibleElement(el) {
  if (!el) return false;
  const style = window.getComputedStyle(el);
  const rect = el.getBoundingClientRect();
  return style.display !== 'none'
    && style.visibility !== 'hidden'
    && rect.width > 0
    && rect.height > 0;
}

function getVerificationCodeTarget() {
  const codeInput = document.querySelector(VERIFICATION_CODE_INPUT_SELECTOR);
  if (codeInput && isVisibleElement(codeInput)) {
    return { type: 'single', element: codeInput };
  }

  const singleInputs = Array.from(document.querySelectorAll('input[maxlength="1"]'))
    .filter(isVisibleElement);
  if (singleInputs.length >= 6) {
    return { type: 'split', elements: singleInputs };
  }

  return null;
}

function getActionText(el) {
  return [
    el?.textContent,
    el?.value,
    el?.getAttribute?.('aria-label'),
    el?.getAttribute?.('title'),
  ]
    .filter(Boolean)
    .join(' ')
    .replace(/\s+/g, ' ')
    .trim();
}

function isActionEnabled(el) {
  return Boolean(el)
    && !el.disabled
    && el.getAttribute('aria-disabled') !== 'true';
}

function findOneTimeCodeLoginTrigger() {
  const candidates = document.querySelectorAll(
    'button, a, [role="button"], [role="link"], input[type="button"], input[type="submit"]'
  );

  for (const el of candidates) {
    if (!isVisibleElement(el)) continue;
    if (el.disabled || el.getAttribute('aria-disabled') === 'true') continue;

    const text = [
      el.textContent,
      el.value,
      el.getAttribute('aria-label'),
      el.getAttribute('title'),
    ]
      .filter(Boolean)
      .join(' ')
      .replace(/\s+/g, ' ')
      .trim();

    if (text && ONE_TIME_CODE_LOGIN_PATTERN.test(text)) {
      return el;
    }
  }

  return null;
}

function findResendVerificationCodeTrigger({ allowDisabled = false } = {}) {
  const candidates = document.querySelectorAll(
    'button, a, [role="button"], [role="link"], input[type="button"], input[type="submit"]'
  );

  for (const el of candidates) {
    if (!isVisibleElement(el)) continue;
    if (!allowDisabled && !isActionEnabled(el)) continue;

    const text = getActionText(el);
    if (text && RESEND_VERIFICATION_CODE_PATTERN.test(text)) {
      return el;
    }
  }

  return null;
}

function isEmailVerificationPage() {
  return /\/email-verification(?:[/?#]|$)/i.test(location.pathname || '');
}

async function prepareLoginCodeFlow(timeout = 15000) {
  const readyTarget = getVerificationCodeTarget();
  if (readyTarget) {
    log('[备用着陆网] 一次性降落坐标输入框已就绪。');
    return { ready: true, mode: readyTarget.type };
  }

  if (isEmailVerificationPage() && isVerificationPageStillVisible()) {
    log('[机动规避] 已切入邮件验证网段，等待输入框稳定渲染...');
    return { ready: true, mode: 'verification_page' };
  }

  const start = Date.now();
  let switchClickCount = 0;
  let lastSwitchAttemptAt = 0;
  let loggedPasswordPage = false;
  let loggedVerificationPage = false;

  while (Date.now() - start < timeout) {
    throwIfStopped();

    const target = getVerificationCodeTarget();
    if (target) {
      log('[备用着陆网] 一次性降落坐标网段已就绪。');
      return { ready: true, mode: target.type };
    }

    if (isEmailVerificationPage() && isVerificationPageStillVisible()) {
      if (!loggedVerificationPage) {
        loggedVerificationPage = true;
        log('[机动规避] 已切入邮件验证网段，等待渲染...');
      }
      await sleep(250);
      continue;
    }

    const passwordInput = document.querySelector('input[type="password"]');
    const switchTrigger = findOneTimeCodeLoginTrigger();

    if (switchTrigger && (switchClickCount === 0 || Date.now() - lastSwitchAttemptAt > 1500)) {
      switchClickCount += 1;
      lastSwitchAttemptAt = Date.now();
      loggedPasswordPage = false;
      log('[雷达预警] 发现密码拦截网，正在执行战术规避 (切换验证码登录)...');
      await humanPause(350, 900);
      simulateClick(switchTrigger);
      await sleep(1200);
      continue;
    }

    if (passwordInput && !loggedPasswordPage) {
      loggedPasswordPage = true;
      log('[扫描中] 正在寻找一次性战术规避入口...');
    }

    await sleep(200);
  }

  throw new Error('机组无法切入一次性着陆网段。URL: ' + location.href);
}

async function resendVerificationCode(step, timeout = 45000) {
  if (step === 7) {
    await prepareLoginCodeFlow();
  }

  const start = Date.now();
  let action = null;
  let loggedWaiting = false;

  while (Date.now() - start < timeout) {
    throwIfStopped();
    action = findResendVerificationCodeTrigger({ allowDisabled: true });

    if (action && isActionEnabled(action)) {
      log(`[信号重发] 重发请求按钮已就绪。`);
      await humanPause(350, 900);
      simulateClick(action);
      await sleep(1200);
      return {
        resent: true,
        buttonText: getActionText(action),
      };
    }

    if (action && !loggedWaiting) {
      loggedWaiting = true;
      log(`[信号重发] 正在等待信号塔冷却...`);
    }

    await sleep(250);
  }

  throw new Error('信号塔冷却超时，无法重发。URL: ' + location.href);
}

async function step2_clickRegister() {
  log('[滑行预备] 机组正在寻找起飞跑道入口 (注册按钮)...');

  let registerBtn = null;
  try {
    registerBtn = await waitForElementByText(
      'a, button, [role="button"], [role="link"]',
      /sign\s*up|register|create\s*account|注册/i,
      10000
    );
  } catch {
    try {
      registerBtn = await waitForElement('a[href*="signup"], a[href*="register"]', 5000);
    } catch {
	  throw new Error(
        '未能发现起飞跑道入口。' +
        '请检查网页加载状态。URL: ' + location.href
      );
    }
  }

  await humanPause(450, 1200);
  reportComplete(2);
  simulateClick(registerBtn);
  log('[滑行就绪] 已成功推入起飞跑道');
}

async function step3_fillEmailPassword(payload) {
  const { email } = payload;
  if (!email) throw new Error('机组警告：未携带任何燃料数据 (邮箱为空)。');

  log(`[注入程序] 开始注入主推燃料：${email}`);

  let emailInput = null;
  try {
    emailInput = await waitForElement(
      'input[type="email"], input[name="email"], input[name="username"], input[id*="email"], input[placeholder*="email"], input[placeholder*="Email"]',
      10000
    );
  } catch {
    throw new Error('未找到主燃料注入舱口。URL: ' + location.href);
  }

  await humanPause(500, 1400);
  fillInput(emailInput, email);
  log('[注入程序] 燃料已注入');

  let passwordInput = document.querySelector('input[type="password"]');

  if (!passwordInput) {
    log('[点火预备] 暂未发现密钥舱，尝试初步点火推进...');
    const submitBtn = document.querySelector('button[type="submit"]')
      || await waitForElementByText('button', /continue|next|submit|继续|下一步/i, 5000).catch(() => null);

    if (submitBtn) {
      await humanPause(400, 1100);
      simulateClick(submitBtn);
      log('[点火预备] 初步点火成功，等待密钥舱部署...');
      await sleep(2000);
    }

    try {
      passwordInput = await waitForElement('input[type="password"]', 10000);
    } catch {
      throw new Error('推进后仍未发现密钥舱口。URL: ' + location.href);
    }
  }

  if (!payload.password) throw new Error('未携带二级起飞密钥。');
  await humanPause(600, 1500);
  fillInput(passwordInput, payload.password);
  log('[起飞锁定] 二级密钥已验证填写');

  reportComplete(3, { email });

  await sleep(500);
  const submitBtn = document.querySelector('button[type="submit"]')
    || await waitForElementByText('button', /continue|sign\s*up|submit|注册|创建|create/i, 5000).catch(() => null);

  if (submitBtn) {
    await humanPause(500, 1300);
    simulateClick(submitBtn);
    log('[全功率发射] 航天器已发射，脱离地面控制！');
  }
}

const INVALID_VERIFICATION_CODE_PATTERN = /代码不正确|验证码不正确|验证码错误|code\s+(?:is\s+)?incorrect|invalid\s+code|incorrect\s+code|try\s+again/i;
const VERIFICATION_PAGE_PATTERN = /检查您的收件箱|输入我们刚刚向|重新发送电子邮件|重新发送验证码|验证码|代码不正确|email\s+verification/i;
const OAUTH_CONSENT_PAGE_PATTERN = /使用\s*ChatGPT\s*登录到\s*Codex|login\s+to\s+codex|log\s+in\s+to\s+codex|authorize|授权/i;
const ADD_PHONE_PAGE_PATTERN = /add[\s-]*phone|添加手机号|手机号码|手机号|phone\s+number|telephone/i;
const STEP5_SUBMIT_ERROR_PATTERN = /无法根据该信息创建帐户|请重试|unable\s+to\s+create\s+(?:your\s+)?account|couldn'?t\s+create\s+(?:your\s+)?account|something\s+went\s+wrong|invalid\s+(?:birthday|birth|date)|生日|出生日期/i;
const SIGNUP_PASSWORD_ERROR_TITLE_PATTERN = /糟糕，出错了|something\s+went\s+wrong|oops/i;
const SIGNUP_PASSWORD_ERROR_DETAIL_PATTERN = /operation\s+timed\s+out|timed\s+out|请求超时|操作超时/i;
const SIGNUP_EMAIL_EXISTS_PATTERN = /与此电子邮件地址相关联的帐户已存在|account\s+associated\s+with\s+this\s+email\s+address\s+already\s+exists|email\s+address.*already\s+exists/i;

function getVerificationErrorText() {
  const messages = [];
  const selectors = [
    '.react-aria-FieldError',
    '[slot="errorMessage"]',
    '[id$="-error"]',
    '[data-invalid="true"] + *',
    '[aria-invalid="true"] + *',
    '[class*="error"]',
  ];

  for (const selector of selectors) {
    document.querySelectorAll(selector).forEach((el) => {
      const text = (el.textContent || '').replace(/\s+/g, ' ').trim();
      if (text) {
        messages.push(text);
      }
    });
  }

  const invalidInput = document.querySelector(`${VERIFICATION_CODE_INPUT_SELECTOR}[aria-invalid="true"], ${VERIFICATION_CODE_INPUT_SELECTOR}[data-invalid="true"]`);
  if (invalidInput) {
    const wrapper = invalidInput.closest('form, [data-rac], ._root_18qcl_51, div');
    if (wrapper) {
      const text = (wrapper.textContent || '').replace(/\s+/g, ' ').trim();
      if (text) {
        messages.push(text);
      }
    }
  }

  return messages.find((text) => INVALID_VERIFICATION_CODE_PATTERN.test(text)) || '';
}

function isStep5Ready() {
  return Boolean(
    document.querySelector('input[name="name"], input[autocomplete="name"], input[name="birthday"], input[name="age"], [role="spinbutton"][data-type="year"]')
  );
}

function getPageTextSnapshot() {
  return (document.body?.innerText || document.body?.textContent || '')
    .replace(/\s+/g, ' ')
    .trim();
}

function getPrimaryContinueButton() {
  const continueBtn = document.querySelector(
    'button[type="submit"][data-dd-action-name="Continue"], button[type="submit"]._primary_3rdp0_107'
  );
  if (continueBtn && isVisibleElement(continueBtn)) {
    return continueBtn;
  }

  const buttons = document.querySelectorAll('button, [role="button"]');
  return Array.from(buttons).find((el) => isVisibleElement(el) && /继续|Continue/i.test(el.textContent || '')) || null;
}

function isVerificationPageStillVisible() {
  if (getVerificationCodeTarget()) return true;
  if (findResendVerificationCodeTrigger({ allowDisabled: true })) return true;
  if (document.querySelector('form[action*="email-verification" i]')) return true;

  return VERIFICATION_PAGE_PATTERN.test(getPageTextSnapshot());
}

function isAddPhonePageReady() {
  const path = `${location.pathname || ''} ${location.href || ''}`;
  if (/\/add-phone(?:[/?#]|$)/i.test(path)) return true;

  const phoneInput = document.querySelector(
    'input[type="tel"]:not([maxlength="6"]), input[name*="phone" i], input[id*="phone" i], input[autocomplete="tel"]'
  );
  if (phoneInput && isVisibleElement(phoneInput)) {
    return true;
  }

  return ADD_PHONE_PAGE_PATTERN.test(getPageTextSnapshot());
}

function isStep8Ready() {
  const continueBtn = getPrimaryContinueButton();
  if (!continueBtn) return false;
  if (isVerificationPageStillVisible()) return false;
  if (isAddPhonePageReady()) return false;

  return OAUTH_CONSENT_PAGE_PATTERN.test(getPageTextSnapshot());
}

function normalizeInlineText(text) {
  return (text || '').replace(/\s+/g, ' ').trim();
}

function findBirthdayReactAriaSelect(labelText) {
  const normalizedLabel = normalizeInlineText(labelText);
  const roots = document.querySelectorAll('.react-aria-Select');

  for (const root of roots) {
    const labelEl = Array.from(root.querySelectorAll('span')).find((el) => normalizeInlineText(el.textContent) === normalizedLabel);
    if (!labelEl) continue;

    const item = root.closest('[class*="selectItem"], ._selectItem_ppsls_113') || root.parentElement;
    const nativeSelect = item?.querySelector('[data-testid="hidden-select-container"] select') || null;
    const button = root.querySelector('button[aria-haspopup="listbox"]') || null;
    const valueEl = root.querySelector('.react-aria-SelectValue') || null;

    return { root, item, labelEl, nativeSelect, button, valueEl };
  }

  return null;
}

async function setReactAriaBirthdaySelect(control, value) {
  if (!control?.nativeSelect) {
    throw new Error('未找到可写入的生日下拉框。');
  }

  const desiredValue = String(value);
  const option = Array.from(control.nativeSelect.options).find((item) => item.value === desiredValue);
  if (!option) {
    throw new Error(`生日下拉框中不存在值 ${desiredValue}。`);
  }

  control.nativeSelect.value = desiredValue;
  option.selected = true;
  control.nativeSelect.dispatchEvent(new Event('input', { bubbles: true }));
  control.nativeSelect.dispatchEvent(new Event('change', { bubbles: true }));
  await sleep(120);
}

function getStep5ErrorText() {
  const messages = [];
  const selectors = [
    '.react-aria-FieldError',
    '[slot="errorMessage"]',
    '[id$="-error"]',
    '[id$="-errors"]',
    '[role="alert"]',
    '[aria-live="assertive"]',
    '[aria-live="polite"]',
    '[class*="error"]',
  ];

  for (const selector of selectors) {
    document.querySelectorAll(selector).forEach((el) => {
      if (!isVisibleElement(el)) return;
      const text = normalizeInlineText(el.textContent);
      if (text) {
        messages.push(text);
      }
    });
  }

  const invalidField = Array.from(document.querySelectorAll('[aria-invalid="true"], [data-invalid="true"]'))
    .find((el) => isVisibleElement(el));
  if (invalidField) {
    const wrapper = invalidField.closest('form, fieldset, [data-rac], div');
    if (wrapper) {
      const text = normalizeInlineText(wrapper.textContent);
      if (text) {
        messages.push(text);
      }
    }
  }

  return messages.find((text) => STEP5_SUBMIT_ERROR_PATTERN.test(text)) || '';
}

async function waitForStep5SubmitOutcome(timeout = 15000) {
  const start = Date.now();

  while (Date.now() - start < timeout) {
    throwIfStopped();

    const errorText = getStep5ErrorText();
    if (errorText) {
      return { invalidProfile: true, errorText };
    }

    if (isAddPhonePageReady()) {
      return { success: true, addPhonePage: true };
    }

    if (isStep8Ready()) {
      return { success: true };
    }

    await sleep(150);
  }

  const errorText = getStep5ErrorText();
  if (errorText) {
    return { invalidProfile: true, errorText };
  }

  return {
    invalidProfile: true,
    errorText: '提交后未进入平飞状态，请检查舱单格式是否合规。',
  };
}

function isSignupPasswordPage() {
  return /\/create-account\/password(?:[/?#]|$)/i.test(location.pathname || '');
}

function getSignupPasswordInput() {
  const input = document.querySelector('input[type="password"]');
  return input && isVisibleElement(input) ? input : null;
}

function getSignupPasswordSubmitButton({ allowDisabled = false } = {}) {
  const direct = document.querySelector('button[type="submit"]');
  if (direct && isVisibleElement(direct) && (allowDisabled || isActionEnabled(direct))) {
    return direct;
  }

  const candidates = document.querySelectorAll('button, [role="button"]');
  return Array.from(candidates).find((el) => {
    if (!isVisibleElement(el) || (!allowDisabled && !isActionEnabled(el))) return false;
    const text = getActionText(el);
    return /继续|continue|submit|创建|create/i.test(text);
  }) || null;
}

function getSignupRetryButton() {
  const direct = document.querySelector('button[data-dd-action-name="Try again"]');
  if (direct && isVisibleElement(direct) && isActionEnabled(direct)) {
    return direct;
  }

  const candidates = document.querySelectorAll('button, [role="button"]');
  return Array.from(candidates).find((el) => {
    if (!isVisibleElement(el) || !isActionEnabled(el)) return false;
    const text = getActionText(el);
    return /重试|try\s+again/i.test(text);
  }) || null;
}

function isSignupPasswordErrorPage() {
  if (!isSignupPasswordPage()) return false;
  const text = getPageTextSnapshot();
  return Boolean(
    getSignupRetryButton()
    && (SIGNUP_PASSWORD_ERROR_TITLE_PATTERN.test(text)
      || SIGNUP_PASSWORD_ERROR_DETAIL_PATTERN.test(text)
      || SIGNUP_PASSWORD_ERROR_TITLE_PATTERN.test(document.title || ''))
  );
}

function isSignupEmailAlreadyExistsPage() {
  return isSignupPasswordPage() && SIGNUP_EMAIL_EXISTS_PATTERN.test(getPageTextSnapshot());
}

function inspectSignupVerificationState() {
  if (isSignupEmailAlreadyExistsPage()) {
    return { state: 'email_exists' };
  }
  if (isStep5Ready()) {
    return { state: 'step5' };
  }

  if (isVerificationPageStillVisible()) {
    return { state: 'verification' };
  }

  if (isSignupPasswordErrorPage()) {
    return {
      state: 'error',
      retryButton: getSignupRetryButton(),
    };
  }

  const passwordInput = getSignupPasswordInput();
  if (passwordInput) {
    return {
      state: 'password',
      passwordInput,
      submitButton: getSignupPasswordSubmitButton({ allowDisabled: true }),
    };
  }

  return { state: 'unknown' };
}

async function waitForSignupVerificationTransition(timeout = 5000) {
  const start = Date.now();

  while (Date.now() - start < timeout) {
    throwIfStopped();

    const snapshot = inspectSignupVerificationState();
    if (snapshot.state === 'step5' || snapshot.state === 'verification' || snapshot.state === 'error' || snapshot.state === 'email_exists') {
      return snapshot;
    }

    await sleep(200);
  }

  return inspectSignupVerificationState();
}

async function prepareSignupVerificationFlow(payload = {}, timeout = 30000) {
  const { password } = payload;
  const start = Date.now();
  let recoveryRound = 0;
  const maxRecoveryRounds = 3;

  while (Date.now() - start < timeout && recoveryRound < maxRecoveryRounds) {
    throwIfStopped();
	await sleep(3000);
    const roundNo = recoveryRound + 1;
        log(`[雷达监测] 正在维持高度，观察防空气流网（第 ${roundNo}/${maxRecoveryRounds} 轮循环扫描）...`, 'info');
    const snapshot = await waitForSignupVerificationTransition(5000);

    if (snapshot.state === 'step5') {
      log('[防线突破] 已突破气流网进入平流层，本阶段跳过。', 'ok');
      return { ready: true, alreadyVerified: true, retried: recoveryRound };
    }

    if (snapshot.state === 'verification') {
      log(`[火力接触] 防空密码网已就绪${recoveryRound ? `（期间自动战术规避 ${recoveryRound} 次）` : ''}。`, 'ok');
      return { ready: true, retried: recoveryRound };
    }

    if (snapshot.state === 'email_exists') {
      throw new Error('航线冲突，当前频段已被其他飞行物占用。');
    }

    recoveryRound += 1;

    if (snapshot.state === 'error') {
      if (snapshot.retryButton && isActionEnabled(snapshot.retryButton)) {
        log(`[信号丢失] 检测到推进器超时报错，正在强制点火重试（第 ${recoveryRound}/${maxRecoveryRounds} 次）...`, 'warn');
        await humanPause(350, 900);
        simulateClick(snapshot.retryButton);
        await sleep(1200);
        continue;
      }

      log(`[气流紊乱] 遭遇异常乱流，姿态调整中（${recoveryRound}/${maxRecoveryRounds}）...`, 'warn');
      continue;
    }

    if (snapshot.state === 'password') {
      if (!password) {
        throw new Error('坠回密钥舱状态，但机组未携带备用密钥，无法补救。');
      }

      if ((snapshot.passwordInput.value || '') !== password) {
        log('[姿态修正] 坠回密钥页面，正在重新填充二级密钥...', 'warn');
        await humanPause(450, 1100);
        fillInput(snapshot.passwordInput, password);
      }

      if (snapshot.submitButton && isActionEnabled(snapshot.submitButton)) {
        log(`[推力恢复] 正在重新激活推进器（第 ${recoveryRound}/${maxRecoveryRounds} 次）...`, 'warn');
        await humanPause(350, 900);
        simulateClick(snapshot.submitButton);
        await sleep(1200);
        continue;
      }

      log(`[气流紊乱] 坠回密钥页且按钮锁定，继续盘旋观察（${recoveryRound}/${maxRecoveryRounds}）...`, 'warn');
      continue;
    }

    log(`[雷达盲区] 飞行器仍在云层穿越中，继续雷达跟踪（${recoveryRound}/${maxRecoveryRounds}）...`, 'warn');
  }

  throw new Error(`密码受阻：穿越防空网阶段失联超时（已战术规避 ${recoveryRound}/${maxRecoveryRounds} 轮）。URL: ${location.href}`);
}


async function waitForVerificationSubmitOutcome(step, timeout) {
  const resolvedTimeout = timeout ?? (step === 7 ? 30000 : 12000);
  const start = Date.now();

  while (Date.now() - start < resolvedTimeout) {
    throwIfStopped();

    const errorText = getVerificationErrorText();
    if (errorText) {
      return { invalidCode: true, errorText };
    }

    if (step === 4 && isStep5Ready()) {
      return { success: true };
    }

    if (step === 7 && isStep8Ready()) {
      return { success: true };
    }

    if (step === 7 && isAddPhonePageReady()) {
      return { success: true, addPhonePage: true };
    }

    await sleep(150);
  }

  if (isVerificationPageStillVisible()) {
    return {
      invalidCode: true,
      errorText: getVerificationErrorText() || '坐标火力打击后仍未突破防线，请求增援。',
    };
  }

  return { success: true, assumed: true };
}

async function fillVerificationCode(step, payload) {
  const { code } = payload;
  if (!code) throw new Error('机组警告：空投弹药坐标为空。');

  log(`[防空火力网] 正在解译打击坐标：${code}`);

  if (step === 7) {
    await prepareLoginCodeFlow();
  }


  let codeInput = null;
  try {
    codeInput = await waitForElement(VERIFICATION_CODE_INPUT_SELECTOR, 10000);
  } catch {
    const singleInputs = document.querySelectorAll('input[maxlength="1"]');
    if (singleInputs.length >= 6) {
      log(`[战术拆分] 发现多目标散布防线，正在执行分导式火力覆盖...`);
      for (let i = 0; i < 6 && i < singleInputs.length; i++) {
        fillInput(singleInputs[i], code[i]);
        await sleep(100);
      }
      const outcome = await waitForVerificationSubmitOutcome(step);
      if (outcome.invalidCode) {
        log(`[打击偏移] 坐标不匹配被防空网拦截：${outcome.errorText}`, 'warn');
      } else if (outcome.addPhonePage) {
        log(`[火力压制] 成功突破主防线，但遭遇手机基站拦截墙。`, 'ok');
      } else {
        log(`[火力压制] 成功贯穿防线${outcome.assumed ? '（雷达静默确认）' : ''}。`, 'ok');
      }
      return outcome;
    }
    throw new Error('未发现火力投射点口。URL: ' + location.href);
  }

  fillInput(codeInput, code);
  log(`[战术打击] 坐标指令已填装`);

  await sleep(500);
  const submitBtn = document.querySelector('button[type="submit"]')
    || await waitForElementByText('button', /verify|confirm|submit|continue|确认|验证/i, 5000).catch(() => null);

  if (submitBtn) {
    await humanPause(450, 1200);
    simulateClick(submitBtn);
    log(`[战术打击] 穿甲指令已发射`);
  }

  const outcome = await waitForVerificationSubmitOutcome(step);
  if (outcome.invalidCode) {
    log(`[打击偏移] 坐标不匹配被防空网拦截：${outcome.errorText}`, 'warn');
  } else if (outcome.addPhonePage) {
    log(`[火力压制] 成功突破主防线，但遭遇手机基站拦截。`, 'ok');
  } else {
    log(`[火力压制] 成功贯穿防线${outcome.assumed ? '（雷达静默确认）' : ''}。`, 'ok');
  }

  return outcome;
}

async function step6_login(payload) {
  const { email, password } = payload;
  if (!email) throw new Error('二机编队警告：领航机未移交邮箱数据。');

  log(`[二机编队] 领航已丢失，开始接力突破作业：${email}`);


  let emailInput = null;
  try {
    emailInput = await waitForElement(
      'input[type="email"], input[name="email"], input[name="username"], input[id*="email"], input[placeholder*="email" i], input[placeholder*="Email"]',
      15000
    );
  } catch {
    throw new Error('二次编队未找到主路口。URL: ' + location.href);
  }

  await humanPause(500, 1400);
  fillInput(emailInput, email);
  log('[二机编队] 第一级燃料注入');

  await sleep(500);
  const submitBtn1 = document.querySelector('button[type="submit"]')
    || await waitForElementByText('button', /continue|next|submit|继续|下一步/i, 5000).catch(() => null);
  if (submitBtn1) {
    await humanPause(400, 1100);
    simulateClick(submitBtn1);
    log('[二机编队] 一级分离成功');
  }

  await sleep(2000);

  const passwordInput = document.querySelector('input[type="password"]');
  if (passwordInput) {
    log('[二机编队] 已发现二级舱口，注入二级密钥...');
    await humanPause(550, 1450);
    fillInput(passwordInput, password);

    await sleep(500);
    const submitBtn2 = document.querySelector('button[type="submit"]')
      || await waitForElementByText('button', /continue|log\s*in|submit|sign\s*in|登录|继续/i, 5000).catch(() => null);
    reportComplete(6, { needsOTP: true });

    if (submitBtn2) {
      await humanPause(450, 1200);
      simulateClick(submitBtn2);
      log('[二机编队] 二级点火成功，预备可能遭遇的防空网拦截');
    }
    return;
  }

  log('[二机编队] 隐身突防模式，未发现密钥舱，自动切入防空网。');
  reportComplete(6, { needsOTP: true });
}

async function step8_findAndClick() {
  log('[盲降进近] 正在搜寻最终着陆跑道引导灯 (继续按钮)...');

  const continueBtn = await findContinueButton();
  await waitForButtonEnabled(continueBtn);

  await humanPause(350, 900);
  continueBtn.scrollIntoView({ behavior: 'smooth', block: 'center' });
  continueBtn.focus();
  await sleep(250);

  const rect = getSerializableRect(continueBtn);
  log('[起落架就绪] 跑道引导灯坐标锁定，请求塔台最后下压击发。');
  return {
    rect,
    buttonText: (continueBtn.textContent || '').trim(),
    url: location.href,
  };
}

async function findContinueButton() {
  const start = Date.now();
  while (Date.now() - start < 10000) {
    throwIfStopped();
    if (isAddPhonePageReady()) {
      throw new Error('出现手机：航向严重偏移，已迫降至基站验证荒漠。URL: ' + location.href);
    }
    const button = getPrimaryContinueButton();
    if (button && isStep8Ready()) {
      return button;
    }
    await sleep(150);
  }

  throw new Error('未发现最终着陆引导灯，复飞失败。URL: ' + location.href);
}

async function waitForButtonEnabled(button, timeout = 8000) {
  const start = Date.now();
  while (Date.now() - start < timeout) {
    throwIfStopped();
    if (isButtonEnabled(button)) return;
    await sleep(150);
  }
  throw new Error('引导灯红标未解除，降落锁定。URL: ' + location.href);
}

function isButtonEnabled(button) {
  return Boolean(button)
    && !button.disabled
    && button.getAttribute('aria-disabled') !== 'true';
}

function getSerializableRect(el) {
  const rect = el.getBoundingClientRect();
  if (!rect.width || !rect.height) {
    throw new Error('着陆界面异常缩小，无法执行降落动作。URL: ' + location.href);
  }

  return {
    left: rect.left,
    top: rect.top,
    width: rect.width,
    height: rect.height,
    centerX: rect.left + (rect.width / 2),
    centerY: rect.top + (rect.height / 2),
  };
}

async function step5_fillNameBirthday(payload) {
  const { firstName, lastName, age, year, month, day } = payload;
  if (!firstName || !lastName) throw new Error('机组警告：缺失乘客舱单信息。');

  const resolvedAge = age ?? (year ? new Date().getFullYear() - Number(year) : null);
  const hasBirthdayData = [year, month, day].every(value => value != null && !Number.isNaN(Number(value)));
  if (!hasBirthdayData && (resolvedAge == null || Number.isNaN(Number(resolvedAge)))) {
    throw new Error('机组警告：缺失出厂日期数据。');
  }

  const fullName = `${firstName} ${lastName}`;
  log(`[客舱录入] 正在登记乘机代号：${fullName}`);

  let nameInput = null;
  try {
    nameInput = await waitForElement(
      'input[name="name"], input[placeholder*="全名"], input[autocomplete="name"]',
      10000
    );
  } catch {
    throw new Error('未找到乘机人姓名入口。URL: ' + location.href);
  }
  await humanPause(500, 1300);
  fillInput(nameInput, fullName);
  log(`[客舱录入] 乘机代号录入完成`);

  let birthdayMode = false;
  let ageInput = null;
  let yearSpinner = null;
  let monthSpinner = null;
  let daySpinner = null;
  let hiddenBirthday = null;
  let yearReactSelect = null;
  let monthReactSelect = null;
  let dayReactSelect = null;
  let visibleAgeInput = false;
  let visibleBirthdaySpinners = false;
  let visibleBirthdaySelects = false;

  for (let i = 0; i < 100; i++) {
    yearSpinner = document.querySelector('[role="spinbutton"][data-type="year"]');
    monthSpinner = document.querySelector('[role="spinbutton"][data-type="month"]');
    daySpinner = document.querySelector('[role="spinbutton"][data-type="day"]');
    hiddenBirthday = document.querySelector('input[name="birthday"]');
    ageInput = document.querySelector('input[name="age"]');
    yearReactSelect = findBirthdayReactAriaSelect('年');
    monthReactSelect = findBirthdayReactAriaSelect('月');
    dayReactSelect = findBirthdayReactAriaSelect('天');

    visibleAgeInput = Boolean(ageInput && isVisibleElement(ageInput));
    visibleBirthdaySpinners = Boolean(
      yearSpinner
      && monthSpinner
      && daySpinner
      && isVisibleElement(yearSpinner)
      && isVisibleElement(monthSpinner)
      && isVisibleElement(daySpinner)
    );
    visibleBirthdaySelects = Boolean(
      yearReactSelect?.button
      && monthReactSelect?.button
      && dayReactSelect?.button
      && isVisibleElement(yearReactSelect.button)
      && isVisibleElement(monthReactSelect.button)
      && isVisibleElement(dayReactSelect.button)
    );

    if (visibleAgeInput) break;
    if (visibleBirthdaySpinners || visibleBirthdaySelects) {
      birthdayMode = true;
      break;
    }
    await sleep(100);
  }

  if (birthdayMode) {
    if (!hasBirthdayData) {
      throw new Error('检测到航管限制（要求年龄认证），但缺乏预制参数。');
    }

    const yearSpinner = document.querySelector('[role="spinbutton"][data-type="year"]');
    const monthSpinner = document.querySelector('[role="spinbutton"][data-type="month"]');
    const daySpinner = document.querySelector('[role="spinbutton"][data-type="day"]');
    const yearReactSelect = findBirthdayReactAriaSelect('年');
    const monthReactSelect = findBirthdayReactAriaSelect('月');
    const dayReactSelect = findBirthdayReactAriaSelect('天');

    if (yearReactSelect?.nativeSelect && monthReactSelect?.nativeSelect && dayReactSelect?.nativeSelect) {
      const desiredDate = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      const hiddenBirthday = document.querySelector('input[name="birthday"]');

      log('[客舱录入] 识别为 React 旋钮式年龄锁，正在解锁...');
      await humanPause(450, 1100);
      await setReactAriaBirthdaySelect(yearReactSelect, year);
      await humanPause(250, 650);
      await setReactAriaBirthdaySelect(monthReactSelect, month);
      await humanPause(250, 650);
      await setReactAriaBirthdaySelect(dayReactSelect, day);

      if (hiddenBirthday) {
        const start = Date.now();
        while (Date.now() - start < 2000) {
          if ((hiddenBirthday.value || '') === desiredDate) break;
          await sleep(100);
        }
      }

      log(`[客舱录入] 出厂认证参数写入完毕: ${desiredDate}`);
    }

    if (yearSpinner && monthSpinner && daySpinner) {
      log('[客舱录入] 识别为转盘式年龄锁，正在解锁...');

      async function setSpinButton(el, value) {
        el.focus();
        await sleep(100);
        document.execCommand('selectAll', false, null);
        await sleep(50);

        const valueStr = String(value);
        for (const char of valueStr) {
          el.dispatchEvent(new KeyboardEvent('keydown', { key: char, code: `Digit${char}`, bubbles: true }));
          el.dispatchEvent(new KeyboardEvent('keypress', { key: char, code: `Digit${char}`, bubbles: true }));
          el.dispatchEvent(new InputEvent('beforeinput', { inputType: 'insertText', data: char, bubbles: true }));
          el.dispatchEvent(new InputEvent('input', { inputType: 'insertText', data: char, bubbles: true }));
          await sleep(50);
        }

        el.dispatchEvent(new KeyboardEvent('keyup', { key: 'Tab', code: 'Tab', bubbles: true }));
        el.blur();
        await sleep(100);
      }

      await humanPause(450, 1100);
      await setSpinButton(yearSpinner, year);
      await humanPause(250, 650);
      await setSpinButton(monthSpinner, String(month).padStart(2, '0'));
      await humanPause(250, 650);
      await setSpinButton(daySpinner, String(day).padStart(2, '0'));
      log(`[客舱录入] 出厂认证转盘锁定: ${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`);
    }

    const hiddenBirthday = document.querySelector('input[name="birthday"]');
    if (hiddenBirthday) {
      const dateStr = `${year}-${String(month).padStart(2, '0')}-${String(day).padStart(2, '0')}`;
      hiddenBirthday.value = dateStr;
      hiddenBirthday.dispatchEvent(new Event('input', { bubbles: true }));
      hiddenBirthday.dispatchEvent(new Event('change', { bubbles: true }));
    }
  } else if (ageInput) {
    if (resolvedAge == null || Number.isNaN(Number(resolvedAge))) {
      throw new Error('航管雷达未扫描到有效出厂参数。');
    }
    await humanPause(500, 1300);
    fillInput(ageInput, String(resolvedAge));
    log(`[客舱录入] 出厂年限简易写入：${resolvedAge}`);
  } else {
    throw new Error('未找到符合航管标准的年龄写入接孔。URL: ' + location.href);
  }
  
  await sleep(500);
  const completeBtn = document.querySelector('button[type="submit"]')
    || await waitForElementByText('button', /完成|create|continue|finish|done|agree/i, 5000).catch(() => null);
  if (!completeBtn) {
    throw new Error('舱单放行按钮失效，塔台未亮绿灯。URL: ' + location.href);
  }

  await humanPause(500, 1300);
  simulateClick(completeBtn);
  log('[飞行审批] 舱单提交完毕，等待航管局放行盖章...');

  const outcome = await waitForStep5SubmitOutcome();
  if (outcome.invalidProfile) {
    throw new Error(`[审批驳回] ${outcome.errorText}`);
  }

  log(`[平飞巡航] 审批通过，飞机进入平流层！`, 'ok');
  reportComplete(5, { addPhonePage: Boolean(outcome.addPhonePage) });
}