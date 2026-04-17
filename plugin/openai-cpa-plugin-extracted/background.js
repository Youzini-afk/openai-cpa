const LOG_PREFIX = '[塔台]';
const STOP_ERROR_MESSAGE = '收到总控熔断指令。';


let currentTask = null;
let stopRequested = false;
let activeTabId = null;
let activeWindowId = null;
let consoleTabId = null;

let heartbeatInterval = null;
let sysConfig = { apiUrl: '', token: '', workerId: '' }; 

function cleanupWindow() {
    if (activeWindowId) {
        chrome.windows.remove(activeWindowId).catch(() => {});
        activeWindowId = null;
        activeTabId = null;
    }
}

async function addLog(message, level = 'info') {
    console.log(`${LOG_PREFIX} [${level}] ${message}`);
    if (consoleTabId) {
        chrome.tabs.sendMessage(consoleTabId, { type: 'WORKER_LOG', log: message }).catch(() => {});
    }
}

function sendResult(resultData) {
    if (consoleTabId) {
        chrome.tabs.sendMessage(consoleTabId, { type: 'WORKER_RESULT', result: resultData }).catch(() => {});
    }
}

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
    if (message.action === 'CMD_INIT_NODE') {
        if (message.payload && 
            sysConfig.apiUrl === message.payload.apiUrl && 
            sysConfig.token === message.payload.token && 
            heartbeatInterval) {
            
            sendResponse({ ok: true, status: 'already_init' });
            return true; 
        }
        
        sysConfig = message.payload;
        
        if (currentTask) {
            currentTask.apiUrl = sysConfig.apiUrl;
            currentTask.token = sysConfig.token;
        }
        startHeartbeat(); 
        sendResponse({ ok: true, status: 'updated' });
        return true;
    }


	if (message.action === 'CMD_EXECUTE_TASK' || message.action === 'EXECUTE_TASK') {
		consoleTabId   = sender.tab ? sender.tab.id : null;
		stopRequested  = false;
		currentTask    = message.payload;

		if (!currentTask.apiUrl) currentTask.apiUrl = sysConfig.apiUrl;
		if (!currentTask.token) currentTask.token = sysConfig.token;

		const provider = currentTask.provider || 'openai';

		if (provider === 'neuralwatt') {
			startNeuralwattWorkflow().catch(err => {
				if (err.message !== STOP_ERROR_MESSAGE) {
					let errorType = 'failed';
					const errMsg = err.message;

					if (errMsg.includes('turnstile_blocked') || errMsg.includes('Turnstile')) {
						errorType = 'turnstile_blocked';
					} else if (errMsg.includes('verify_link_timeout') || errMsg.includes('验证链接')) {
						errorType = 'verify_link_timeout';
					} else if (errMsg.includes('login_failed') || errMsg.includes('登录失败')) {
						errorType = 'login_failed';
					} else if (errMsg.includes('key_create_failed') || errMsg.includes('Key')) {
						errorType = 'key_create_failed';
					} else if (errMsg.includes('register_failed')) {
						errorType = 'register_failed';
					}

					addLog(`[NW坠机] 航班执行异常: ${errMsg}`, 'error');
					cleanupWindow();

					sendResult({
						status: 'error',
						email: currentTask.email || '',
						password: currentTask.password || '',
						error_msg: errMsg,
						error_type: errorType,
						provider: 'neuralwatt',
					});
				}
			});
		} else {
			startWorkflow().catch(err => {
				if (err.message !== STOP_ERROR_MESSAGE) {
					let errorType = 'failed';
					const errMsg = err.message;

					if (errMsg.includes('出现手机') || errMsg.toLowerCase().includes('phone')) {
						errorType = 'phone_verify';
					} 
					else if (errMsg.includes('密码提交受阻') || errMsg.includes('密码')) {
						errorType = 'pwd_blocked';
					}

					addLog(`[坠机事故] 航班执行异常: ${errMsg}`, 'error');
					cleanupWindow();
					
					sendResult({
						status: 'error',
						task_id: currentTask.taskId || '',
						email: currentTask.email || '',
						password: currentTask.password || '',
						error_msg: errMsg,
						token_data: '',
						error_type: errorType
					});
				}
			});
		}
		
		sendResponse({ ok: true });
		return true;
	}

    if (message.action === 'CMD_STOP_WORKER') {
        stopRequested = true;
        cleanupWindow();
        addLog('🛑 [系统警报] 收到总控 [紧急熔断] 指令，航班已在空中解体并清理残骸。', 'warn');
        sendResponse({ ok: true });
        return true;
    }

    if (message.action === 'FORWARD_CONTENT_LOG') {
        addLog(message.log, message.level || 'info');
        sendResponse({ ok: true });
        return true;
    }
});

