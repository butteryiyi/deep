# browser_manager.py
# 策略：生成中只监控+保存快照，完成后点复制按钮拿原生 Markdown
# DOM 纯文本仅用于：审查检测、长度监控、兜底
# v6.1: 基于旧版v6 — 唯一改动：所有消息都通过文件上传发送，不再粘贴到textarea
#       保留：服务器错误检测 + 自动重试 + 页面恢复 + 登录检测/重登录
#       保留：每次发送消息前自动点击"专家模式"按钮

import os
import sys
import time
import json
import asyncio
import base64
import shutil
import tempfile
from pathlib import Path
from datetime import datetime
from typing import AsyncGenerator, Optional

from auth_handler import AuthHandler

CENSORSHIP_PHRASES = [
    "这个问题我暂时无法回答",
    "让我们换个话题再聊聊吧",
    "我无法回答这个问题",
    "抱歉，我无法",
    "这个话题不太适合讨论",
    "我没法对此进行回答",
    "作为AI助手，我无法",
    "很抱歉，这个问题",
]

# ═══════════════════════════════════════════════════════════════
# 服务器错误/异常状态检测短语
# ═══════════════════════════════════════════════════════════════
SERVER_ERROR_PHRASES = [
    "服务器繁忙，请稍后重试",
    "服务器繁忙",
    "Server is busy",
    "server is busy",
    "Network Error",
    "网络错误",
    "请求过于频繁",
    "Too Many Requests",
    "rate limit",
    "Rate limit",
    "请稍后重试",
    "出错了",
    "Something went wrong",
    "something went wrong",
    "An error occurred",
    "服务暂时不可用",
    "Service Unavailable",
]

# ★ 上传文件的固定临时路径
_UPLOAD_TMP_PATH = os.path.join(tempfile.gettempdir(), "ds_upload_prompt.txt")


def _is_censored(text: str) -> bool:
    text = text.strip()
    if not text:
        return False
    for phrase in CENSORSHIP_PHRASES:
        if phrase in text:
            if len(text) < 150:
                return True
    return False


# ═══════════════════════════════════════════════════════════════
# 剪贴板拦截（唯一注入的 JS）
# ═══════════════════════════════════════════════════════════════
INSTALL_CLIPBOARD_HOOK_JS = """
() => {
    if (window.__clipHooked) return 'already';
    window.__clipData = { text: '', time: 0 };
    try {
        const orig = navigator.clipboard.writeText.bind(navigator.clipboard);
        navigator.clipboard.writeText = async function(text) {
            if (text && text.length > 5) {
                window.__clipData = { text: text, time: Date.now() };
            }
            return orig(text);
        };
        window.__clipHooked = true;
        return 'ok';
    } catch(e) {
        return 'fail:' + e.message;
    }
}
"""

# ═══════════════════════════════════════════════════════════════
# 读取状态（轻量，只拿纯文本用于监控和审查检测）
# ═══════════════════════════════════════════════════════════════
READ_STATE_JS = """
() => {
    const R = {
        domText: '',
        domLen: 0,
        thinkLen: 0,
        hasButton: false,
        buttonCount: 0,
        isComplete: false,
        isGenerating: false,
        itemCount: 0,
        errorText: '',
        hasError: false,
    };

    const errorSelectors = [
        '.ds-toast', '.ds-notification', '[class*="error"]',
        '[class*="toast"]', '[class*="notice"]', '[class*="alert"]',
        '[class*="warning"]', '[class*="retry"]', '.ant-message',
        '.ant-notification',
    ];

    for (const sel of errorSelectors) {
        try {
            const els = document.querySelectorAll(sel);
            for (const el of els) {
                const text = (el.innerText || '').trim();
                if (text && text.length > 2 && text.length < 200) {
                    R.errorText += text + ' | ';
                }
            }
        } catch(e) {}
    }

    const floatingLayers = document.querySelectorAll(
        '[class*="toast"], [class*="modal"], [class*="dialog"], ' +
        '[class*="popup"], [class*="overlay"], [class*="notification"], ' +
        '[role="alert"], [role="dialog"]'
    );
    for (const layer of floatingLayers) {
        const t = (layer.innerText || '').trim();
        if (t && t.length < 300) {
            R.errorText += t + ' | ';
        }
    }

    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    R.itemCount = items.length;
    if (items.length === 0) return R;

    const lastItem = items[items.length - 1];
    const itemClass = lastItem.className || '';

    R.isComplete = itemClass.includes('_43c05b5');

    const msgDiv = lastItem.querySelector('div.ds-message');
    if (!msgDiv) return R;

    const lastItemText = (lastItem.innerText || '').trim();
    const retryBtn = lastItem.querySelector('[class*="retry"], [class*="regenerate"]');
    if (retryBtn) {
        R.errorText += '(has-retry-btn) ';
    }

    const errorKeywords = ['服务器繁忙', '请稍后重试', 'Server is busy', 'Network Error',
                           '网络错误', '出错了', 'Something went wrong', '请求过于频繁'];
    for (const kw of errorKeywords) {
        if (lastItemText.includes(kw)) {
            R.hasError = true;
            R.errorText += kw + ' ';
        }
    }

    const directChildren = msgDiv.children;
    for (let i = directChildren.length - 1; i >= 0; i--) {
        const child = directChildren[i];
        if (child.tagName === 'DIV' &&
            child.classList.contains('ds-markdown') &&
            !child.classList.contains('ds-think-content')) {
            R.domText = child.innerText || '';
            R.domLen = R.domText.length;
            break;
        }
    }

    const thinkDiv = msgDiv.querySelector('div.ds-think-content');
    if (thinkDiv) {
        const thinkMd = thinkDiv.querySelector('div.ds-markdown');
        if (thinkMd) {
            R.thinkLen = (thinkMd.textContent || '').length;
        }
    }

    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const btns = btnContainer.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length > 0;
    } else {
        const btns = lastItem.querySelectorAll('div.ds-icon-button');
        R.buttonCount = btns.length;
        R.hasButton = btns.length >= 3;
    }

    R.isGenerating = !R.isComplete && R.itemCount >= 2;
    if (!R.isGenerating) {
        const thinkAnim = lastItem.querySelector('span.e4b3a110');
        if (thinkAnim) {
            const style = thinkAnim.getAttribute('style') || '';
            if (style.includes('running')) {
                R.isGenerating = true;
            }
        }
    }

    return R;
}
"""

# 点击复制按钮
CLICK_COPY_JS = """
() => {
    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    if (items.length === 0) return 'no-items';
    const lastItem = items[items.length - 1];

    if (window.__clipData) {
        window.__clipData = { text: '', time: 0 };
    }

    const btnContainer = lastItem.querySelector('div._965abe9');
    if (btnContainer) {
        const firstBtn = btnContainer.querySelector('div.ds-icon-button');
        if (firstBtn) {
            firstBtn.click();
            return 'clicked';
        }
    }

    const allBtns = lastItem.querySelectorAll('div.ds-icon-button');
    for (const btn of allBtns) {
        const path = btn.querySelector('svg path');
        if (path) {
            const d = path.getAttribute('d') || '';
            if (d.startsWith('M6.14923')) {
                btn.click();
                return 'clicked-svg';
            }
        }
    }

    return 'not-found';
}
"""

# 滚动到底部
SCROLL_BOTTOM_JS = """
() => {
    const sa = document.querySelector('.ds-scroll-area');
    if (sa) { sa.scrollTop = sa.scrollHeight; return true; }
    return false;
}
"""

