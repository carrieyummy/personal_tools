"use strict";

/**
 * 将当前 Chrome 的 B 站登录 Cookie 保存为本机 JSON 文件。
 * 无参数；返回 Promise<void>。凭据仅写入用户触发的本机下载，不发送网络请求。
 */
async function exportCookies() {
  const button = document.getElementById("export");
  const status = document.getElementById("status");
  button.disabled = true;
  try {
    const cookies = await chrome.cookies.getAll({ domain: "bilibili.com" });
    if (!cookies.some((cookie) => cookie.name === "SESSDATA" && cookie.value)) {
      throw new Error("没有找到已登录的 B 站账号。请在同一个 Chrome 配置中确认登录后重试。");
    }
    const payload = { version: 1, exportedAt: new Date().toISOString(), userAgent: navigator.userAgent, cookies };
    const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = "bilibili-cookies.json";
    document.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
    status.textContent = "已开始保存。保存后运行工作区“B站：验证 Chrome 登录态”，然后即可下载。";
  } catch (error) {
    status.textContent = error.message || "导出失败，请重试。";
  } finally {
    button.disabled = false;
  }
}

document.getElementById("export").addEventListener("click", exportCookies);