function startHeartbeat() {
    if (heartbeatInterval) clearInterval(heartbeatInterval);
    addLog(`💓 [生命维持] 节点激活！代号: ${sysConfig.workerId}，坐标: ${sysConfig.apiUrl}`);

    const sendBeat = async () => {
        if (!sysConfig.apiUrl || !sysConfig.token) return;
        let baseUrl = sysConfig.apiUrl;
        if (!baseUrl.startsWith('http')) baseUrl = 'http://' + baseUrl;
        const url = `${baseUrl}/api/ext/heartbeat?worker_id=${sysConfig.workerId}`;

        try {
            console.log(`📡 [心跳调试] 正在尝试向总控发送心跳: ${url}`);
            const res = await fetch(url, {
                method: 'POST',
                headers: { 'Authorization': `Bearer ${sysConfig.token}` }
            });
            
            if (!res.ok) {
                console.error(`❌ [心跳失败] 后端拒绝了心跳请求，HTTP 状态码: ${res.status}`);
            } else {
                console.log(`✅ [心跳成功] Python 已确认收到并登记在线！`);
            }
        } catch (e) {
            console.error('⚠️ [心跳异常] 发生网络级物理断连', e.message);
        }
    };

    sendBeat();
    heartbeatInterval = setInterval(sendBeat, 10000); 
}

function stopHeartbeat() {
    if (heartbeatInterval) {
        clearInterval(heartbeatInterval);
        heartbeatInterval = null;
    }
}