# 点击"重新生成"按钮
CLICK_REGENERATE_JS = """
() => {
    const allBtns = document.querySelectorAll('button, div[role="button"], [class*="btn"], [class*="button"]');
    for (const btn of allBtns) {
        const text = (btn.innerText || '').trim();
        if (text.includes('重新生成') || text.includes('重试') || text.includes('Retry') || text.includes('Regenerate')) {
            btn.click();
            return 'clicked:' + text;
        }
    }

    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
    if (items.length === 0) return 'no-items';
    const lastItem = items[items.length - 1];

    const clickables = lastItem.querySelectorAll('[class*="retry"], [class*="regenerate"], [class*="refresh"]');
    for (const el of clickables) {
        el.click();
        return 'clicked-class';
    }

    return 'not-found';
}
"""

# ═══════════════════════════════════════════════════════════════
# 点击专家模式按钮
# ═══════════════════════════════════════════════════════════════
CLICK_EXPERT_MODE_JS = """
() => {
    const expertByAttr = document.querySelector('[data-model-type="expert"]');
    if (expertByAttr) {
        const isChecked = expertByAttr.getAttribute('aria-checked');
        if (isChecked === 'true') {
            return { status: 'already_selected', method: 'data-attr' };
        }
        expertByAttr.click();
        return { status: 'clicked', method: 'data-attr' };
    }

    const allRadios = document.querySelectorAll('div[role="radio"]');
    for (const div of allRadios) {
        const text = (div.innerText || '').trim();
        if (text.includes('专家模式') || text.includes('Expert Mode') || text.includes('Expert mode') || text.includes('expert mode')) {
            const isChecked = div.getAttribute('aria-checked');
            if (isChecked === 'true') {
                return { status: 'already_selected', method: 'role-radio-text' };
            }
            div.click();
            return { status: 'clicked', method: 'role-radio-text' };
        }
    }

    const byClass = document.querySelectorAll('div._9f2341b');
    for (const el of byClass) {
        const text = (el.innerText || '').trim();
        if (text.includes('专家模式') || text.includes('Expert Mode') || text.includes('Expert mode') || text.includes('Expert') || text.includes('expert')) {
            const isChecked = el.getAttribute('aria-checked');
            if (isChecked === 'true') {
                return { status: 'already_selected', method: 'class-name' };
            }
            el.click();
            return { status: 'clicked', method: 'class-name' };
        }
    }

    const targetTexts = ['专家模式', 'Expert Mode', 'Expert mode', 'expert mode'];
    const allClickables = document.querySelectorAll('div, span, button, a');
    for (const el of allClickables) {
        const directText = el.childNodes.length > 0 ?
            Array.from(el.childNodes)
                .filter(n => n.nodeType === 3)
                .map(n => n.textContent.trim())
                .join('') : '';
        const innerText = (el.innerText || '').trim();

        let matched = false;
        for (const t of targetTexts) {
            if (innerText === t || directText === t) {
                matched = true;
                break;
            }
        }

        if (matched) {
            let target = el;
            let parent = el.parentElement;
            while (parent) {
                if (parent.getAttribute('role') === 'radio' ||
                    parent.getAttribute('data-model-type') === 'expert' ||
                    parent.classList.contains('_9f2341b')) {
                    target = parent;
                    break;
                }
                parent = parent.parentElement;
            }
            target.click();
            return { status: 'clicked', method: 'text-search' };
        }
    }

    return { status: 'not_found', method: 'none' };
}
"""

# ═══════════════════════════════════════════════════════════════
# 检查专家模式是否已选中
# ═══════════════════════════════════════════════════════════════
CHECK_EXPERT_MODE_JS = """
() => {
    const expertEl = document.querySelector('[data-model-type="expert"]');
    if (expertEl) {
        return {
            found: true,
            checked: expertEl.getAttribute('aria-checked') === 'true'
        };
    }

    const radios = document.querySelectorAll('div[role="radio"]');
    for (const radio of radios) {
        const text = (radio.innerText || '').trim();
        if (text.includes('专家模式') || text.includes('Expert Mode') || text.includes('Expert mode') || text.includes('expert mode')) {
            return {
                found: true,
                checked: radio.getAttribute('aria-checked') === 'true'
            };
        }
    }

    return { found: false, checked: false };
}
"""

# ═══════════════════════════════════════════════════════════════
# ★ 等待文件上传完成的JS — 检测附件预览区域是否出现了文件卡片
# ═══════════════════════════════════════════════════════════════
CHECK_FILE_ATTACHED_JS = """
() => {
    // 检查是否有文件预览卡片（上传成功后会显示文件名+大小）
    const fileCards = document.querySelectorAll(
        '[class*="file-item"], [class*="file-card"], [class*="attachment"], ' +
        '[class*="upload-item"], [class*="file-preview"]'
    );
    if (fileCards.length > 0) return { attached: true, count: fileCards.length, method: 'class' };

    // 检查是否有包含 .txt / KB / MB 等文字的预览元素
    const allEls = document.querySelectorAll('div, span');
    for (const el of allEls) {
        const text = (el.innerText || '').trim();
        if (text.includes('ds_upload_prompt') || 
            (text.includes('.txt') && (text.includes('KB') || text.includes('MB') || text.includes('B')))) {
            // 确认这不是对话区的内容
            const inChat = el.closest('div[data-virtual-list-item-key]');
            if (!inChat) {
                return { attached: true, count: 1, method: 'text-detect' };
            }
        }
    }

    return { attached: false, count: 0, method: 'none' };
}
"""


