
# DeepSeek Proxy

本项目提供一个基于浏览器的 DeepSeek 聊天代理服务，通过 Playwright 控制真实浏览器与 DeepSeek 网页交互，并对外提供兼容 OpenAI 格式的 HTTP 和 WebSocket 接口。采用 **Cookie 注入** 认证，无需账号密码，只需在本地手动登录一次并导出认证数据，即可在服务器端复用会话。

## ✨ 功能特点

- 🚀 **完全模拟真实用户**：浏览器自动处理反爬、验证码等，无需担心接口变更
- 🔑 **Cookie 注入认证**：只需在本地手动登录一次，导出 Cookie 和 localStorage，部署后自动注入
- 🌐 **兼容 OpenAI API**：支持 `/v1/chat/completions`（**流式和非流式**）和 `/v1/models`
- 📡 **WebSocket 实时通信**：支持流式传输，体验打字机效果
- 📦 **多种部署方式**：支持 Render、本地运行，也可轻松 Docker 部署
- 💓 **心跳保活**：定期模拟活动，防止会话过期
- 📸 **调试支持**：提供 `/screenshot` 端点查看浏览器当前状态

## 🚀 部署方式

### 一、部署到 Render

#### 1. 准备工作：导出认证数据（**必须在本地电脑操作**）

由于 Render 无法直接登录 DeepSeek 账号，你需要在 **本地电脑** 上运行导出工具，获取认证数据后作为环境变量配置。

##### 本地导出详细步骤

- **确保 Python 环境**  
  需要 Python 3.10 或更高版本（建议使用虚拟环境）。

- **安装依赖**  
  在项目目录下执行：
  ```bash
  pip install -r requirements.txt
  python -m playwright install chromium   # 安装 Chromium 浏览器（用于导出工具）
  ```

- **运行导出脚本**  
  ```bash
  python export_cookies.py
  ```
  脚本会自动打开一个 Chromium 浏览器窗口（有界面）。**请手动登录你的 DeepSeek 账号**（输入邮箱/密码或扫码），直到看到聊天主界面（即 URL 变为 `https://chat.deepseek.com/` 且不再有 `sign_in`）。

- **等待导出完成**  
  登录成功后，回到终端。脚本会自动检测登录状态，并提示“检测到登录成功”。如果自动检测失败，可以手动在终端按 `Enter` 键继续。

- **获取认证数据**  
  脚本会做以下事情：
  - 将完整认证数据保存到本地文件 `deepseek_auth.json`（可用于本地部署）。
  - 在终端输出一段 **JSON 字符串**，并标明这是环境变量 `DEEPSEEK_AUTH` 的值。**复制整个 JSON 字符串**（从 `{` 到 `}` 包括所有内容）。

  > ⚠️ 务必完整复制，不要遗漏任何字符。该字符串可能很长，请确保复制完整。

- **关闭浏览器**  
  脚本会自动关闭浏览器，导出完成。

#### 2. 在 Render 上部署

- **上传代码**  
  将本项目代码推送到你的 GitHub 仓库（或直接使用 Git 部署）。

- **新建 Web Service**  
  在 Render 控制台点击 **New +** → **Web Service**，选择你的仓库。

- **配置服务**  
  按以下设置：
  - **Environment**: `Python 3`
  - **Build Command**:
    ```bash
    pip install -r requirements.txt && python -m playwright install firefox
    ```
  - **Start Command**:
    ```bash
    python app.py
    ```

- **添加环境变量**  
  在 Render 的 **Environment** 选项卡中添加以下两个 **Secret** 变量：
  - `DEEPSEEK_AUTH`：**粘贴刚才复制的完整 JSON 字符串**（注意不要有多余空格）
  - `API_SECRET_KEY`：自定义一个 API 密钥（例如 `sk-123456`），用于后续调用接口

- **部署**  
  点击 **Create Web Service**。Render 会自动构建并启动。构建过程可能需要几分钟（因为需要下载 Playwright 浏览器）。

- **访问服务**  
  部署成功后，访问 `https://你的服务名.onrender.com` 查看状态页。如果显示“运行中”，则服务已就绪。