async function startWorkflow() {
    addLog(`🛫 [起飞许可] 获批起飞！航班编号: ${currentTask.email}`);
    const newWindow = await chrome.windows.create({ 
        url: currentTask.registerUrl, 
        type: 'normal',
        focused: true,
        width: 1000,
        height: 800
    });
    activeWindowId = newWindow.id;
    activeTabId = newWindow.tabs[0].id;

    await waitForContentScriptReady();

    addLog('🛤️ [跑道滑行] 塔台呼叫，引导机组进入起飞位置 (请求点击注册入口)...');
    await sendToContent({ type: 'EXECUTE_STEP', step: 2, payload: {} });
    addLog('⏳ [等待指令] 正在等待机组反馈跑道视线...');
    await sleep(2000);

    addLog('✈️ [滑行起飞] 引导机组注入基础燃料 (自动填写邮箱密码)...');
    const step3Res = await sendToContent({
        type: 'EXECUTE_STEP',
        step: 3,
        payload: {
            email:    currentTask.email,
            password: currentTask.password,
        },
    });
	
	if (step3Res && step3Res.isBlocked) { 
		throw new Error('密码提交受阻：页面未跳转或账号被即时封禁'); 
	}
	
    addLog('☁️ [爬升阶段] 进入云层，开启验证码气流雷达...');
    const prepRes = await sendToContent({
        type: 'PREPARE_SIGNUP_VERIFICATION',
        payload: { password: currentTask.password },
    }, 15);

    if (prepRes.alreadyVerified) {
        addLog('✨ [气流平稳] 未遭遇验证码拦截，直接拉升高度！');
    } else {
        addLog('⚡ [雷达预警] 遭遇拦截，正在向总控呼叫防空支援 (请求验证码)...');
        const code = await fetchCodeFromConsole(currentTask.email, currentTask.email_jwt);
        addLog(`🎯 [防空确认] 成功接收坐标 [${code}]，授权机组精准打击...`);

        const fillRes = await sendToContent({
            type: 'FILL_CODE',
            step: 4,
            payload: { code },
        });
        if (fillRes.invalidCode) throw new Error(`坐标拦截失败 (验证码被拒): ${fillRes.errorText}`);
    }

    await sleep(3000);
    const { firstName, lastName, birthday } = currentTask;
    const [year, month, day] = birthday.split('-').map(Number);
    addLog(`📋 [平飞巡航] 正在同步航线乘客舱单: ${firstName} ${lastName} | 生日: ${birthday}`);
    await sendToContent({
        type: 'EXECUTE_STEP',
        step: 5,
        payload: { firstName, lastName, year, month, day },
    }, 6);
    
    addLog('🛬 [进近着陆] 开始盲降探测，观察是否已自动切入最终跑道...');
    let callback_url = '';
    
    for (let i = 0; i < 10; i++) {
        if (stopRequested) throw new Error(STOP_ERROR_MESSAGE);
        try {
            const t = await chrome.tabs.get(activeTabId);
            if (t.url && t.url.includes('code=') && t.url.includes('state=') && t.url.includes('localhost')) {
                callback_url = t.url;
                break;
            }
        } catch(e) {}
        await sleep(1000);
    }
    
    if (!callback_url) {
        addLog('⚠️ [偏离航线] 未能一次切入跑道，启动备降预案 (二次授权兜底)...');
        await chrome.tabs.update(activeTabId, { url: currentTask.registerUrl });
        await sleep(4000);
        await waitForContentScriptReady();
    
        await sendToContent({
            type: 'EXECUTE_STEP',
            step: 6,
            payload: { email: currentTask.email, password: currentTask.password }
        });

        addLog('🌀 [复飞盘旋] 探测二次拦截码...');
        try {
            await sendToContent({ type: 'PREPARE_LOGIN_CODE' }, 10);
            addLog('⚡ [雷达预警] 遭遇二次拦截，呼叫防空支援...');
            const loginCode = await fetchCodeFromConsole(currentTask.email, currentTask.email_jwt);
            await sendToContent({ type: 'FILL_CODE', step: 7, payload: { code: loginCode } });
        } catch (e) {
            addLog('✨ [平稳通过] 未触发二次拦截。');
        }

        addLog('🔧 [起落架就绪] 正在寻找并确认最终降落许可按钮...');
        await sleep(2000);
        const clickRes = await sendToContent({ type: 'STEP8_FIND_AND_CLICK' }, 10);
        if (clickRes && clickRes.rect) addLog('✅ [降落许可] 已授权！');

        addLog('🛬 [最终着陆] 正在对准跑道中线，截获最终着陆凭证...');
        for (let i = 0; i < 60; i++) {
            if (stopRequested) throw new Error(STOP_ERROR_MESSAGE);
            try {
                const t = await chrome.tabs.get(activeTabId);
                if (t.url && t.url.includes('code=') && t.url.includes('state=') && t.url.includes('localhost')) {
                    callback_url = t.url;
                    addLog('🎯 [黑匣子确认] 成功捕获底层 Token 凭据！', 'success');
                    break;
                }
            } catch(e) { break; }
            await sleep(1000);
        }
    }

    if (!callback_url) throw new Error('通信链路丢失，未能捕获到航班回调信号。');

    addLog('🎉 [完美触地] 航班顺利返航！战利品已装车，正在销毁临时跑道...', 'success');
    cleanupWindow();

    sendResult({
		status: 'success',
		task_id: currentTask.taskId || '',
		email: currentTask.email,
		password: currentTask.password,
		error_msg: '',
		token_data: '',
		callback_url: callback_url,
		code_verifier: currentTask.code_verifier,
		expected_state: currentTask.expected_state,
	});
}

function sleep(ms) {
    return new Promise(r => setTimeout(r, ms));
}

async function waitForContentScriptReady() {
    for (let i = 0; i < 20; i++) {
        if (stopRequested) throw new Error(STOP_ERROR_MESSAGE);
        try {
            const pong = await new Promise((resolve) => {
                chrome.tabs.sendMessage(activeTabId, { type: 'PING' }, (res) => {
                    if (chrome.runtime.lastError) resolve(null);
                    else resolve(res);
                });
            });
            if (pong && pong.ok) {
                await sleep(1500);
                return;
            }
        } catch (e){}
        console.log(`[Wait] 📡 [航电系统] 正在自检并尝试连接机组... (${i + 1}/20)`);
        await sleep(2000);
    }
    throw new Error('航电系统连接超时，机组未响应，可能受困于 CF 验证。');
}

async function sendToContent(message, retries = 10) {
    for (let i = 0; i < retries; i++) {
        if (stopRequested) throw new Error(STOP_ERROR_MESSAGE);
        try {
            const tab = await chrome.tabs.get(activeTabId);
            if (tab.status !== 'complete' && i < 5) {
                await sleep(1500);
                continue;
            }
            return await new Promise((resolve, reject) => {
                chrome.tabs.sendMessage(activeTabId, message, (response) => {
                    if (chrome.runtime.lastError) reject(new Error(chrome.runtime.lastError.message));
                    else if (response && response.error) reject(new Error(response.error));
                    else resolve(response || {});
                });
            });
        } catch (e) {
            if (e.message.includes('Receiving end does not exist') || e.message.includes('closed') || e.message.includes('No tab with id')) {
                console.log(`[重试] 第 ${i + 1} 次...`);
                await sleep(2000);
                continue;
            }
            throw e;
        }
    }
    throw new Error(`机组通信彻底失联：重试 ${retries} 次均无响应。`);
}

