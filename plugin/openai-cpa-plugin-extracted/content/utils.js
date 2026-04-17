
const SCRIPT_SOURCE = 'signup-page';
const LOG_PREFIX = `[机械臂]`;
const STOP_ERROR_MESSAGE = '收到总控熔断指令。';
let flowStopped = false;

if (!window.__MULTIPAGE_UTILS_LISTENER_READY__) {
  window.__MULTIPAGE_UTILS_LISTENER_READY__ = true;

  chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.type === 'STOP_FLOW') {
      flowStopped = true;
      console.warn(LOG_PREFIX, STOP_ERROR_MESSAGE);
      return;
    }

    if (message.type === 'PING') {
      sendResponse({ ok: true, source: SCRIPT_SOURCE });
    }
  });
}

function resetStopState() {
  flowStopped = false;
}

function isStopError(error) {
  const message = typeof error === 'string' ? error : error?.message;
  return message === STOP_ERROR_MESSAGE;
}

function throwIfStopped() {
  if (flowStopped) {
    throw new Error(STOP_ERROR_MESSAGE);
  }
}

const originalConsoleLog = console.log;
const originalConsoleWarn = console.warn;
const originalConsoleError = console.error;

function sendLogToTower(msgArray, level = 'info') {
  try {
    const logString = msgArray.map(item => {
      if (item instanceof Element) return `<${item.tagName.toLowerCase()} ${item.id ? 'id="'+item.id+'"' : ''}>`;
      if (typeof item === 'object') {
        try { return JSON.stringify(item); } catch(e) { return Object.prototype.toString.call(item); }
      }
      return String(item);
    }).join(' ');

    chrome.runtime.sendMessage({
      action: 'FORWARD_CONTENT_LOG',
      log: logString,
      level: level
    }).catch(() => {});
  } catch (e) {}
}

console.log = function(...args) {
  originalConsoleLog.apply(console, args);
  sendLogToTower(args, 'info');
};

console.warn = function(...args) {
  originalConsoleWarn.apply(console, args);
  sendLogToTower(args, 'warn');
};

console.error = function(...args) {
  originalConsoleError.apply(console, args);
  sendLogToTower(args, 'error');
};

function log(message, level = 'info') {
  if (level === 'warn') {
    console.warn(message);
  } else if (level === 'error') {
    console.error(message);
  } else {
    console.log(message);
  }
}

function reportComplete(step, data = {}) {
  log(`${LOG_PREFIX} 动作阶段 [${step}] 执行完毕`, 'ok');
}

function reportError(step, errorMessage) {
  log(`${LOG_PREFIX} 动作阶段 [${step}] 遇到障碍: ${errorMessage}`, 'error');
}

function waitForElement(selector, timeout = 10000) {
  return new Promise((resolve, reject) => {
    throwIfStopped();

    const existing = document.querySelector(selector);
    if (existing) {
      resolve(existing);
      return;
    }

    let settled = false;
    let stopTimer = null;
    const cleanup = () => {
      if (settled) return;
      settled = true;
      observer.disconnect();
      clearTimeout(timer);
      clearTimeout(stopTimer);
    };

    const observer = new MutationObserver(() => {
      if (flowStopped) {
        cleanup();
        reject(new Error(STOP_ERROR_MESSAGE));
        return;
      }
      const el = document.querySelector(selector);
      if (el) {
        cleanup();
        resolve(el);
      }
    });

    observer.observe(document.body || document.documentElement, {
      childList: true,
      subtree: true,
    });

    const timer = setTimeout(() => {
      cleanup();
      const msg = `雷达扫描 [${selector}] 超时 (超过 ${timeout}ms)`;
      console.error(LOG_PREFIX, msg);
      reject(new Error(msg));
    }, timeout);

    const pollStop = () => {
      if (settled) return;
      if (flowStopped) {
        cleanup();
        reject(new Error(STOP_ERROR_MESSAGE));
        return;
      }
      stopTimer = setTimeout(pollStop, 100);
    };
    pollStop();
  });
}

function waitForElementByText(containerSelector, textPattern, timeout = 10000) {
  return new Promise((resolve, reject) => {
    throwIfStopped();

    function search() {
      const candidates = document.querySelectorAll(containerSelector);
      for (const el of candidates) {
        if (textPattern.test(el.textContent)) {
          return el;
        }
      }
      return null;
    }

    const existing = search();
    if (existing) {
      resolve(existing);
      return;
    }

    let settled = false;
    let stopTimer = null;
    const cleanup = () => {
      if (settled) return;
      settled = true;
      observer.disconnect();
      clearTimeout(timer);
      clearTimeout(stopTimer);
    };

    const observer = new MutationObserver(() => {
      if (flowStopped) {
        cleanup();
        reject(new Error(STOP_ERROR_MESSAGE));
        return;
      }
      const el = search();
      if (el) {
        cleanup();
        resolve(el);
      }
    });

    observer.observe(document.body || document.documentElement, {
      childList: true,
      subtree: true,
    });

    const timer = setTimeout(() => {
      cleanup();
      const msg = `雷达扫描文本 [${textPattern}] 超时 (超过 ${timeout}ms)`;
      console.error(LOG_PREFIX, msg);
      reject(new Error(msg));
    }, timeout);

    const pollStop = () => {
      if (settled) return;
      if (flowStopped) {
        cleanup();
        reject(new Error(STOP_ERROR_MESSAGE));
        return;
      }
      stopTimer = setTimeout(pollStop, 100);
    };
    pollStop();
  });
}

function fillInput(el, value) {
  throwIfStopped();
  const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
    window.HTMLInputElement.prototype,
    'value'
  ).set;
  nativeInputValueSetter.call(el, value);
  el.dispatchEvent(new Event('input', { bubbles: true }));
  el.dispatchEvent(new Event('change', { bubbles: true }));
  log(`${LOG_PREFIX} 数据注入完毕: [${el.name || el.id || el.type || '未知接入槽'}]`);
}

function fillSelect(el, value) {
  throwIfStopped();
  el.value = value;
  el.dispatchEvent(new Event('change', { bubbles: true }));
  log(`${LOG_PREFIX} 档位已拨动: ${value}`);
}

function simulateClick(el) {
  throwIfStopped();
  el.dispatchEvent(new MouseEvent('click', { bubbles: true, cancelable: true }));
  log(`${LOG_PREFIX} 执行精准击发: "${el.textContent?.trim().slice(0, 30) || el.tagName}"`);
}

function sleep(ms) {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    function tick() {
      if (flowStopped) {
        reject(new Error(STOP_ERROR_MESSAGE));
        return;
      }
      if (Date.now() - start >= ms) {
        resolve();
        return;
      }
      setTimeout(tick, Math.min(100, Math.max(25, ms - (Date.now() - start))));
    }
    tick();
  });
}

async function humanPause(min = 250, max = 850) {
  const duration = Math.floor(Math.random() * (max - min + 1)) + min;
  await sleep(duration);
}