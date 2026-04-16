"""
心跳保活服务：
- 定期模拟用户活动，防止会话过期
- 定期检查登录状态，失效时自动重新注入 Cookie
- 定期刷新页面，防止前端 SPA 状态丢失
- 定期检测长时间忙碌的页面，执行轻量截图唤醒，防止页面陷入冻结
"""

import time
import asyncio
from browser_manager import BrowserManager


class KeepaliveService:
    def __init__(
        self,
        browser_mgr: BrowserManager,
        interval: int = 30,
        login_check_interval: int = 180,        # 每 3 分钟检查一次登录状态
        page_refresh_interval: int = 1800,       # 每 30 分钟强制刷新一次空闲页面
        busy_page_timeout: int = 180,            # 忙碌超过 3 分钟视为卡死
        busy_rescue_check_interval: int = 60,    # 每 60 秒检查一次是否有卡死页面
    ):
        self.browser_mgr = browser_mgr
        self.interval = interval
        self.login_check_interval = login_check_interval
        self.page_refresh_interval = page_refresh_interval
        self.busy_page_timeout = busy_page_timeout
        self.busy_rescue_check_interval = busy_rescue_check_interval
        self._task: asyncio.Task = None
        self._running = False
        self.is_running = False

        self._last_login_check = 0.0
        self._last_page_refresh = 0.0
        self._last_busy_rescue_check = 0.0
        self._login_check_failures = 0

        # 记录每个页面进入忙碌状态的时间 {page_id: busy_start_time}
        self._page_busy_since: dict[str, float] = {}
        # 记录对卡死页面已执行的救援次数 {page_id: count}
        self._page_rescue_count: dict[str, int] = {}

    async def start(self):
        if self.is_running:
            return
        self._running = True
        self.is_running = True
        now = time.time()
        self._last_login_check = now
        self._last_page_refresh = now
        self._last_busy_rescue_check = now
        self._task = asyncio.create_task(self._heartbeat_loop())
        print(f"💓 心跳服务已启动（活动间隔: {self.interval}s, "
              f"登录检查间隔: {self.login_check_interval}s, "
              f"页面刷新间隔: {self.page_refresh_interval}s, "
              f"忙碌超时: {self.busy_page_timeout}s）")

    async def stop(self):
        self._running = False
        self.is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        print("💔 心跳服务已停止。")

    async def _heartbeat_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.interval)

                if not self._running or not self.browser_mgr:
                    continue

                if not await self.browser_mgr.is_alive():
                    continue

                # ── 1. 常规模拟活动（每次心跳都做） ──
                await self.browser_mgr.simulate_activity()

                now = time.time()

                # ── 2. 定期检查登录状态 ──
                if now - self._last_login_check >= self.login_check_interval:
                    self._last_login_check = now
                    await self._check_and_fix_login()

                # ── 3. 定期刷新空闲页面（防止前端 SPA 状态腐化） ──
                if now - self._last_page_refresh >= self.page_refresh_interval:
                    self._last_page_refresh = now
                    await self._refresh_idle_pages()

                # ── 4. 定期检查并救援卡死的忙碌页面 ──
                if now - self._last_busy_rescue_check >= self.busy_rescue_check_interval:
                    self._last_busy_rescue_check = now
                    await self._rescue_stuck_busy_pages()

            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️ 心跳循环异常: {e}")
                await asyncio.sleep(5)

    async def _check_and_fix_login(self):
        """检查登录状态，如果失效则自动重新登录"""
        try:
            is_logged_in = await self.browser_mgr.check_login_status()

            if is_logged_in:
                if self._login_check_failures > 0:
                    print(f"💓 登录状态已恢复正常")
                self._login_check_failures = 0
                return

            self._login_check_failures += 1
            print(f"⚠️ 检测到登录状态失效 "
                  f"(连续 {self._login_check_failures} 次)")

            # 第一次检测失败：先尝试简单刷新页面
            if self._login_check_failures == 1:
                print("  🔄 尝试刷新页面恢复...")
                refreshed = await self.browser_mgr.refresh_idle_pages()
                if refreshed:
                    await asyncio.sleep(3)
                    recheck = await self.browser_mgr.check_login_status()
                    if recheck:
                        print("  ✅ 刷新页面后登录状态恢复！")
                        self._login_check_failures = 0
                        return

            # 第二次及之后：重新注入 Cookie
            print("  🔄 刷新无效，开始重新注入 Cookie...")
            success = await self.browser_mgr.re_login()
            if success:
                self._login_check_failures = 0
                print("  ✅ 重新登录成功！")
            else:
                print(f"  ❌ 重新登录失败（第 {self._login_check_failures} 次），"
                      f"下次心跳将继续尝试")

        except Exception as e:
            print(f"⚠️ 登录状态检查异常: {e}")

    async def _refresh_idle_pages(self):
        """定期刷新空闲页面，防止前端状态腐化"""
        try:
            count = await self.browser_mgr.refresh_idle_pages()
            if count > 0:
                print(f"💓 已刷新 {count} 个空闲页面")
        except Exception as e:
            print(f"⚠️ 刷新空闲页面异常: {e}")

    async def _rescue_stuck_busy_pages(self):
        """
        检测长时间处于忙碌状态的页面，执行轻量截图唤醒。
        
        策略：
        - 页面忙碌超过 busy_page_timeout（默认3分钟）→ 执行轻量截图
        - 截图不会中断正在进行的对话，只是触发浏览器渲染管线
        - 如果同一页面被反复救援超过 3 次，说明可能真的卡死，打印警告
        """
        try:
            if not hasattr(self.browser_mgr, 'context_pages') or \
               not self.browser_mgr.context_pages:
                return

            now = time.time()
            current_busy_ids = set()

            for cp in self.browser_mgr.context_pages:
                page_id = id(cp)
                page_id_str = str(page_id)

                if cp.busy:
                    current_busy_ids.add(page_id_str)

                    # 第一次发现该页面处于忙碌状态，记录起始时间
                    if page_id_str not in self._page_busy_since:
                        self._page_busy_since[page_id_str] = now
                        continue

                    busy_duration = now - self._page_busy_since[page_id_str]

                    # 忙碌时间未超过阈值，跳过
                    if busy_duration < self.busy_page_timeout:
                        continue

                    # ── 超时！执行轻量截图唤醒 ──
                    rescue_count = self._page_rescue_count.get(page_id_str, 0)
                    rescue_count += 1
                    self._page_rescue_count[page_id_str] = rescue_count

                    print(f"🚑 检测到页面已忙碌 {busy_duration:.0f}s（超过 "
                          f"{self.busy_page_timeout}s），执行轻量截图唤醒 "
                          f"（第 {rescue_count} 次救援）")

                    try:
                        await self._lightweight_screenshot(cp)
                        print(f"  ✅ 轻量截图完成，页面应已被唤醒")
                    except Exception as e:
                        print(f"  ❌ 轻量截图失败: {e}")

                    # 重置忙碌起始时间，避免下一轮立即再次触发
                    self._page_busy_since[page_id_str] = now

                    # 多次救援仍未恢复，发出严重警告
                    if rescue_count >= 3:
                        print(f"  ⚠️ 该页面已被救援 {rescue_count} 次仍处于忙碌状态，"
                              f"可能需要人工干预或强制重启")

                else:
                    # 页面不再忙碌，清除跟踪记录
                    if page_id_str in self._page_busy_since:
                        del self._page_busy_since[page_id_str]
                    if page_id_str in self._page_rescue_count:
                        del self._page_rescue_count[page_id_str]

            # 清理已不存在的页面记录（页面被销毁或替换的情况）
            stale_ids = set(self._page_busy_since.keys()) - current_busy_ids - \
                        {str(id(cp)) for cp in self.browser_mgr.context_pages if not cp.busy}
            for stale_id in stale_ids:
                self._page_busy_since.pop(stale_id, None)
                self._page_rescue_count.pop(stale_id, None)

        except Exception as e:
            print(f"⚠️ 救援卡死页面异常: {e}")

    async def _lightweight_screenshot(self, cp):
        """
        对指定的 ContextPage 执行轻量截图操作。
        
        这不是为了保存图片，而是为了触发浏览器的渲染管线，
        唤醒可能因为后台节流而冻结的页面进程。
        
        关键：使用较小的截图区域、较短的超时，避免对正在运行的任务造成影响。
        """
        page = cp.page
        if not page or page.is_closed():
            raise RuntimeError("页面已关闭，无法截图")

        # 方法 1：优先尝试截取一个极小区域（最轻量）
        try:
            await page.screenshot(
                clip={"x": 0, "y": 0, "width": 1, "height": 1},
                timeout=10000,  # 10 秒超时
            )
            return
        except Exception:
            pass

        # 方法 2：回退到执行一段 JS 触发渲染
        try:
            await page.evaluate(
                "() => { document.hidden; window.innerHeight; "
                "document.querySelectorAll('*').length; }",
                timeout=10000,
            )
            return
        except Exception:
            pass

        # 方法 3：最后尝试完整截图
        await page.screenshot(timeout=15000)