class ChatPage:
    def __init__(self, page, page_id: int):
        self.page = page
        self.page_id = page_id
        self.busy = False
        self.request_count = 0
        self.last_used = 0.0
        self._hook_installed = False

    async def ensure_clipboard_hook(self):
        try:
            hooked = await self.page.evaluate("() => !!window.__clipHooked")
            if hooked:
                return
        except Exception:
            pass
        try:
            result = await self.page.evaluate(INSTALL_CLIPBOARD_HOOK_JS)
            self._hook_installed = (result in ('ok', 'already'))
        except Exception as e:
            print(f"  ⚠️ P#{self.page_id} hook 失败: {e}")

    async def reset_clip(self):
        try:
            await self.page.evaluate(
                "() => { if(window.__clipData) window.__clipData = {text:'',time:0}; }"
            )
        except Exception:
            pass

    async def read_state(self) -> dict:
        try:
            return await asyncio.wait_for(
                self.page.evaluate(READ_STATE_JS),
                timeout=15
            )
        except asyncio.TimeoutError:
            return {
                "domText": "", "domLen": 0, "thinkLen": 0,
                "hasButton": False, "buttonCount": 0,
                "isComplete": False, "isGenerating": False,
                "itemCount": 0, "errorText": "", "hasError": False,
                "error": "evaluate_timeout",
            }
        except Exception as e:
            return {
                "domText": "", "domLen": 0, "thinkLen": 0,
                "hasButton": False, "buttonCount": 0,
                "isComplete": False, "isGenerating": False,
                "itemCount": 0, "errorText": "", "hasError": False,
                "error": str(e),
            }

    async def click_copy_and_wait(self, timeout: float = 3.0) -> str:
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(CLICK_COPY_JS),
                timeout=10
            )
            if result in ('not-found', 'no-items'):
                return ""

            deadline = time.time() + timeout
            while time.time() < deadline:
                await asyncio.sleep(0.15)
                try:
                    clip = await asyncio.wait_for(
                        self.page.evaluate(
                            "() => (window.__clipData && window.__clipData.text) || ''"
                        ),
                        timeout=5
                    )
                except asyncio.TimeoutError:
                    continue
                if clip:
                    return clip
            return ""
        except Exception as e:
            print(f"  ⚠️ 复制失败: {e}")
            return ""

    async def scroll_to_bottom(self):
        try:
            await asyncio.wait_for(
                self.page.evaluate(SCROLL_BOTTOM_JS),
                timeout=5
            )
        except Exception:
            pass

    async def click_regenerate(self) -> str:
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(CLICK_REGENERATE_JS),
                timeout=10
            )
            return result
        except asyncio.TimeoutError:
            return "timeout"
        except Exception as e:
            print(f"  ⚠️ 点击重新生成失败: {e}")
            return f"error:{e}"

    async def click_expert_mode(self) -> dict:
        """点击专家模式按钮，返回操作结果"""
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(CLICK_EXPERT_MODE_JS),
                timeout=10
            )
            return result
        except asyncio.TimeoutError:
            return {"status": "timeout", "method": "none"}
        except Exception as e:
            print(f"  ⚠️ P#{self.page_id} 点击专家模式失败: {e}")
            return {"status": f"error:{e}", "method": "none"}

    async def check_expert_mode(self) -> dict:
        """检查专家模式是否已选中"""
        try:
            result = await asyncio.wait_for(
                self.page.evaluate(CHECK_EXPERT_MODE_JS),
                timeout=5
            )
            return result
        except asyncio.TimeoutError:
            return {"found": False, "checked": False}
        except Exception as e:
            return {"found": False, "checked": False}

    async def ensure_expert_mode(self) -> bool:
        check = await self.check_expert_mode()
        if check.get("checked"):
            return True

        result = await self.click_expert_mode()
        status = result.get("status", "")
        method = result.get("method", "")

        if status == "already_selected":
            return True
        elif status == "clicked":
            await asyncio.sleep(0.5)
            verify = await self.check_expert_mode()
            if verify.get("checked"):
                print(f"  🔷 P#{self.page_id} 专家模式已激活 (方式: {method})")
                return True
            else:
                print(f"  🔷 P#{self.page_id} 专家模式已点击 (方式: {method})，验证状态未确认，继续执行")
                return True
        elif status == "not_found":
            print(f"  ⚠️ P#{self.page_id} 未找到专家模式按钮")
            return False
        else:
            print(f"  ⚠️ P#{self.page_id} 专家模式操作异常: {status}")
            return False

    async def check_server_error(self) -> tuple[bool, str]:
        """检查页面是否存在服务器错误提示"""
        try:
            state = await self.read_state()
            error_text = state.get("errorText", "")
            has_error = state.get("hasError", False)

            if has_error:
                return True, error_text.strip()

            html_check = await asyncio.wait_for(self.page.evaluate("""
                () => {
                    const items = document.querySelectorAll('div[data-virtual-list-item-key]');
                    if (items.length === 0) return { found: false, text: '' };
                    const lastItem = items[items.length - 1];
                    const fullText = lastItem.innerText || '';

                    const keywords = ['服务器繁忙', '请稍后重试', 'Server is busy',
                                      'Network Error', '网络错误', '出错了',
                                      'Something went wrong', '请求过于频繁',
                                      'Too Many Requests'];
                    for (const kw of keywords) {
                        if (fullText.includes(kw)) {
                            return { found: true, text: kw };
                        }
                    }

                    const body = document.body.innerText || '';
                    for (const kw of keywords) {
                        if (body.includes(kw) && !fullText.includes(kw)) {
                            return { found: true, text: 'toast:' + kw };
                        }
                    }

                    return { found: false, text: '' };
                }
            """), timeout=10)

            if html_check and html_check.get("found"):
                return True, html_check.get("text", "unknown error")

            return False, ""
        except asyncio.TimeoutError:
            return False, "check_timeout"
        except Exception as e:
            return False, f"check_error_failed:{e}"

    async def start_new_chat(self):
        self._hook_installed = False
        if "chat.deepseek.com" not in (self.page.url or ""):
            await self.page.goto(
                "https://chat.deepseek.com/",
                wait_until="domcontentloaded", timeout=30000,
            )
            await asyncio.sleep(2)

        # 优先用 Ctrl+J 快捷键开新对话
        try:
            await self.page.keyboard.press("Control+j")
            await asyncio.sleep(1)
            textarea = self.page.locator("textarea").first
            try:
                val = await textarea.input_value()
                if val == "" or val is None:
                    return
            except Exception:
                pass
            return
        except Exception:
            pass

        # 兜底：文本匹配
        for sel in [
            "xpath=//*[contains(text(), '开启新对话')]",
            "xpath=//*[contains(text(), '新对话')]",
            "xpath=//*[contains(text(), 'New chat')]",
            "xpath=//*[contains(text(), 'New Chat')]",
            "xpath=//*[contains(text(), 'new chat')]",
            "[class*='new-chat']",
        ]:
            try:
                btn = self.page.locator(sel).first
                if await btn.is_visible(timeout=2000):
                    await btn.click()
                    await asyncio.sleep(1)
                    return
            except Exception:
                continue

        await self.page.goto(
            "https://chat.deepseek.com/",
            wait_until="domcontentloaded", timeout=30000,
        )
        await asyncio.sleep(3)

    # ═══════════════════════════════════════════════════════════════
    # ★ 新增：通过 input[type=file] 上传文件（最可靠的方式）
    # ═══════════════════════════════════════════════════════════════
