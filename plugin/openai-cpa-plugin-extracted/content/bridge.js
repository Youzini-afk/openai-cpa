(function() {
    function isAlive() {
        return !!(chrome.runtime && chrome.runtime.id);
    }

    if (window.__BRIDGE_INJECTED__) return;
    window.__BRIDGE_INJECTED__ = true;

    if (isAlive()) {
        window.postMessage({ type: "WORKER_READY" }, "*");
    }

    window.addEventListener("message", (event) => {
        if (event.source !== window || !event.data) return;

        if (!isAlive()) return;

        if (event.data.type === "CHECK_EXTENSION_READY") {
            window.postMessage({ type: "WORKER_READY" }, "*");
        }

        try {
            if (event.data.type === "CMD_INIT_NODE") {
                chrome.runtime.sendMessage({ action: "CMD_INIT_NODE", payload: event.data.payload });
            }
            if (event.data.type === "CMD_EXECUTE_TASK") {
                chrome.runtime.sendMessage({ action: "CMD_EXECUTE_TASK", payload: event.data.payload });
            }
            if (event.data.type === "CMD_STOP_WORKER" || event.data.action === "CMD_STOP_WORKER") {
                chrome.runtime.sendMessage({ action: "CMD_STOP_WORKER" });
            }
        } catch (e) {
        }
    });

    if (isAlive()) {
        chrome.runtime.onMessage.addListener((message) => {
            if (!isAlive()) return;
            if (message.type === "WORKER_LOG") {
                window.postMessage({ type: "WORKER_LOG_REPLY", log: message.log }, "*");
            }
            if (message.type === "WORKER_RESULT") {
                window.postMessage({ type: "WORKER_RESULT_REPLY", result: message.result }, "*");
            }
        });
    }
})();