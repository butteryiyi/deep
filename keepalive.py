"""
心跳保活服务：
- 定期模拟用户活动，防止会话过期
- 定期检查登录状态，失效时自动重新注入 Cookie
- 定期刷新页面，防止前端 SPA 状态丢失
- 定期检测长时间忙碌的页面，执行轻量截图唤醒
- 【新增】检测浏览器进程崩溃，自动完整重启
"""

import time
import asyncio
from browser_manager import BrowserManager


class KeepaliveService:
    def __init__(
        self,
        browser_mgr: BrowserManager,
        interval: int = 30,
        login_check_interval: int = 180,
        page_refresh_interval: int = 1800,
        busy_page_timeout: int = 180,
        busy_rescue_check_interval: int = 60,
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

        self._page_busy_since: dict[str, float] = {}
        self._page_rescue_count: dict[str, int] = {}

        # ── 新增：浏览器重启相关 ──
        self._last_restart_attempt = 0.0
        self._restart_cooldown = 60.0        # 重启冷却时间（秒），失败后递增
        self._restart_count = 0              # 连续重启次数
        self._max_restart_count = 10         # 最大连续重启次数
        self._consecutive_dead_checks = 0    # 连续检测到浏览器死亡的次数

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

                # ── 关键修复：浏览器死亡时尝试重启，而不是跳过 ──
                if not await self.browser_mgr.is_alive():
                    self._consecutive_dead_checks += 1
                    print(f"💀 浏览器不可用（连续第 {self._consecutive_dead_checks} 次检测）")

                    # 连续 2 次检测到死亡才触发重启（避免偶发性误判）
                    if self._consecutive_dead_checks >= 2:
                        await self._try_restart_browser()
                    continue

                # 浏览器活着，重置死亡计数
                self._consecutive_dead_checks = 0

                # ── 1. 常规模拟活动（每次心跳都做） ──
                await self.browser_mgr.simulate_activity()

                now = time.time()

                # ── 2. 定期检查登录状态 ──
                if now - self._last_login_check >= self.login_check_interval:
                    self._last_login_check = now
                    await self._check_and_fix_login()

                # ── 3. 定期刷新空闲页面 ──
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

    # ═══════════════════════════════════════════════════════════════
    # 新增：浏览器完整重启
    # ═══════════════════════════════════════════════════════════════
    async def _try_restart_browser(self):
        """尝试完整重启浏览器"""
        now = time.time()

        # 检查冷却时间
        if now - self._last_restart_attempt < self._restart_cooldown:
            remaining = self._restart_cooldown - (now - self._last_restart_attempt)
            print(f"  ⏳ 重启冷却中，还需等待 {remaining:.0f}s")
            return

        # 检查是否超过最大重启次数
        if self._restart_count >= self._max_restart_count:
            print(f"  ❌ 已连续重启 {self._restart_count} 次均失败，"
                  f"停止自动重启，需要人工干预！")
            # 每 10 分钟重置一次，给一次新的机会
            if now - self._last_restart_attempt > 600:
                print(f"  🔄 距上次重启已超过 10 分钟，重置重启计数，再试一次...")
                self._restart_count = 0
                self._restart_cooldown = 60.0
            else:
                return

        self._last_restart_attempt = now
        self._restart_count += 1

        print(f"\n🔄 浏览器不可用，尝试第 {self._restart_count} 次完整重启...")

        try:
            if not hasattr(self.browser_mgr, 'restart'):
                print("  ❌ BrowserManager 没有 restart() 方法，无法自动重启")
                return

            success = await self.browser_mgr.restart()

            if success:
                print(f"  ✅ 浏览器重启成功！")
                self._restart_count = 0
                self._restart_cooldown = 60.0
                self._consecutive_dead_checks = 0
                # 重置其他计时器
                now2 = time.time()
                self._last_login_check = now2
                self._last_page_refresh = now2
                self._last_busy_rescue_check = now2
                self._login_check_failures = 0
                self._page_busy_since.clear()
                self._page_rescue_count.clear()
            else:
                print(f"  ❌ 浏览器重启失败（第 {self._restart_count} 次）")
                # 递增冷却时间：60s → 120s → 240s → ... → 最大600s
                self._restart_cooldown = min(self._restart_cooldown * 2, 600)
                print(f"  ⏳ 下次重启冷却时间: {self._restart_cooldown:.0f}s")

        except Exception as e:
            print(f"  ❌ 浏览器重启异常: {e}")
            import traceback
            traceback.print_exc()
            self._restart_cooldown = min(self._restart_cooldown * 2, 600)

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

            print("  🔄 刷新无效，开始重新注入 Cookie...")
            success = await self.browser_mgr.re_login()
            if success:
                self._login_check_failures = 0
                print("  ✅ 重新登录成功！")
            else:
                print(f"  ❌ 重新登录失败（第 {self._login_check_failures} 次），"
                      f"下次心跳将继续尝试")

                # 如果连续多次重新登录失败，可能需要完整重启
                if self._login_check_failures >= 5:
                    print(f"  ⚠️ 连续 {self._login_check_failures} 次登录失败，"
                          f"尝试完整重启浏览器...")
                    await self._try_restart_browser()

        except Exception as e:
            print(f"⚠️ 登录状态检查异常: {e}")

    async def _refresh_idle_pages(self):
        """定期刷新空闲页面"""
        try:
            count = await self.browser_mgr.refresh_idle_pages()
            if count > 0:
                print(f"💓 已刷新 {count} 个空闲页面")
        except Exception as e:
            err_msg = str(e)
            print(f"⚠️ 刷新空闲页面异常: {e}")
            # 如果是浏览器级别的崩溃，标记以便下一轮触发重启
            if "has been closed" in err_msg or "Target closed" in err_msg:
                self._consecutive_dead_checks = 2  # 立即触发重启

    async def _rescue_stuck_busy_pages(self):
        """检测长时间处于忙碌状态的页面，执行轻量截图唤醒"""
        try:
            # ── 修复：使用正确的属性名 _pages（而非 context_pages） ──
            if not self.browser_mgr._pages:
                return

            now = time.time()
            current_busy_ids = set()

            for cp in self.browser_mgr._pages:
                page_id = id(cp)
                page_id_str = str(page_id)

                if cp.busy:
                    current_busy_ids.add(page_id_str)

                    if page_id_str not in self._page_busy_since:
                        self._page_busy_since[page_id_str] = now
                        continue

                    busy_duration = now - self._page_busy_since[page_id_str]

                    if busy_duration < self.busy_page_timeout:
                        continue

                    rescue_count = self._page_rescue_count.get(page_id_str, 0)
                    rescue_count += 1
                    self._page_rescue_count[page_id_str] = rescue_count

                    print(f"🚑 检测到页面#{cp.page_id}已忙碌 {busy_duration:.0f}s"
                          f"（超过 {self.busy_page_timeout}s），"
                          f"执行轻量截图唤醒（第 {rescue_count} 次救援）")

                    try:
                        await self._lightweight_screenshot(cp)
                        print(f"  ✅ 轻量截图完成，页面应已被唤醒")
                    except Exception as e:
                        print(f"  ❌ 轻量截图失败: {e}")

                    self._page_busy_since[page_id_str] = now

                    if rescue_count >= 3:
                        print(f"  ⚠️ 页面#{cp.page_id}已被救援 {rescue_count} 次"
                              f"仍处于忙碌状态，可能需要人工干预")

                else:
                    if page_id_str in self._page_busy_since:
                        del self._page_busy_since[page_id_str]
                    if page_id_str in self._page_rescue_count:
                        del self._page_rescue_count[page_id_str]

            # 清理已不存在的页面记录
            all_page_ids = {str(id(cp)) for cp in self.browser_mgr._pages}
            stale_ids = set(self._page_busy_since.keys()) - all_page_ids
            for stale_id in stale_ids:
                self._page_busy_since.pop(stale_id, None)
                self._page_rescue_count.pop(stale_id, None)

        except Exception as e:
            print(f"⚠️ 救援卡死页面异常: {e}")

    async def _lightweight_screenshot(self, cp):
        """对指定页面执行轻量截图，触发浏览器渲染管线唤醒"""
        page = cp.page
        if not page or page.is_closed():
            raise RuntimeError("页面已关闭，无法截图")

        try:
            await page.screenshot(
                clip={"x": 0, "y": 0, "width": 1, "height": 1},
                timeout=10000,
            )
            return
        except Exception:
            pass

        try:
            await page.evaluate(
                "() => { document.hidden; window.innerHeight; "
                "document.querySelectorAll('*').length; }",
                timeout=10000,
            )
            return
        except Exception:
            pass

        await page.screenshot(timeout=15000)