async function fetchVerifyLinkFromConsole(email, email_jwt) {
    if (!currentTask.apiUrl) throw new Error("总控调度地址丢失！");

    let baseUrl = currentTask.apiUrl;
    if (!baseUrl.startsWith('http')) baseUrl = 'http://' + baseUrl;

    for (let i = 0; i < 30; i++) {
        if (stopRequested) {
            addLog('🛑 [NW验证] 收到塔台熔断指令。', 'warn');
            throw new Error(STOP_ERROR_MESSAGE);
        }

        try {
            const url = `${baseUrl}/api/ext/nw_get_verify_link?email=${encodeURIComponent(email)}&email_jwt=${encodeURIComponent(email_jwt)}&max_attempts=1`;
            addLog(`[NW验证] 第 ${i+1} 次轮询验证链接...`);

            const res = await fetch(url, {
                method: 'GET',
                headers: {
                    'Authorization': `Bearer ${currentTask.token}`,
                    'Accept': 'application/json'
                }
            });

            if (res.status === 401 || res.status === 403) throw new Error(`Token校验失败 (HTTP ${res.status})`);

            const data = await res.json();

            if (data.status === 'success' && data.verify_link) {
                addLog(`🎯 [NW验证] 验证链接获取成功！`);
                return data.verify_link;
            } else if (data.status === 'error') {
                addLog(`⚠️ [NW验证] 后端返回异常: ${data.message}`, 'warn');
            } else {
                addLog(`⏳ [NW验证] 验证链接尚未到达...`);
            }
        } catch (e) {
            if (e.message.includes('Failed to fetch')) addLog("无法连接到总控，请检查服务状态", "error");
            if (e.message.includes('Token校验失败')) throw e;
        }
        await sleep(3000);
    }
    throw new Error('verify_link_timeout: 验证链接获取超时（超90秒）');
}

async function startNeuralwattWorkflow() {
    addLog(`🛫 [NW起飞] 获批起飞！目标: ${currentTask.email}`);

    const newWindow = await chrome.windows.create({
        url: currentTask.registerUrl || 'https://portal.neuralwatt.com/auth/register',
        type: 'normal',
        focused: true,
        width: 1000,
        height: 800
    });
    activeWindowId = newWindow.id;
    activeTabId = newWindow.tabs[0].id;

    await waitForContentScriptReady();

    addLog('[NW注册] 开始填写注册表单...');
    const fillRes = await sendToContent({
        type: 'NW_STEP_REGISTER_FILL',
        payload: {
            email: currentTask.email,
            password: currentTask.password,
            name: currentTask.name || `${currentTask.firstName || ''} ${currentTask.lastName || ''}`.trim(),
        },
    });

    if (fillRes && fillRes.turnstileReady === false) {
        addLog('⚠️ [NW注册] Turnstile 未就绪，等待额外时间...', 'warn');
        await sleep(10000);
    }

    addLog('[NW注册] 提交注册表单...');
    const submitRes = await sendToContent({ type: 'NW_STEP_REGISTER_SUBMIT' });

    if (submitRes && submitRes.needsVerify) {
        addLog('[NW验证] 需要邮箱验证，向后端轮询验证链接...');
        try {
            const verifyLink = await fetchVerifyLinkFromConsole(currentTask.email, currentTask.email_jwt);
            addLog('[NW验证] 在浏览器中打开验证链接...');

            await chrome.tabs.update(activeTabId, { url: verifyLink });
            await sleep(5000);

            addLog('[NW验证] 验证链接已访问');
        } catch (e) {
            if (e.message.includes('verify_link_timeout')) {
                addLog('❌ [NW验证] 验证链接获取超时', 'error');
                throw e;
            }
            addLog(`⚠️ [NW验证] 验证环节异常: ${e.message}`, 'warn');
        }
    } else {
        addLog('[NW注册] 注册成功，无需邮箱验证');
    }

    await sleep(2000);

    addLog('[NW登录] 打开登录页面...');
    const loginUrl = currentTask.loginUrl || 'https://portal.neuralwatt.com/auth/login?next=/dashboard/keys';
    await chrome.tabs.update(activeTabId, { url: loginUrl });
    await sleep(4000);
    await waitForContentScriptReady();

    addLog('[NW登录] 填写登录表单...');
    const loginRes = await sendToContent({
        type: 'NW_STEP_LOGIN',
        payload: {
            email: currentTask.email,
            password: currentTask.password,
        },
    });

    if (loginRes && !loginRes.loggedIn && !loginRes.assumed) {
        throw new Error('login_failed: 登录失败，未进入仪表盘');
    }

    addLog('[NW登录] 登录成功');

    await sleep(2000);

    const currentTabUrl = (await chrome.tabs.get(activeTabId)).url || '';
    if (!currentTabUrl.includes('/dashboard/keys')) {
        addLog('[NW Key] 导航到 Key 管理页...');
        await chrome.tabs.update(activeTabId, { url: 'https://portal.neuralwatt.com/dashboard/keys' });
        await sleep(4000);
        await waitForContentScriptReady();
    }

    addLog('[NW Key] 创建 API Key...');
    const keyRes = await sendToContent({ type: 'NW_STEP_CREATE_KEY' });

    const apiKey = (keyRes && keyRes.apiKey) || '';
    const apiBase = 'https://api.neuralwatt.com/v1';
    const testModel = 'meta-llama/Llama-3.3-70B-Instruct';

    if (apiKey) {
        addLog(`🎉 [NW完成] API Key 获取成功: ${apiKey.slice(0, 8)}...`, 'success');
    } else {
        addLog('⚠️ [NW完成] 未能获取 API Key，将尝试后端兜底创建', 'warn');
    }

    cleanupWindow();

    sendResult({
        status: 'success',
        email: currentTask.email,
        password: currentTask.password,
        api_key: apiKey,
        api_base: apiBase,
        test_model: testModel,
        provider: 'neuralwatt',
    });
}