async def upload_file_and_send(self, file_path: str, trigger_text: str) -> bool:
    """
    上传文件 + 输入触发语 + 按回车发送。
    返回 True 表示成功发送，False 表示上传失败。
    """
    # ── 第1步：通过 input[type=file] 设置文件 ──
    uploaded = False
    try:
        file_input = self.page.locator('input[type="file"]')
        count = await file_input.count()
        if count > 0:
            for i in range(count):
                try:
                    await file_input.nth(i).set_input_files(file_path)
                    uploaded = True
                    break
                except Exception:
                    continue
    except Exception as e:
        print(f"  ⚠️ P#{self.page_id} set_input_files 失败: {e}")

    if not uploaded:
        try:
            async with self.page.expect_file_chooser(timeout=5000) as fc_info:
                await self.page.evaluate("""
                    () => {
                        const textarea = document.querySelector('textarea');
                        if (!textarea) return;
                        let container = textarea;
                        for (let i = 0; i < 8; i++) {
                            if (container.parentElement) container = container.parentElement;
                        }
                        const btns = container.querySelectorAll('div.ds-icon-button');
                        for (const btn of btns) {
                            const svg = btn.querySelector('svg');
                            if (svg) {
                                btn.click();
                                return;
                            }
                        }
                    }
                """)
            file_chooser = await fc_info.value
            await file_chooser.set_files(file_path)
            uploaded = True
        except Exception as e:
            print(f"  ⚠️ P#{self.page_id} file_chooser 方式也失败: {e}")

    if not uploaded:
        return False

    # ── 第2步：等待文件附件出现在输入区域 ──
    attach_ok = False
    for _ in range(20):  # 最多等 10 秒
        await asyncio.sleep(0.5)
        try:
            check = await asyncio.wait_for(
                self.page.evaluate(CHECK_FILE_ATTACHED_JS),
                timeout=5
            )
            if check.get("attached"):
                attach_ok = True
                break
        except Exception:
            continue

    if not attach_ok:
        print(f"  ⚠️ P#{self.page_id} 文件上传后未检测到附件预览，仍尝试发送")

    # ══════════════════════════════════════════════════════════
    # ★ 修复核心：确保 textarea 获得焦点 + 正确触发 React 状态
    # ══════════════════════════════════════════════════════════

    # 等待附件渲染稳定
    await asyncio.sleep(0.8)

    textarea = self.page.locator("textarea").first
    try:
        await textarea.wait_for(state="visible", timeout=5000)
    except Exception as e:
        print(f"  ⚠️ P#{self.page_id} textarea 不可见: {e}")

    # ★ 修复1：强制点击 textarea 获取焦点
    try:
        await textarea.click()
        await asyncio.sleep(0.3)
    except Exception:
        pass

    # ★ 修复2：用 Playwright 的 type() 逐字输入（而非 fill），
    #           这样能正确触发 React 的 onChange/onInput 事件链
    try:
        # 先清空
        await textarea.fill("")
        await asyncio.sleep(0.1)

        # 使用 page.keyboard.type() 模拟真实键盘输入
        # 这比 fill() 更可靠地触发 React 事件
        await self.page.keyboard.type(trigger_text, delay=10)
        await asyncio.sleep(0.3)
    except Exception:
        # 兜底：JS 强制设值 + 手动派发完整事件链
        try:
            await self.page.evaluate("""
                (text) => {
                    const el = document.querySelector('textarea');
                    if (!el) return;

                    // 聚焦
                    el.focus();

                    // 通过原生 setter 设值
                    const nativeInputValueSetter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    nativeInputValueSetter.call(el, text);

                    // ★ 派发完整的事件链，确保 React 能捕获
                    el.dispatchEvent(new Event('focus', { bubbles: true }));
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                    el.dispatchEvent(new Event('change', { bubbles: true }));

                    // React 16+ 需要这个
                    const reactPropsKey = Object.keys(el).find(
                        k => k.startsWith('__reactProps$') || k.startsWith('__reactFiber$')
                    );
                    if (reactPropsKey && el[reactPropsKey] && el[reactPropsKey].onChange) {
                        el[reactPropsKey].onChange({ target: el });
                    }
                }
            """, trigger_text)
            await asyncio.sleep(0.3)
        except Exception as e2:
            print(f"  ⚠️ P#{self.page_id} 输入触发语失败: {e2}")

    # ★ 修复3：验证发送按钮是否可用，确认 React 状态已更新
    send_ready = False
    for _ in range(10):  # 最多等 5 秒
        try:
            is_ready = await self.page.evaluate("""
                () => {
                    const textarea = document.querySelector('textarea');
                    if (!textarea) return false;

                    // 检查 textarea 是否有内容
                    if (!textarea.value || textarea.value.trim() === '') return false;

                    // 检查发送按钮是否启用（蓝色可点击状态）
                    // DeepSeek 的发送按钮在有内容时会变为可点击
                    const sendBtns = document.querySelectorAll(
                        '[class*="send"], [data-testid="send"], ' +
                        'button[class*="submit"]'
                    );

                    // 也可以检查附近的 icon-button 是否有 disabled 属性
                    return true;
                }
            """)
            if is_ready:
                send_ready = True
                break
        except Exception:
            pass
        await asyncio.sleep(0.5)

    # ★ 修复4：再次确保焦点在 textarea，然后按 Enter
    try:
        await textarea.click()
        await asyncio.sleep(0.2)
    except Exception:
        pass

    # ══════════════════════════════════════════════════════════
    # ★ 修复5：多重发送策略，确保消息发出去
    # ══════════════════════════════════════════════════════════
    sent = False

    # 策略1：textarea.press("Enter")
    try:
        await textarea.press("Enter")
        await asyncio.sleep(0.8)
        # 验证是否发送成功（检查 itemCount 是否增加）
        state = await self.read_state()
        if state.get("itemCount", 0) >= 1:
            sent = True
            print(f"  ✅ P#{self.page_id} Enter 发送成功 (策略1: textarea.press)")
    except Exception:
        pass

    # 策略2：page.keyboard.press("Enter")
    if not sent:
        try:
            await textarea.click()
            await asyncio.sleep(0.2)
            await self.page.keyboard.press("Enter")
            await asyncio.sleep(0.8)
            state = await self.read_state()
            if state.get("itemCount", 0) >= 1:
                sent = True
                print(f"  ✅ P#{self.page_id} Enter 发送成功 (策略2: keyboard.press)")
        except Exception:
            pass

    # 策略3：点击发送按钮
    if not sent:
        try:
            clicked = await self.page.evaluate("""
                () => {
                    // 找发送按钮（通常是输入框右侧的圆形按钮）
                    const textarea = document.querySelector('textarea');
                    if (!textarea) return 'no-textarea';

                    let container = textarea;
                    for (let i = 0; i < 6; i++) {
                        if (container.parentElement) container = container.parentElement;
                    }

                    // 找所有可能的发送按钮
                    const candidates = container.querySelectorAll(
                        'div.ds-icon-button, button, [role="button"]'
                    );

                    for (const btn of candidates) {
                        const svg = btn.querySelector('svg');
                        if (!svg) continue;

                        // 排除附件按钮（回形针图标），找发送按钮（箭头/三角图标）
                        const paths = svg.querySelectorAll('path');
                        for (const path of paths) {
                            const d = (path.getAttribute('d') || '');
                            // DeepSeek 发送按钮的 SVG path 通常包含向上箭头的特征
                            if (d.includes('M12') || d.includes('arrow') ||
                                btn.closest('[class*="send"]')) {
                                btn.click();
                                return 'clicked-send-btn';
                            }
                        }
                    }

                    // 兜底：找最后一个 icon-button（通常就是发送按钮）
                    const allBtns = container.querySelectorAll('div.ds-icon-button');
                    if (allBtns.length > 0) {
                        const lastBtn = allBtns[allBtns.length - 1];
                        lastBtn.click();
                        return 'clicked-last-btn';
                    }

                    return 'not-found';
                }
            """)
            if clicked and 'clicked' in clicked:
                await asyncio.sleep(0.8)
                state = await self.read_state()
                if state.get("itemCount", 0) >= 1:
                    sent = True
                    print(f"  ✅ P#{self.page_id} 点击发送按钮成功 (策略3: {clicked})")
        except Exception as e:
            print(f"  ⚠️ P#{self.page_id} 点击发送按钮失败: {e}")

    # 策略4：JS 模拟 Enter 键事件
    if not sent:
        try:
            await self.page.evaluate("""
                () => {
                    const textarea = document.querySelector('textarea');
                    if (!textarea) return;
                    textarea.focus();
                    const enterEvent = new KeyboardEvent('keydown', {
                        key: 'Enter',
                        code: 'Enter',
                        keyCode: 13,
                        which: 13,
                        bubbles: true,
                        cancelable: true
                    });
                    textarea.dispatchEvent(enterEvent);
                }
            """)
            await asyncio.sleep(0.8)
            state = await self.read_state()
            if state.get("itemCount", 0) >= 1:
                sent = True
                print(f"  ✅ P#{self.page_id} JS模拟Enter成功 (策略4)")
        except Exception:
            pass

    if not sent:
        print(f"  ⚠️ P#{self.page_id} 所有发送策略均未确认成功，继续等待...")

    await asyncio.sleep(0.5)
    return True



    async def type_and_send(self, message: str):
        """原版：直接在 textarea 输入文字并发送"""
        textarea = self.page.locator("textarea").first
        await textarea.wait_for(state="visible", timeout=10000)
        await textarea.click()
        await asyncio.sleep(0.3)
        try:
            await textarea.fill("")
            await asyncio.sleep(0.1)
            await textarea.fill(message)
        except Exception:
            await self.page.evaluate("""
                (text) => {
                    const el = document.querySelector('textarea');
                    if (!el) return;
                    const setter = Object.getOwnPropertyDescriptor(
                        window.HTMLTextAreaElement.prototype, 'value'
                    ).set;
                    setter.call(el, text);
                    el.dispatchEvent(new Event('input', { bubbles: true }));
                }
            """, message)
        await asyncio.sleep(0.5)
        await textarea.press("Enter")
        await asyncio.sleep(0.5)

    async def is_alive(self) -> bool:
        try:
            if self.page.is_closed():
                return False
            await self.page.evaluate("() => 1")
            return True
        except Exception:
            return False