> ⚠️ Render 免费实例可能因不活跃而休眠，建议搭配 [UptimeRobot](https://uptimerobot.com) 等定期 ping 你的服务保持活跃。

---

### 二、本地部署

#### 1. 克隆项目并安装依赖

```bash
git clone <仓库地址>
cd <项目目录>
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate
pip install -r requirements.txt
python -m playwright install firefox   # 安装 Firefox（用于运行服务）
```

#### 2. 获取认证数据（与 Render 准备工作相同）

重复上面“本地导出详细步骤”中的操作，运行 `export_cookies.py` 手动登录并导出。导出后，你会获得：
- 本地文件 `deepseek_auth.json`
- 终端输出的 `DEEPSEEK_AUTH` 环境变量值

#### 3. 运行服务

有两种方式提供认证数据：

- **方式一（推荐测试）：直接使用本地文件**  
  确保 `deepseek_auth.json` 存在于当前目录，然后启动服务：
  ```bash
  python app.py
  ```
  程序会自动读取该文件中的认证数据。

- **方式二（生产推荐）：使用环境变量**  
  导出环境变量后再启动：
  ```bash
  export DEEPSEEK_AUTH='{"cookies":[...]}'   # 粘贴完整的 JSON
  export API_SECRET_KEY='sk-123456'
  python app.py
  ```

服务默认监听 `http://0.0.0.0:7860`，可通过 `--port` 修改端口。

#### 4. 验证

访问 `http://localhost:7860` 查看状态页，或直接调用 API（见下文）。

---

## 📡 API 使用说明

### 鉴权

所有请求（除 `/`、`/health` 外）都需要提供 API Key，支持以下方式之一：

- `Authorization: Bearer <API_SECRET_KEY>`
- `X-API-Key: <API_SECRET_KEY>`
- 查询参数 `?api_key=<API_SECRET_KEY>`

### 模型列表

```bash
curl -H "Authorization: Bearer <API_SECRET_KEY>" http://localhost:7860/v1/models
```

响应示例：
```json
{
  "object": "list",
  "data": [
    {
      "id": "deepseek-chat",
      "object": "model",
      "created": 1700000000,
      "owned_by": "deepseek-proxy"
    }
  ]
}
```

### 聊天补全（支持流式输出）

#### 非流式请求

```bash
curl -X POST http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer <API_SECRET_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [
      {"role": "system", "content": "你是一个助手"},
      {"role": "user", "content": "你好"}
    ]
  }'
```

响应格式与 OpenAI 完全一致。

#### 流式请求（体验打字机效果）

只需在请求体中添加 `"stream": true`：

```bash
curl -X POST http://localhost:7860/v1/chat/completions \
  -H "Authorization: Bearer <API_SECRET_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "deepseek-chat",
    "messages": [{"role": "user", "content": "写一首诗"}],
    "stream": true
  }'
```

服务器会返回 `text/event-stream` 格式的数据，每个 chunk 包含部分内容，最后以 `[DONE]` 结束。

### WebSocket 接口（实时流式）

连接地址：`ws://localhost:7860/ws?api_key=<API_SECRET_KEY>`  
或连接后在第一条消息中发送 JSON `{"api_key": "<API_SECRET_KEY>"}` 进行认证。

发送消息格式（JSON 或纯文本）：
```json
{"message": "你的问题"}
```

服务端会依次返回：
- `{"type": "start"}`
- 多个 `{"type": "chunk", "content": "部分回复"}`（可实现打字效果）
- `{"type": "end", "full_content": "完整回复"}`

---

## ⚙️ 配置选项

| 环境变量 | 说明 | 默认值 |
|---------|------|--------|
| `DEEPSEEK_AUTH` | **推荐**：JSON 字符串，包含 cookies、localStorage 等完整认证数据 | 无 |
| `DEEPSEEK_TOKEN` | 仅 Token（如 `ds_token`），自动注入 localStorage | 无 |
| `DEEPSEEK_COOKIES` | Cookie 数组 JSON（与 `DEEPSEEK_LOCAL_STORAGE` 配合） | 无 |
| `DEEPSEEK_LOCAL_STORAGE` | localStorage 对象 JSON | 无 |
| `API_SECRET_KEY` | API 密钥，用于接口鉴权 | `zxcvbnm` |
| `HEADLESS` | 是否以无头模式运行浏览器（本地调试时可设为 `false`） | `true` |

> **优先级**：`DEEPSEEK_AUTH` > `DEEPSEEK_TOKEN` > `DEEPSEEK_COOKIES` > 本地文件 `deepseek_auth.json`

---

## ❓ 常见问题

### Q: 导出的 Cookie 会过期吗？
A: 会。DeepSeek 的登录会话通常持续数天至数周。如果遇到登录失效（API 返回错误或状态页显示“未登录”），请重新运行 `export_cookies.py` 并更新环境变量或本地文件。

### Q: 为什么一定要在本地导出 Cookie，不能直接在 Render 上登录？
A: Render 等云平台运行的是无头浏览器，无法手动操作登录，且容易被 DeepSeek 识别为机器人。本地导出可确保你通过正常的浏览器登录，获取可信的会话凭证。

### Q: 流式输出和打字机效果怎么实现？
A: 本项目已内置支持。对于 HTTP 请求，设置 `stream: true` 即可；对于 WebSocket，服务端会逐块返回 `chunk` 消息，前端可以边接收边显示，实现打字效果。

### Q: 浏览器初始化失败怎么办？
A: 检查是否已正确安装 Playwright 浏览器：`python -m playwright install firefox`。如果使用 Chromium，可在 `browser_manager.py` 中修改 `browser_type` 为 `chromium`。同时确保系统依赖已安装（参见 Dockerfile）。

### Q: 如何调试浏览器界面？
A: 本地运行时，设置环境变量 `HEADLESS=false`，浏览器将显示窗口，你可以观察其行为。同时可以访问 `/screenshot` 端点查看当前浏览器截图。

### Q: 心跳服务有什么作用？
A: 定期（默认 30 秒）在页面上执行简单的活动（如滚动），防止长时间无操作导致会话被服务器强制断开。

---

## 📁 项目结构

```
.
├── app.py                 # FastAPI 主服务
├── browser_manager.py     # 浏览器生命周期管理、消息发送
├── auth_handler.py        # Cookie 注入认证
├── keepalive.py           # 心跳保活
├── export_cookies.py      # 本地导出认证数据工具
├── requirements.txt       # Python 依赖
├── Dockerfile             # Docker 构建文件
└── README.md              # 本文档
```

---

## 📄 许可证

MIT License