async function fetchCodeFromConsole(email, email_jwt) {
    if (!currentTask.apiUrl) throw new Error("总控调度地址丢失，无法请求资源！");

    let baseUrl = currentTask.apiUrl;
    if (!baseUrl.startsWith('http')) baseUrl = 'http://' + baseUrl;

    for (let i = 0; i < 20; i++) {
        if (stopRequested) {
            addLog('🛑 [雷达中断] 收到塔台熔断指令，已切断防空扫描阵列电源。', 'warn');
            throw new Error(STOP_ERROR_MESSAGE);
        }
        
        try {
            await new Promise((resolve, reject) => {
                chrome.tabs.sendMessage(activeTabId, { type: 'CHECK_HEALTH' }, (res) => {
                    if (chrome.runtime.lastError) {
                        reject(new Error("前线机组物理失联 (网页可能已关闭)"));
                    } else if (res && res.healthy) {
                        resolve();
                    } else {
                        reject(new Error(res ? res.reason : "阵地环境恶化"));
                    }
                });
            });
        } catch (e) {
            addLog(`⚠️ [雷达中断] 侦测到前线异常: ${e.message}。立刻终止无效死等！`, 'error');
            throw new Error(`密码受阻：飞行环境恶化: ${e.message}`);
        }
        
        try {
            const url = `${baseUrl}/api/ext/get_mail_code?email=${encodeURIComponent(email)}&email_jwt=${encodeURIComponent(email_jwt)}&max_attempts=1`;
            console.log(`[轮询第 ${i+1} 次] 正在请求: ${url}`);

            const res = await fetch(url, { 
                method: 'GET',
                headers: { 
                    'Authorization': `Bearer ${currentTask.token}`,
                    'Accept': 'application/json'
                } 
            });

            if (res.status === 401 || res.status === 403) throw new Error(`防空识别码校验失败 (HTTP ${res.status})，Token已过期`);

            const data = await res.json();
            
            if (data.status === 'success' && data.code) {
                console.log('🎯 [防空确认] 雷达成功捕获降落坐标:', data.code);
                return data.code;
            } else if (data.status === 'error') {
                console.warn('⚠️ [频段干扰] 防空阵列返回异常:', data.message);
            } else {
                console.log('⏳ [静默守听] 坐标尚未出现，雷达持续扫描中...');
            }

        } catch (e) {
            console.error('🚫 请求异常:', e.message);
            if (e.message.includes('Failed to fetch')) addLog("无法连接到地面调度中心，请检查服务状态", "error");
        }
        await sleep(2000);
    }
    throw new Error('向总控请求防空坐标超时（超40秒未达）');
}