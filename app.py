"""
主服务器：FastAPI + WebSocket 代理
外部请求通过 HTTP/WebSocket 进入，由内部浏览器在已认证的上下文中执行。
"""

import os
import sys
import json
import asyncio
import signal
import time
from datetime import datetime
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse, Response
import uvicorn

from browser_manager import BrowserManager
from keepalive import KeepaliveService

# ============================================================
# 全局实例
# ============================================================
browser_mgr: BrowserManager = None
keepalive_svc: KeepaliveService = None

# API 密钥验证
API_SECRET_KEY = os.getenv("API_SECRET_KEY", "")


def verify_api_key(request: Request):
    """验证 API 密钥（如果设置了 API_SECRET_KEY 环境变量）"""
    if not API_SECRET_KEY:
        return  # 未设置密钥则不验证

    # 从 Authorization header 获取
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
        if token == API_SECRET_KEY:
            return

    # 从 query parameter 获取
    token = request.query_params.get("token", "")
    if token == API_SECRET_KEY:
        return

    raise HTTPException(status_code=401, detail="Invalid API key")


# ============================================================
# FastAPI 生命周期管理
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用启动时先让服务器就绪，再在后台初始化浏览器。"""
    global browser_mgr, keepalive_svc

    print(f"\n{'='*60}")
    print(f"  应用启动 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"{'='*60}\n")

    browser_mgr = BrowserManager()
    keepalive_svc = KeepaliveService(browser_mgr)

    asyncio.create_task(initialize_background())

    print("🚀 服务已就绪（浏览器后台初始化中），等待请求...\n")
    yield

    print("\n⏹️  正在关闭服务...")
    if keepalive_svc:
        await keepalive_svc.stop()
    if browser_mgr:
        await browser_mgr.shutdown()
    print("✅ 服务已安全关闭。")


async def initialize_background():
    """后台初始化浏览器，完成后启动心跳服务。"""
    global browser_mgr, keepalive_svc
    print("⏳ 后台任务：开始初始化浏览器...")
    try:
        await browser_mgr.initialize()
        print("✅ 后台任务：浏览器初始化完成。")

        if keepalive_svc and not keepalive_svc.is_running:
            await keepalive_svc.start()
    except Exception as e:
        print(f"❌ 后台任务：浏览器初始化失败: {e}")
        import traceback
        traceback.print_exc()


app = FastAPI(title="DeepSeek Proxy", lifespan=lifespan)


# ============================================================
# 健康检查 & 状态页
# ============================================================
@app.api_route("/", methods=["GET", "HEAD"])
async def index(request: Request):
    """状态仪表盘页面。支持 GET 和 HEAD（Render 健康检查用 HEAD）。"""
    # HEAD 请求直接返回 200，不需要 body
    if request.method == "HEAD":
        return Response(status_code=200)

    status = await browser_mgr.get_status() if browser_mgr else {"status": "initializing"}
    uptime = status.get("uptime_seconds", 0)
    hours, remainder = divmod(int(uptime), 3600)
    minutes, seconds = divmod(remainder, 60)

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>DeepSeek Proxy</title>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #0d1117; color: #c9d1d9; 
                   display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }}
            .card {{ background: #161b22; border: 1px solid #30363d; border-radius: 12px; 
                     padding: 40px; max-width: 500px; width: 90%; }}
            h1 {{ color: #58a6ff; margin-top: 0; }}
            .status {{ display: flex; align-items: center; gap: 10px; margin: 20px 0; }}
            .dot {{ width: 12px; height: 12px; border-radius: 50%; 
                    background: {"#3fb950" if status.get("browser_alive") else "#f85149"}; }}
            .info {{ background: #21262d; padding: 15px; border-radius: 8px; margin: 10px 0; 
                     font-family: monospace; font-size: 14px; }}
            .label {{ color: #8b949e; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h1>🤖 DeepSeek Proxy</h1>
            <div class="status">
                <div class="dot"></div>
                <span>{"运行中" if status.get("browser_alive") else "离线"}</span>
            </div>
            <div class="info">
                <div><span class="label">运行时间：</span>{hours}h {minutes}m {seconds}s</div>
                <div><span class="label">登录状态：</span>{"✅ 已登录" if status.get("logged_in") else "❌ 未登录"}</div>
                <div><span class="label">心跳次数：</span>{status.get("heartbeat_count", 0)}</div>
                <div><span class="label">处理请求：</span>{status.get("requests_handled", 0)}</div>
                <div><span class="label">引擎：</span>{status.get("engine", "N/A")}</div>
            </div>
            <p style="color: #8b949e; font-size: 12px;">
                POST /v1/chat/completions 发送聊天请求<br>
                WS /ws 建立 WebSocket 连接
            </p>
        </div>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@app.api_route("/health", methods=["GET", "HEAD"])
async def health(request: Request):
    """健康检查端点：只要服务进程活着就返回200，不依赖浏览器状态。"""
    if request.method == "HEAD":
        return Response(status_code=200)
    return {"status": "ok"}


@app.get("/status")
async def status():
    """详细状态端点。"""
    if browser_mgr:
        return await browser_mgr.get_status()
    return {"status": "initializing"}


@app.get("/screenshot")
async def screenshot():
    """获取当前浏览器截图（Base64），用于调试。"""
    if not browser_mgr:
        raise HTTPException(status_code=503, detail="浏览器未就绪")
    img_base64 = await browser_mgr.take_screenshot_base64()
    if img_base64:
        html = f"""
        <html><body style="background:#000;display:flex;justify-content:center;padding:20px;">
        <img src="data:image/png;base64,{img_base64}" style="max-width:100%;border:1px solid #333;"/>
        </body></html>
        """
        return HTMLResponse(content=html)
    raise HTTPException(status_code=500, detail="截图失败")


# ============================================================
# 辅助函数
# ============================================================
def build_prompt_from_messages(messages: list) -> str:
    prompt_parts = []
    prompt_parts.append("请根据以下对话历史和最后一个用户对话，生成对应的回复。")

    for message in messages:
        role = message.get("role", "")
        content = message.get("content", "")

        processed_content = ""
        if isinstance(content, str):
            processed_content = content
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    processed_content += part.get("text", "")

        if role and processed_content:
            prompt_parts.append(f"角色: {role}\n内容: {processed_content}")

    return "\n\n---\n\n".join(prompt_parts)


# ============================================================
# 核心 API：兼容 OpenAI 格式的聊天接口
# ============================================================
@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    # 验证 API 密钥
    verify_api_key(request)

    if not browser_mgr or not await browser_mgr.is_alive():
        raise HTTPException(status_code=503, detail="浏览器会话未就绪")

    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="无效的 JSON 请求体")

    messages = body.get("messages", [])
    if not messages:
        raise HTTPException(status_code=400, detail="messages 不能为空")

    stream = body.get("stream", False)

    user_prompt = build_prompt_from_messages(messages)

    if not user_prompt:
        raise HTTPException(status_code=400, detail="未找到有效的用户输入")

    print(f"📝 构建的 prompt 长度: {len(user_prompt)} 字符")

    if stream:
        async def generate():
            async for chunk in browser_mgr.send_message_stream(user_prompt):
                data = {
                    "id": f"chatcmpl-{int(time.time()*1000)}",
                    "object": "chat.completion.chunk",
                    "created": int(time.time()),
                    "model": "deepseek-chat",
                    "choices": [{
                        "index": 0,
                        "delta": {"content": chunk},
                        "finish_reason": None
                    }]
                }
                yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"

            end_data = {
                "id": f"chatcmpl-{int(time.time()*1000)}",
                "object": "chat.completion.chunk",
                "created": int(time.time()),
                "model": "deepseek-chat",
                "choices": [{
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop"
                }]
            }
            yield f"data: {json.dumps(end_data, ensure_ascii=False)}\n\n"
            yield "data: [DONE]\n\n"

        return StreamingResponse(generate(), media_type="text/event-stream")
    else:
        response_text = await browser_mgr.send_message(user_prompt)
        return {
            "id": f"chatcmpl-{int(time.time()*1000)}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": "deepseek-chat",
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": response_text},
                "finish_reason": "stop"
            }],
            "usage": {
                "prompt_tokens": len(user_prompt),
                "completion_tokens": len(response_text),
                "total_tokens": len(user_prompt) + len(response_text)
            }
        }


# ============================================================
# WebSocket 端点
# ============================================================
@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    print(f"📡 WebSocket 客户端已连接: {websocket.client}")

    try:
        while True:
            data = await websocket.receive_text()
            try:
                payload = json.loads(data)
                message = payload.get("message", "")
            except json.JSONDecodeError:
                message = data

            if not message:
                await websocket.send_json({"error": "消息不能为空"})
                continue

            if not browser_mgr or not await browser_mgr.is_alive():
                await websocket.send_json({"error": "浏览器会话未就绪"})
                continue

            await websocket.send_json({"type": "start"})
            full_response = ""
            async for chunk in browser_mgr.send_message_stream(message):
                full_response += chunk
                await websocket.send_json({"type": "chunk", "content": chunk})
            await websocket.send_json({"type": "end", "full_content": full_response})

    except WebSocketDisconnect:
        print(f"📡 WebSocket 客户端已断开: {websocket.client}")
    except Exception as e:
        print(f"❌ WebSocket 错误: {e}")
        try:
            await websocket.send_json({"error": str(e)})
        except Exception:
            pass


# ============================================================
# 入口
# ============================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