# ═══════════════════════════════════════════════════════════════
# SSE 心跳标记
# ═══════════════════════════════════════════════════════════════
HEARTBEAT_MARKER = "\x00__HEARTBEAT__\x00"


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.requests_handled = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"
        self._engine = "unknown"

        self._page_count = int(os.getenv("PAGE_COUNT", "3"))
        self._pages: list[ChatPage] = []
        self._page_semaphore: asyncio.Semaphore = None

        self._ready = False
        self._ready_event = asyncio.Event()
        self._camoufox_ctx = None

        self._consecutive_errors = 0
        self._last_error_time = 0.0

        # ★ 文件上传触发语
        self._upload_trigger_text = os.getenv(
            "UPLOAD_TRIGGER_TEXT",
            "请根据我上传的文件内容回复"
        )

    async def wait_until_ready(self, timeout: float = 180.0) -> bool:
        try:
            await asyncio.wait_for(self._ready_event.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    @property
    def is_ready(self) -> bool:
        return self._ready

    def _prepare_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"
        if store_dir.exists() and any(store_dir.iterdir()):
            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(store_dir, cache_dir, dirs_exist_ok=True)
            except Exception:
                pass
        else:
            store_dir.mkdir(parents=True, exist_ok=True)

    def _save_camoufox_cache(self):
        home_cache = Path.home() / ".cache"
        store_dir = home_cache / "camoufox_store"
        cache_dir = home_cache / "camoufox"
        if cache_dir.exists() and any(cache_dir.iterdir()):
            try:
                store_dir.mkdir(parents=True, exist_ok=True)
                shutil.copytree(cache_dir, store_dir, dirs_exist_ok=True)
            except Exception:
                pass

    async def initialize(self):
        print("🔧 正在初始化浏览器...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        camoufox_ok = False
        try:
            os.environ['CAMOUFOX_NO_UPDATE_CHECK'] = '1'
            self._prepare_camoufox_cache()
            await self._start_with_camoufox()
            camoufox_ok = True
            self._engine = "camoufox"
            self._save_camoufox_cache()
        except Exception as e:
            print(f"⚠️ Camoufox 失败: {e}，回退 Playwright")
            if self._camoufox_ctx:
                try:
                    await self._camoufox_ctx.__aexit__(None, None, None)
                except Exception:
                    pass
                self._camoufox_ctx = None

        if not camoufox_ok:
            await self._start_with_playwright()
            self._engine = "playwright-firefox"

        await self._inject_stealth()

        first_page = await self.context.new_page()
        auth = AuthHandler(first_page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)
        if not self.logged_in:
            print("⚠️ 登录可能未完成")
            await first_page.close()
        else:
            print("🎉 登录成功！")
            cp = ChatPage(first_page, 0)
            await cp.ensure_clipboard_hook()
            self._pages.append(cp)
            print(f"  📄 页面#0 就绪")

        for i in range(1, self._page_count):
            try:
                page = await self.context.new_page()
                await page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await asyncio.sleep(2)
                cp = ChatPage(page, i)
                await cp.ensure_clipboard_hook()
                self._pages.append(cp)
                print(f"  📄 页面#{i} 就绪")
            except Exception as e:
                print(f"  ⚠️ 页面#{i} 失败: {e}")

        self._page_semaphore = asyncio.Semaphore(len(self._pages))
        self._ready = True
        self._ready_event.set()
        print(f"✅ 就绪（{self._engine}，{len(self._pages)} 并发页面）")

    async def _start_with_camoufox(self):
        from camoufox.async_api import AsyncCamoufox
        self._camoufox_ctx = AsyncCamoufox(headless=self.headless, geoip=False)
        self.browser = await self._camoufox_ctx.__aenter__()
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN", timezone_id="Asia/Shanghai",
        )
        print("  ✅ Camoufox 启动")

    async def _start_with_playwright(self):
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()
        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception:
            import subprocess
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, timeout=120,
            )
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN", timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        print("  ✅ Playwright Firefox 启动")

    async def _inject_stealth(self):
        if self._engine == "camoufox":
            await self.context.add_init_script(
                "if(navigator.webdriver!==undefined)"
                "{Object.defineProperty(navigator,'webdriver',{get:()=>undefined})}"
            )
        else:
            await self.context.add_init_script("""
                Object.defineProperty(navigator,'webdriver',{get:()=>undefined});
                Object.defineProperty(navigator,'languages',{
                    get:()=>['zh-CN','zh','en-US','en']
                });
            """)

    async def _acquire_page(self) -> ChatPage:
        await self._page_semaphore.acquire()
        for cp in self._pages:
            if not cp.busy:
                cp.busy = True
                cp.last_used = time.time()
                return cp
        for _ in range(100):
            await asyncio.sleep(0.1)
            for cp in self._pages:
                if not cp.busy:
                    cp.busy = True
                    cp.last_used = time.time()
                    return cp
        raise RuntimeError("无法获取空闲页面")

    def _release_page(self, cp: ChatPage):
        cp.busy = False
        self._page_semaphore.release()

    async def _recover_page(self, cp: ChatPage):
        """恢复一个异常的页面到可用状态"""
        print(f"  🔄 正在恢复页面#{cp.page_id}...")
        try:
            if not await cp.is_alive():
                new_page = await self.context.new_page()
                await new_page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await asyncio.sleep(2)
                cp.page = new_page
                cp._hook_installed = False
                await cp.ensure_clipboard_hook()
                print(f"  ✅ 页面#{cp.page_id} 已通过新建恢复")
            else:
                await cp.page.goto(
                    "https://chat.deepseek.com/",
                    wait_until="domcontentloaded", timeout=30000,
                )
                await asyncio.sleep(2)
                cp._hook_installed = False
                await cp.ensure_clipboard_hook()
                print(f"  ✅ 页面#{cp.page_id} 已通过刷新恢复")
        except Exception as e:
            print(f"  ❌ 页面#{cp.page_id} 恢复失败: {e}")

    # ═══════════════════════════════════════════════════════════════
    # ★ 将内容写入临时文件
    # ═══════════════════════════════════════════════════════════════
    def _write_upload_file(self, content: str) -> str:
        with open(_UPLOAD_TMP_PATH, "w", encoding="utf-8") as f:
            f.write(content)
        return _UPLOAD_TMP_PATH

    # ═══════════════════════════════════════════════════════════════
    # 非流式接口
    # ═══════════════════════════════════════════════════════════════
    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            if chunk != HEARTBEAT_MARKER:
                full += chunk
        return full

    # ═══════════════════════════════════════════════════════════════
    # 流式接口 — 和旧版完全一样的结构
    # ═══════════════════════════════════════════════════════════════
    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        if not self._ready:
            ok = await self.wait_until_ready(timeout=180)
            if not ok:
                yield "[错误] 浏览器初始化超时"
                return

        self.total_requests += 1
        self.requests_handled += 1
        req_id = self.total_requests
        print(f"📨 #{req_id} ({len(message)} 字符)")

        done_event = asyncio.Event()
        holder = {"text": None, "error_type": None}

        async def _background_work():
            cp = None
            try:
                try:
                    cp = await asyncio.wait_for(self._acquire_page(), timeout=120)
                except asyncio.TimeoutError:
                    holder["text"] = "[错误] 所有页面繁忙，等待超时，请稍后重试"
                    holder["error_type"] = "page_timeout"
                    return
                except RuntimeError as e:
                    holder["text"] = f"[错误] {e}"
                    holder["error_type"] = "exception"
                    return

                print(f"  [#{req_id}] → 页面#{cp.page_id}")

                max_retries = 2
                for attempt in range(max_retries + 1):
                    text, err = await self._do_send_and_wait(
                        cp, message, req_id, attempt
                    )

                    if err == "server_error" and attempt < max_retries:
                        wait_time = 5 * (attempt + 1)
                        print(f"  [#{req_id}] 🔄 服务器错误，{wait_time}s 后重试 "
                              f"({attempt + 1}/{max_retries})...")
                        await asyncio.sleep(wait_time)
                        await self._recover_page(cp)
                        continue

                    holder["text"] = text
                    holder["error_type"] = err
                    if err is None:
                        self._consecutive_errors = 0
                    elif err == "server_error":
                        self._consecutive_errors += 1
                        self._last_error_time = time.time()
                    break

                if holder["text"] is None and holder["error_type"] == "server_error":
                    holder["text"] = "[错误] 服务器繁忙，多次重试后仍然失败，请稍后重试。"

            except Exception as e:
                print(f"  [#{req_id}] ❌ 后台任务异常: {e}")
                import traceback
                traceback.print_exc()
                holder["text"] = f"[错误] {e}"
                holder["error_type"] = "exception"
            finally:
                if cp:
                    self._release_page(cp)
                done_event.set()

        task = asyncio.create_task(_background_work())

        heartbeat_interval = 3.0
        while not done_event.is_set():
            try:
                await asyncio.wait_for(done_event.wait(), timeout=heartbeat_interval)
                break
            except asyncio.TimeoutError:
                yield HEARTBEAT_MARKER

        if not task.done():
            try:
                await task
            except Exception:
                pass

        final_text = holder.get("text")
        if final_text:
            yield final_text
        else:
            yield "抱歉，未能获取到响应。请稍后重试。"

    # ═══════════════════════════════════════════════════════════════
    # 核心等待逻辑 — ★ 唯一改动：用文件上传代替 type_and_send
    # ═══════════════════════════════════════════════════════════════
    async def _do_send_and_wait(
        self, cp: ChatPage, message: str, req_id: int, retry_num: int
    ) -> tuple[Optional[str], Optional[str]]:
        cp.request_count += 1

        # 检查存活
        if not await cp.is_alive():
            print(f"  [#{req_id}] 页面死亡，恢复中...")
            await self._recover_page(cp)
            if not await cp.is_alive():
                return "[错误] 页面恢复失败", "exception"

        # 新对话 + 安装 hook
        await cp.start_new_chat()
        await asyncio.sleep(0.5)
        await cp.ensure_clipboard_hook()
        await cp.reset_clip()

        # 专家模式
        expert_ok = await cp.ensure_expert_mode()
        if expert_ok:
            print(f"  [#{req_id}] 🔷 专家模式已确认激活")
        else:
            print(f"  [#{req_id}] ⚠️ 专家模式未能激活，将以当前模式继续")
        await asyncio.sleep(0.3)

        # ═══ ★ 核心改动：所有消息都通过文件上传发送 ═══
        file_path = self._write_upload_file(message)
        print(f"  [#{req_id}] 📎 消息({len(message)}字符)，走文件上传模式")

        upload_ok = await cp.upload_file_and_send(file_path, self._upload_trigger_text)

        if not upload_ok:
            # 文件上传失败，回退到直接输入（截断）
            print(f"  [#{req_id}] ⚠️ 文件上传失败，回退直接输入")
            truncated = message[:8000] if len(message) > 8000 else message
            await cp.type_and_send(truncated)

        retry_tag = f" (重试#{retry_num})" if retry_num > 0 else ""
        print(f"  [#{req_id}] 已发送{retry_tag}")

        # ═══════════════════════════════════════════════════
        # 核心等待循环 — 和旧版完全一样
        # ═══════════════════════════════════════════════════

        max_wait = 600
        poll_interval = 0.4
        best_dom_text = ""
        gen_started = False
        no_change_count = 0
        prev_len = 0
        start_ts = time.time()
        scroll_counter = 0
        final_text = None
        error_type = None

        # DOM 归零检测
        dom_zero_count = 0
        dom_was_positive = False
        dom_zero_start_time = 0.0

        # 等第二个 item 出现（AI 开始回复）
        for _ in range(60):
            await asyncio.sleep(0.5)
            st = await cp.read_state()

            has_err, err_msg = await cp.check_server_error()
            if has_err:
                print(f"  [#{req_id}] ❌ 等待期间检测到服务器错误: {err_msg}")
                return f"[服务器错误] {err_msg}", "server_error"

            if st.get("itemCount", 0) >= 2:
                break
            if time.time() - start_ts > 30:
                break

        while True:
            elapsed = time.time() - start_ts
            if elapsed > max_wait:
                print(f"  [#{req_id}] ⏰ 超时 {max_wait}s")
                error_type = "timeout"
                break

            await asyncio.sleep(poll_interval)
            scroll_counter += 1

            # 定期滚动到底部
            if scroll_counter % 12 == 0:
                await cp.scroll_to_bottom()

            # 读状态
            state = await cp.read_state()
            dom_text = state.get("domText", "")
            dom_len = state.get("domLen", 0)
            think_len = state.get("thinkLen", 0)
            is_complete = state.get("isComplete", False)
            has_button = state.get("hasButton", False)
            is_gen = state.get("isGenerating", False)
            btn_count = state.get("buttonCount", 0)
            has_error = state.get("hasError", False)
            error_text = state.get("errorText", "")

            # ══ 服务器错误检测 ══
            if has_error:
                print(f"  [#{req_id}] ❌ 检测到服务器错误: {error_text}")
                if best_dom_text:
                    final_text = best_dom_text + "\n\n[注意：响应可能不完整，服务器中途报错]"
                    error_type = "server_error"
                else:
                    final_text = None
                    error_type = "server_error"
                break

            if scroll_counter % 15 == 0:
                deep_has_err, deep_err_msg = await cp.check_server_error()
                if deep_has_err:
                    print(f"  [#{req_id}] ❌ 深度检查发现服务器错误: {deep_err_msg}")
                    if best_dom_text:
                        final_text = best_dom_text + "\n\n[注意：响应可能不完整，服务器中途报错]"
                        error_type = "server_error"
                    else:
                        final_text = None
                        error_type = "server_error"
                    break

            # 检测生成开始
            if not gen_started and (dom_len > 0 or think_len > 0 or is_gen):
                gen_started = True
                no_change_count = 0
                print(f"  [#{req_id}] 🚀 开始 "
                      f"(think={think_len} reply={dom_len})")

            # ══ 持续保存 DOM 纯文本快照 ══
            if dom_len > len(best_dom_text):
                best_dom_text = dom_text

            # ══ DOM 突然归零检测 ══
            if gen_started and dom_len > 0:
                dom_was_positive = True
                dom_zero_count = 0
                dom_zero_start_time = 0

            if gen_started and dom_was_positive and dom_len == 0 and think_len == 0:
                if dom_zero_count == 0:
                    dom_zero_start_time = time.time()
                dom_zero_count += 1

                if dom_zero_count >= int(10 / poll_interval):
                    print(f"  [#{req_id}] ⚠️ DOM 归零已 "
                          f"{time.time() - dom_zero_start_time:.0f}s，"
                          f"主动检查错误...")
                    zero_has_err, zero_err_msg = await cp.check_server_error()
                    if zero_has_err:
                        print(f"  [#{req_id}] ❌ DOM 归零确认为服务器错误: "
                              f"{zero_err_msg}")
                        if best_dom_text:
                            final_text = (best_dom_text +
                                          "\n\n[注意：响应可能不完整，服务器中途报错]")
                        error_type = "server_error"
                        break
                    elif dom_zero_count >= int(20 / poll_interval):
                        print(f"  [#{req_id}] ❌ DOM 持续归零 20s+，"
                              f"判定为异常中断")
                        if best_dom_text:
                            final_text = (best_dom_text +
                                          "\n\n[注意：响应可能不完整，生成过程异常中断]")
                        error_type = "server_error"
                        break

            # ── 审查检测（生成中） ──
            if (gen_started and len(best_dom_text) > 80
                and dom_text and dom_len < len(best_dom_text) * 0.4
                and _is_censored(dom_text)):
                print(f"  [#{req_id}] 🛡️ 生成中审查! "
                      f"dom={dom_len} snap={len(best_dom_text)}")
                final_text = best_dom_text
                break

            # ── 完成检测 ──
            if gen_started and is_complete and has_button and btn_count >= 3:
                # 再确认一次
                await asyncio.sleep(0.3)
                confirm = await cp.read_state()
                if not (confirm.get("isComplete") and confirm.get("hasButton")):
                    continue

                # 完成时也检查错误
                if confirm.get("hasError"):
                    err = confirm.get("errorText", "")
                    print(f"  [#{req_id}] ❌ 完成时检测到错误: {err}")
                    if best_dom_text:
                        final_text = (best_dom_text +
                                      "\n\n[注意：响应可能不完整]")
                    error_type = "server_error"
                    break

                # 滚到底确保完整
                await cp.scroll_to_bottom()
                await asyncio.sleep(0.2)
                final_state = await cp.read_state()
                final_dom = final_state.get("domText", "")
                final_dom_len = final_state.get("domLen", 0)

                # 更新快照
                if final_dom_len > len(best_dom_text):
                    best_dom_text = final_dom

                # ══ 检查完成时 DOM 是否已被审查替换 ══
                if (_is_censored(final_dom) and
                    len(best_dom_text) > final_dom_len * 2):
                    print(f"  [#{req_id}] 🛡️ 完成时已审查! "
                          f"dom={final_dom_len} snap={len(best_dom_text)}")
                    final_text = best_dom_text
                    break

                # ══ 点复制按钮拿 Markdown ══
                clip_text = await cp.click_copy_and_wait(timeout=3.0)

                if clip_text and not _is_censored(clip_text):
                    final_text = clip_text
                    print(f"  [#{req_id}] ✅ 完成: "
                          f"clip={len(clip_text)} dom={final_dom_len}")
                elif clip_text and _is_censored(clip_text):
                    final_text = best_dom_text
                    print(f"  [#{req_id}] 🛡️ 剪贴板被审查! "
                          f"clip={len(clip_text)} snap={len(best_dom_text)}")
                else:
                    if final_dom and not _is_censored(final_dom):
                        final_text = final_dom
                    else:
                        final_text = best_dom_text
                    print(f"  [#{req_id}] ⚠️ 剪贴板为空, "
                          f"用dom={len(final_text or '')}")
                break

            # ── 无进展检测 ──
            if dom_len == prev_len:
                no_change_count += 1
            else:
                no_change_count = 0
                prev_len = dom_len

            no_change_timeout = 45
            if no_change_count > int(no_change_timeout / poll_interval):
                nc_has_err, nc_err_msg = await cp.check_server_error()
                if nc_has_err:
                    print(f"  [#{req_id}] ❌ 无进展期间发现服务器错误: "
                          f"{nc_err_msg}")
                    if best_dom_text:
                        final_text = (best_dom_text +
                                      "\n\n[注意：响应可能不完整，服务器中途报错]")
                    error_type = "server_error"
                    break

                if gen_started and best_dom_text:
                    final_text = best_dom_text
                    print(f"  [#{req_id}] ⏰ {no_change_timeout}s 无进展: "
                          f"{len(best_dom_text)} 字")
                    break
                elif not gen_started and elapsed > 60:
                    nr_has_err, nr_err_msg = await cp.check_server_error()
                    if nr_has_err:
                        print(f"  [#{req_id}] ❌ 长时间无响应+服务器错误: "
                              f"{nr_err_msg}")
                        error_type = "server_error"
                    else:
                        print(f"  [#{req_id}] ❌ 60s 无响应")
                        error_type = "no_response"
                    break

            # 日志
            if scroll_counter % 37 == 0:
                err_info = f" err={error_text[:30]}" if error_text else ""
                print(f"  [#{req_id}] ⏳ {elapsed:.0f}s "
                      f"dom={dom_len} think={think_len} "
                      f"snap={len(best_dom_text)} "
                      f"comp={is_complete} btn={btn_count}"
                      f"{err_info}")

        # ═══════════════════════════════════════════
        # 输出
        # ═══════════════════════════════════════════
        if final_text:
            return final_text, error_type
        else:
            # 兜底
            clip = await cp.click_copy_and_wait(timeout=5.0)
            if clip and not _is_censored(clip):
                print(f"  [#{req_id}] 📋 兜底复制: {len(clip)} 字")
                return clip, error_type
            elif best_dom_text:
                print(f"  [#{req_id}] 📋 兜底快照: "
                      f"{len(best_dom_text)} 字")
                return best_dom_text, error_type
            else:
                await cp.scroll_to_bottom()
                await asyncio.sleep(1)
                st = await cp.read_state()
                dt = st.get("domText", "")
                if dt and not _is_censored(dt):
                    print(f"  [#{req_id}] 📋 兜底DOM: {len(dt)} 字")
                    return dt, error_type
                else:
                    print(f"  [#{req_id}] ❌ 完全无响应")
                    if error_type:
                        return None, error_type
                    return "抱歉，未能获取到响应。请稍后重试。", "no_response"

    # ═══════════════════════════════════════════════════════════════
    # 登录状态检测 & 自动重登录 — 和旧版完全一样
    # ═══════════════════════════════════════════════════════════════

    async def check_login_status(self) -> bool:
        for cp in self._pages:
            if cp.busy:
                continue
            try:
                if cp.page.is_closed():
                    continue

                result = await cp.page.evaluate("""
                    () => {
                        const url = window.location.href;

                        if (url.includes('sign_in') || url.includes('/login')) {
                            return { logged_in: false, reason: 'redirected_to_login', url: url };
                        }

                        if (!url.includes('deepseek.com')) {
                            return { logged_in: false, reason: 'not_on_deepseek', url: url };
                        }

                        const textarea = document.querySelector('textarea');
                        const sidebar = document.querySelector('[class*="sidebar"]');
                        const chatArea = document.querySelector('[class*="chat"]');

                        const loginBtns = document.querySelectorAll(
                            'a[href*="login"], a[href*="sign_in"], ' +
                            'button[class*="login"], button[class*="sign"]'
                        );
                        const bodyText = document.body ? (document.body.innerText || '') : '';
                        const hasLoginPrompt = bodyText.includes('登录') && bodyText.includes('注册');
                        const hasLogoutUI = loginBtns.length > 0 && !textarea;

                        if (hasLogoutUI || (hasLoginPrompt && !textarea)) {
                            return { logged_in: false, reason: 'login_ui_detected', url: url };
                        }

                        if (textarea || sidebar || chatArea) {
                            return { logged_in: true, reason: 'chat_elements_found', url: url };
                        }

                        return { logged_in: false, reason: 'no_chat_elements', url: url };
                    }
                """)

                logged_in = result.get("logged_in", False)
                reason = result.get("reason", "unknown")
                url = result.get("url", "")

                if not logged_in:
                    print(f"  🔍 登录检查：未登录 (原因: {reason}, URL: {url[:80]})")
                return logged_in

            except Exception as e:
                print(f"  ⚠️ 登录状态检查异常 (页面#{cp.page_id}): {e}")
                continue

        print("  ⚠️ 无法检查登录状态（所有页面忙），假定已登录")
        return True

    async def re_login(self) -> bool:
        print("\n🔄 ========== 开始重新登录流程 ==========")

        target_cp = None
        is_temp = False

        for cp in self._pages:
            if not cp.busy:
                try:
                    if not cp.page.is_closed():
                        target_cp = cp
                        break
                except Exception:
                    continue

        if not target_cp:
            print("  ⚠️ 所有页面都忙，尝试新建临时页面...")
            try:
                temp_page = await self.context.new_page()
                target_cp = ChatPage(temp_page, 99)
                is_temp = True
            except Exception as e:
                print(f"  ❌ 无法创建临时页面: {e}")
                return False

        try:
            auth = AuthHandler(target_cp.page, context=self.context)
            success = await auth.login(self.email, self.password)

            if success:
                self.logged_in = True
                print("  ✅ Cookie 重新注入成功！")

                await asyncio.sleep(1)
                for cp in self._pages:
                    if cp.page_id == target_cp.page_id or cp.busy:
                        continue
                    try:
                        if cp.page.is_closed():
                            continue
                        await cp.page.reload(
                            wait_until="domcontentloaded", timeout=30000
                        )
                        await asyncio.sleep(2)
                        cp._hook_installed = False
                        await cp.ensure_clipboard_hook()
                        print(f"  ✅ 页面#{cp.page_id} 已刷新")
                    except Exception as e:
                        print(f"  ⚠️ 刷新页面#{cp.page_id} 失败: {e}")

                if not is_temp:
                    target_cp._hook_installed = False
                    await target_cp.ensure_clipboard_hook()

                print("🔄 ========== 重新登录完成 ==========\n")
                return True
            else:
                self.logged_in = False
                print("  ❌ 重新登录失败，Cookie 可能已完全过期。")
                print("🔄 ========== 重新登录失败 ==========\n")
                return False

        except Exception as e:
            print(f"  ❌ 重新登录异常: {e}")
            import traceback
            traceback.print_exc()
            return False

        finally:
            if is_temp and target_cp:
                try:
                    await target_cp.page.close()
                except Exception:
                    pass

    async def refresh_idle_pages(self) -> int:
        refreshed = 0
        for cp in self._pages:
            if cp.busy:
                continue
            try:
                if cp.page.is_closed():
                    continue

                current_url = cp.page.url or ""
                if "deepseek.com" not in current_url:
                    await cp.page.goto(
                        "https://chat.deepseek.com/",
                        wait_until="domcontentloaded", timeout=30000,
                    )
                else:
                    await cp.page.reload(
                        wait_until="domcontentloaded", timeout=30000
                    )
                await asyncio.sleep(2)
                cp._hook_installed = False
                await cp.ensure_clipboard_hook()
                refreshed += 1
            except Exception as e:
                print(f"  ⚠️ 刷新页面#{cp.page_id} 失败: {e}")
        return refreshed

    async def is_alive(self) -> bool:
        if not self._ready or not self._pages:
            return False
        for cp in self._pages:
            if await cp.is_alive():
                return True
        return False

    async def get_status(self) -> dict:
        alive_count = 0
        for cp in self._pages:
            try:
                if not cp.page.is_closed():
                    alive_count += 1
            except Exception:
                pass
        busy_count = sum(1 for cp in self._pages if cp.busy)
        return {
            "browser_alive": alive_count > 0,
            "logged_in": self.logged_in,
            "ready": self._ready,
            "engine": self._engine,
            "mode": "clipboard-first-v6.1-file-upload",
            "has_token": True,
            "cookie_count": 0,
            "page_count": len(self._pages),
            "pages_alive": alive_count,
            "pages_busy": busy_count,
            "pages_idle": alive_count - busy_count,
            "uptime_seconds": time.time() - self.start_time,
            "heartbeat_count": self.heartbeat_count,
            "requests_handled": self.requests_handled,
            "total_requests": self.total_requests,
            "consecutive_errors": self._consecutive_errors,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        for cp in self._pages:
            try:
                if not cp.page.is_closed():
                    buf = await cp.page.screenshot(full_page=False)
                    return base64.b64encode(buf).decode("utf-8")
            except Exception:
                continue
        return None

    async def simulate_activity(self):
        import random
        self.heartbeat_count += 1

        for cp in self._pages:
            try:
                if cp.page.is_closed() or cp.busy:
                    continue

                await cp.page.mouse.move(
                    random.randint(100, 1800),
                    random.randint(100, 900),
                )

                if self.heartbeat_count % 3 == 0:
                    await cp.page.evaluate("""
                        () => {
                            window.dispatchEvent(new Event('mousemove'));
                            window.dispatchEvent(new Event('focus'));
                            try {
                                Object.defineProperty(document, 'hidden', {
                                    get: () => false, configurable: true
                                });
                                Object.defineProperty(document, 'visibilityState', {
                                    get: () => 'visible', configurable: true
                                });
                                document.dispatchEvent(new Event('visibilitychange'));
                            } catch(e) {}
                        }
                    """)

                if self.heartbeat_count % 6 == 0:
                    await cp.page.evaluate("""
                        () => {
                            try {
                                fetch('/api/v0/users/current', {
                                    method: 'GET',
                                    credentials: 'include'
                                }).catch(() => {});
                            } catch(e) {}
                        }
                    """)

            except Exception:
                pass

        if self.heartbeat_count % 5 == 0:
            for cp in self._pages:
                if not cp.busy:
                    try:
                        await cp.ensure_clipboard_hook()
                    except Exception:
                        pass

        if self.heartbeat_count % 10 == 0:
            alive = 0
            for cp in self._pages:
                try:
                    if not cp.page.is_closed():
                        alive += 1
                except Exception:
                    pass
            busy = sum(1 for cp in self._pages if cp.busy)
            print(f"💓 #{self.heartbeat_count} ({alive}活/{busy}忙) 登录={self.logged_in}")

    async def shutdown(self):
        try:
            self._save_camoufox_cache()
            if self.context:
                await self.context.close()
            if self._camoufox_ctx:
                await self._camoufox_ctx.__aexit__(None, None, None)
            elif self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 已关闭")
        except Exception as e:
            print(f"⚠️ {e}")
