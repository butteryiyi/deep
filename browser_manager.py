# browser_manager.py
"""
DeepSeek 反代（浏览器驻留模式）
- Playwright 登录
- 拦截浏览器请求获取 API 信息
- 保留浏览器：通过 page.evaluate(fetch) 发请求，自动处理 PoW
"""

import os
import sys
import json
import time
import asyncio
import re
import hashlib
from datetime import datetime
from typing import AsyncGenerator, Optional

from auth_handler import AuthHandler


class BrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self.page = None
        self.logged_in = False
        self.start_time = time.time()
        self.heartbeat_count = 0
        self.total_requests = 0

        self.email = os.getenv("DEEPSEEK_EMAIL", "")
        self.password = os.getenv("DEEPSEEK_PASSWORD", "")
        self.headless = os.getenv("HEADLESS", "true").lower() == "true"

        self._cookies: dict = {}
        self._token: str = ""
        self._api_base: str = "https://chat.deepseek.com/api/v0"
        self._extra_headers: dict = {}

    async def initialize(self):
        print("🔧 正在初始化...")

        if not os.environ.get("PLAYWRIGHT_BROWSERS_PATH"):
            if os.path.isdir("/opt/browsers"):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = "/opt/browsers"

        await self._login_with_browser()

        if not self.logged_in:
            raise RuntimeError("❌ 登录失败。")

        # 拦截获取 Token + Headers
        await self._capture_real_api()
        await self._extract_credentials()

        if not self._token:
            raise RuntimeError("❌ 无法提取 Token。")

        # 验证 API
        ok = await self._verify_api()
        if ok:
            print("🎉 API 验证通过，浏览器驻留模式就绪。")
        else:
            print("⚠️ API 验证未通过，但仍继续运行。")

    async def _login_with_browser(self):
        print("  → 启动浏览器进行登录...")
        from playwright.async_api import async_playwright
        self.playwright = await async_playwright().start()

        try:
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )
        except Exception:
            import subprocess
            env = os.environ.copy()
            subprocess.run(
                [sys.executable, "-m", "playwright", "install", "firefox"],
                capture_output=True, text=True, env=env, timeout=120,
            )
            self.browser = await self.playwright.firefox.launch(
                headless=self.headless, args=["--no-sandbox"]
            )

        self.context = await self.browser.new_context(
            viewport={"width": 1920, "height": 1080},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:126.0) "
                "Gecko/20100101 Firefox/126.0"
            ),
        )
        await self.context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )
        self.page = await self.context.new_page()

        auth = AuthHandler(self.page, context=self.context)
        self.logged_in = await auth.login(self.email, self.password)

    async def _capture_real_api(self):
        """拦截浏览器请求，获取 Token 和 Headers"""
        print("  → 拦截浏览器请求获取认证信息...")

        captured = []

        async def on_request(request):
            if request.resource_type in ("fetch", "xhr"):
                captured.append({
                    "url": request.url,
                    "method": request.method,
                    "headers": dict(request.headers),
                })

        self.page.on("request", on_request)
        await asyncio.sleep(3)

        # 如果自然加载没有足够请求，触发一次
        deepseek_reqs = [r for r in captured if "deepseek.com/api" in r["url"]]
        if not deepseek_reqs:
            print("  → 触发页面操作以捕获 API 请求...")
            try:
                for sel in [
                    "xpath=//*[contains(text(), '开启新对话')]",
                    "xpath=//*[contains(text(), 'New chat')]",
                ]:
                    try:
                        btn = self.page.locator(sel).first
                        if await btn.is_visible(timeout=2000):
                            await btn.click()
                            await asyncio.sleep(1)
                            break
                    except Exception:
                        continue

                textarea = self.page.locator("textarea").first
                await textarea.wait_for(state="visible", timeout=5000)
                await textarea.fill("test")
                await textarea.press("Enter")
                await asyncio.sleep(5)
            except Exception as e:
                print(f"  ⚠️ 触发操作失败: {e}")

        self.page.remove_listener("request", on_request)

        # 提取信息
        for req in captured:
            if "deepseek.com" not in req["url"]:
                continue
            headers = req["headers"]
            auth_h = headers.get("authorization", "")
            if auth_h.startswith("Bearer ") and not self._token:
                self._token = auth_h[7:]
                print(f"  ✅ Token: {self._token[:30]}...")

            for key in ["x-app-version", "x-client-locale", "x-client-platform",
                         "x-client-version", "x-client-timezone"]:
                if key in headers and key not in self._extra_headers:
                    self._extra_headers[key] = headers[key]

    async def _extract_credentials(self):
        print("  → 提取凭据...")

        raw_cookies = await self.context.cookies()
        for c in raw_cookies:
            self._cookies[c["name"]] = c["value"]
        print(f"  ✅ Cookie: {len(self._cookies)} 个")

        if self._token:
            return

        try:
            token = await self.page.evaluate("""
                () => {
                    function extract(raw) {
                        if (!raw) return null;
                        try {
                            const p = JSON.parse(raw);
                            if (p && typeof p.value === 'string') return p.value;
                            if (typeof p === 'string') return p;
                        } catch(e) {}
                        return raw;
                    }
                    for (const k of ['userToken', 'ds_token', 'token', '_token', 'auth_token']) {
                        const r = localStorage.getItem(k);
                        if (r) {
                            const v = extract(r);
                            if (v && v.length > 20 && !v.startsWith('{') && !v.startsWith('<')) return v;
                        }
                    }
                    return null;
                }
            """)
            if token:
                self._token = token.strip()
                print(f"  ✅ Token(localStorage): {self._token[:30]}...")
        except Exception as e:
            print(f"  ⚠️ {e}")

    async def _verify_api(self) -> bool:
        """在浏览器内验证 API"""
        try:
            result = await self.page.evaluate("""
                async (token) => {
                    try {
                        const resp = await fetch('/api/v0/chat_session/create', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'application/json',
                                'Authorization': 'Bearer ' + token,
                            },
                            body: JSON.stringify({}),
                        });
                        const data = await resp.json();
                        return {status: resp.status, data: JSON.stringify(data).substring(0, 500)};
                    } catch(e) {
                        return {error: e.message};
                    }
                }
            """, self._token)

            print(f"  → 验证结果: {result}")
            if result.get("status") == 200:
                data = json.loads(result["data"])
                if data.get("code") == 0:
                    # 清理测试会话
                    sid = data.get("data", {}).get("biz_data", {}).get("chat_session", {}).get("id")
                    if sid:
                        await self.page.evaluate("""
                            async (args) => {
                                await fetch('/api/v0/chat_session/delete', {
                                    method: 'POST',
                                    credentials: 'include',
                                    headers: {
                                        'Content-Type': 'application/json',
                                        'Authorization': 'Bearer ' + args.token,
                                    },
                                    body: JSON.stringify({chat_session_id: args.sid}),
                                });
                            }
                        """, {"token": self._token, "sid": sid})
                    return True
            return False
        except Exception as e:
            print(f"  → 验证异常: {e}")
            return False

    # ─── 核心：在浏览器内执行完整对话流程（自动处理 PoW）───

    async def send_message(self, message: str) -> str:
        full = ""
        async for chunk in self.send_message_stream(message):
            full += chunk
        return full

    async def send_message_stream(self, message: str) -> AsyncGenerator[str, None]:
        self.total_requests += 1
        req_id = self.total_requests
        print(f"📨 请求 #{req_id} (长度: {len(message)} 字符)")

        if not self.page:
            yield "[错误] 浏览器未就绪"
            return

        try:
            # 全部在浏览器内完成：创建会话 → PoW → 发消息 → 收流
            # 用 page.evaluate 执行 JS，但 SSE 流式不能用 evaluate 直接返回
            # 所以：用 page.evaluate 创建会话 + PoW，然后用 route 拦截来读流

            # Step 1: 创建会话
            session_result = await self.page.evaluate("""
                async (token) => {
                    try {
                        const resp = await fetch('/api/v0/chat_session/create', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'application/json',
                                'Authorization': 'Bearer ' + token,
                            },
                            body: JSON.stringify({}),
                        });
                        return await resp.json();
                    } catch(e) {
                        return {error: e.message};
                    }
                }
            """, self._token)

            if session_result.get("error") or session_result.get("code") != 0:
                yield f"[错误] 创建会话失败: {json.dumps(session_result, ensure_ascii=False)[:200]}"
                return

            chat_session_id = session_result["data"]["biz_data"]["id"]
            print(f"  [{req_id}] 会话: {chat_session_id}")

            # Step 2: 获取 PoW Challenge
            pow_result = await self.page.evaluate("""
                async (token) => {
                    try {
                        const resp = await fetch('/api/v0/chat/create_pow_challenge', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'application/json',
                                'Authorization': 'Bearer ' + token,
                            },
                            body: JSON.stringify({target_path: '/api/v0/chat/completion'}),
                        });
                        return await resp.json();
                    } catch(e) {
                        return {error: e.message};
                    }
                }
            """, self._token)

            pow_response_header = ""
            if pow_result.get("code") == 0:
                challenge_data = pow_result["data"]["biz_data"]["challenge"]
                algorithm = challenge_data.get("algorithm", "")
                challenge = challenge_data.get("challenge", "")
                salt = challenge_data.get("salt", "")
                difficulty = challenge_data.get("difficulty", 0)
                expire_at = challenge_data.get("expire_at", 0)

                print(f"  [{req_id}] PoW: algo={algorithm} diff={difficulty}")

                # Step 3: 在浏览器内解 PoW（复用页面已加载的 WASM）
                pow_answer = await self.page.evaluate("""
                    async (params) => {
                        // 浏览器里应该有全局的 PoW solver
                        // 如果没有，用纯 JS 实现
                        const {challenge, salt, difficulty, algorithm, expire_at} = params;

                        if (algorithm === 'DeepSeekHashV1') {
                            // 尝试使用页面上的 sha3
                            async function sha3_256(data) {
                                // 尝试全局 sha3
                                if (typeof window.sha3_256 === 'function') {
                                    return window.sha3_256(data);
                                }
                                // 尝试 crypto.subtle (SHA-256, 非 SHA3)
                                // DeepSeek 用的是 SHA3，需要 WASM
                                // 从页面寻找已加载的 wasm 模块
                                if (typeof window.__wbg_sha3_256 === 'function') {
                                    return window.__wbg_sha3_256(data);
                                }
                                return null;
                            }

                            // 暴力搜索 nonce
                            for (let nonce = 0; nonce < 1000000; nonce++) {
                                const input = salt + '_' + nonce + '_' + challenge;
                                // 用 Web Crypto API (SHA-256 作为 fallback)
                                const encoder = new TextEncoder();
                                const dataBytes = encoder.encode(input);
                                const hashBuffer = await crypto.subtle.digest('SHA-256', dataBytes);
                                const hashArray = new Uint8Array(hashBuffer);

                                // 检查前 difficulty 位是否为 0
                                let leadingZeros = 0;
                                for (const byte of hashArray) {
                                    if (byte === 0) { leadingZeros += 8; }
                                    else {
                                        let b = byte;
                                        while ((b & 0x80) === 0) { leadingZeros++; b <<= 1; }
                                        break;
                                    }
                                    if (leadingZeros >= difficulty) break;
                                }

                                if (leadingZeros >= difficulty) {
                                    return {
                                        nonce: nonce.toString(),
                                        result: Array.from(hashArray).map(b => b.toString(16).padStart(2, '0')).join(''),
                                    };
                                }
                            }
                            return {error: 'PoW solve failed after 1M iterations'};
                        }
                        return {error: 'Unknown algorithm: ' + algorithm};
                    }
                """, {
                    "challenge": challenge,
                    "salt": salt,
                    "difficulty": difficulty,
                    "algorithm": algorithm,
                    "expire_at": expire_at,
                })

                if pow_answer.get("error"):
                    print(f"  [{req_id}] ⚠️ PoW JS 求解失败: {pow_answer['error']}")
                    # 用 Python hashlib 兜底（SHA3-256）
                    pow_answer = self._solve_pow_python(challenge, salt, difficulty)

                if pow_answer and not pow_answer.get("error"):
                    # 构造 header: algorithm=xxx,challenge=xxx,salt=xxx,answer=nonce,signature=hash
                    pow_response_header = (
                        f"{algorithm}_{challenge}_{salt}_{pow_answer['nonce']}"
                    )
                    print(f"  [{req_id}] PoW 已解决: nonce={pow_answer['nonce']}")
            else:
                print(f"  [{req_id}] ⚠️ PoW challenge 获取失败，尝试不带 PoW 发送")

            # Step 4: 发送消息（SSE 流式）
            # 不能用 evaluate 读 SSE 流，改用：在浏览器中创建请求，用 CDP 或 route 拦截
            # 最简方案：在浏览器中执行 fetch，把结果分批 postMessage 给我们

            # 注入一个全局函数用于流式收集
            await self.page.evaluate("""
                () => {
                    window.__ds_stream_chunks = [];
                    window.__ds_stream_done = false;
                    window.__ds_stream_error = null;
                }
            """)

            # 启动 fetch（不 await，让它在后台跑）
            prompt_escaped = json.dumps(message)
            await self.page.evaluate("""
                (params) => {
                    const {token, session_id, prompt, pow_header} = params;
                    const headers = {
                        'Content-Type': 'application/json',
                        'Accept': 'text/event-stream',
                        'Authorization': 'Bearer ' + token,
                    };
                    if (pow_header) {
                        headers['x-ds-pow-response'] = pow_header;
                    }

                    fetch('/api/v0/chat/completion', {
                        method: 'POST',
                        credentials: 'include',
                        headers: headers,
                        body: JSON.stringify({
                            chat_session_id: session_id,
                            parent_message_id: null,
                            prompt: prompt,
                            ref_file_ids: [],
                            thinking_enabled: false,
                            search_enabled: false,
                        }),
                    }).then(async (resp) => {
                        const reader = resp.body.getReader();
                        const decoder = new TextDecoder();
                        let buffer = '';

                        while (true) {
                            const {done, value} = await reader.read();
                            if (done) break;

                            buffer += decoder.decode(value, {stream: true});
                            const lines = buffer.split('\\n');
                            buffer = lines.pop();

                            for (const line of lines) {
                                if (line.startsWith('data: ')) {
                                    const data = line.substring(6).trim();
                                    if (data === '[DONE]') {
                                        window.__ds_stream_done = true;
                                        return;
                                    }
                                    try {
                                        const parsed = JSON.parse(data);
                                        const choices = parsed.choices || [];
                                        if (choices.length > 0) {
                                            const content = choices[0].delta?.content || '';
                                            if (content) {
                                                window.__ds_stream_chunks.push(content);
                                            }
                                        }
                                    } catch(e) {}
                                }
                            }
                        }
                        window.__ds_stream_done = true;
                    }).catch((e) => {
                        window.__ds_stream_error = e.message;
                        window.__ds_stream_done = true;
                    });
                }
            """, {
                "token": self._token,
                "session_id": chat_session_id,
                "prompt": message,
                "pow_header": pow_response_header,
            })

            # Step 5: 轮询读取结果
            read_index = 0
            full_text = ""
            max_wait = 300  # 最多等 5 分钟
            waited = 0

            while waited < max_wait:
                result = await self.page.evaluate("""
                    (fromIndex) => {
                        const chunks = window.__ds_stream_chunks.slice(fromIndex);
                        return {
                            chunks: chunks,
                            total: window.__ds_stream_chunks.length,
                            done: window.__ds_stream_done,
                            error: window.__ds_stream_error,
                        };
                    }
                """, read_index)

                if result.get("error"):
                    yield f"[错误] {result['error']}"
                    break

                new_chunks = result.get("chunks", [])
                for chunk in new_chunks:
                    full_text += chunk
                    yield chunk
                    read_index += 1

                if result.get("done") and not new_chunks:
                    break

                await asyncio.sleep(0.1)
                if not new_chunks:
                    waited += 0.1

            # 清理会话
            try:
                await self.page.evaluate("""
                    async (args) => {
                        await fetch('/api/v0/chat_session/delete', {
                            method: 'POST',
                            credentials: 'include',
                            headers: {
                                'Content-Type': 'application/json',
                                'Authorization': 'Bearer ' + args.token,
                            },
                            body: JSON.stringify({chat_session_id: args.sid}),
                        });
                    }
                """, {"token": self._token, "sid": chat_session_id})
            except Exception:
                pass

            print(f"  [{req_id}] ✅ 完成，长度: {len(full_text)}")

        except Exception as e:
            print(f"  [{req_id}] ❌ {e}")
            import traceback
            traceback.print_exc()
            yield f"[错误] {str(e)}"

    def _solve_pow_python(self, challenge: str, salt: str, difficulty: int) -> dict:
        """Python SHA3-256 PoW 求解（兜底）"""
        try:
            for nonce in range(10_000_000):
                input_str = f"{salt}_{nonce}_{challenge}"
                hash_bytes = hashlib.sha3_256(input_str.encode()).digest()

                leading_zeros = 0
                for byte in hash_bytes:
                    if byte == 0:
                        leading_zeros += 8
                    else:
                        b = byte
                        while (b & 0x80) == 0:
                            leading_zeros += 1
                            b <<= 1
                        break
                    if leading_zeros >= difficulty:
                        break

                if leading_zeros >= difficulty:
                    hex_result = hash_bytes.hex()
                    print(f"    PoW Python 求解成功: nonce={nonce}")
                    return {"nonce": str(nonce), "result": hex_result}
        except Exception as e:
            print(f"    PoW Python 异常: {e}")

        return {"error": "Python PoW failed"}

    async def is_alive(self) -> bool:
        if not self.page:
            return False
        try:
            result = await self.page.evaluate("() => document.title")
            return bool(result)
        except Exception:
            return False

    async def get_status(self) -> dict:
        return {
            "logged_in": self.logged_in,
            "mode": "browser-resident",
            "api_base": self._api_base,
            "has_token": bool(self._token),
            "cookie_count": len(self._cookies),
            "uptime_seconds": time.time() - self.start_time,
            "total_requests": self.total_requests,
            "timestamp": datetime.now().isoformat(),
        }

    async def take_screenshot_base64(self) -> Optional[str]:
        if not self.page:
            return None
        try:
            buf = await self.page.screenshot(full_page=False)
            import base64
            return base64.b64encode(buf).decode()
        except Exception:
            return None

    async def simulate_activity(self):
        self.heartbeat_count += 1
        # 保持页面活跃
        if self.page:
            try:
                await self.page.evaluate("() => document.title")
            except Exception:
                pass

    async def shutdown(self):
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            if self.playwright:
                await self.playwright.stop()
            print("🔒 已关闭。")
        except Exception as e:
            print(f"⚠️ {e}")
