# Meeting Assistant - Real-Time AI Teleprompter

一款 AI 驱动的实时会议提词器软件，支持语音识别、大模型生成答案，并根据自定义知识库自动调整回答。

## 核心功能

### 1. 语音识别
- 实时监听会议对话
- 语音转录后手动提交问题
- 支持多语言（中文、英文、日语、韩语等）

### 2. AI 答案生成
- 根据已转录的问题生成便于口头表达的英文回答
- 集成知识库，提供精准答案
- 支持自定义系统提示词

### 3. 提词器窗口
- 始终置顶的透明悬浮窗
- 隐私模式启用系统级内容保护，并降低窗口可见度
- 可自由拖拽移动
- 简洁专业的显示效果

### 4. 知识库管理
- 支持上传 PDF、TXT、DOCX 文件
- 支持直接输入文本
- 文档自动切块，支持中英文关键词检索

### 5. 快捷键操作
- `Ctrl + Shift + H` - 显示/隐藏提词器
- `Ctrl + Shift + M` - 最小化/恢复设置窗口
- `Ctrl + H` - 开启/关闭隐私模式

## 技术架构

### 前端：Electron
- 设置窗口：API配置、知识库管理、语音设置
- 提词器窗口：透明、悬浮、隐私保护

### 后端：Python (FastAPI)
- 语音识别：faster-whisper + PyAudio
- 大模型：OpenAI API（支持自配API Key）
- 知识库：LangChain 文档加载 + 本地关键词检索

### 本地安全
- 桌面端与后端之间使用每次启动随机生成的访问令牌
- 上传文件限制为 PDF、TXT、DOCX，单个文件最大 25 MB
- Electron 渲染页面禁用 Node 集成，并启用内容安全策略
- API Key 只写入项目本地的 `backend/.env`，界面不会回显密钥
- Whisper 模型保存在 `backend/whisper_models/base`，不占用用户级模型缓存

> 隐私模式在 Windows 上会请求系统将窗口排除在大多数屏幕捕获之外。不同会议或录屏软件的实现可能不同，正式会议前请先用实际软件测试一次。

## 安装与运行

首次使用只需在 Windows 中安装两个版本管理器：

```bash
winget install astral-sh.uv
winget install Volta.Volta
```

然后双击 `install.bat`，它会根据 `.python-version`、`uv.lock`、`package.json` 和 `package-lock.json` 恢复隔离环境。

### 手动恢复环境

```bash
uv sync --locked
volta run npm ci
```

### 配置 API Key

复制并编辑配置文件：

```bash
cd backend
copy .env.example .env
```

编辑 `.env` 文件，填入你的 API Key：

```
OPENAI_API_KEY=your-api-key-here
OPENAI_API_BASE=https://api.openai.com/v1
OPENAI_MODEL=gpt-4
```

### 启动应用

```bash
start.bat
```

或者分别启动：

```bash
cd backend
..\.venv\Scripts\python.exe main.py

cd ..
volta run npm start
```

## 使用指南

### 1. 首次使用
- 打开设置窗口，配置 API Key
- 点击 "Test Connection" 测试连接
- 上传文档到知识库（可选）
- 选择语音识别语言

### 2. 开始使用提词器
- 点击 "Open Teleprompter" 打开提词器窗口
- 将提词器窗口拖放到会议软件的合适位置
- 点击 "Start Listening" 开始监听
- 语音转录完成后点击“生成答案”

### 3. 会议中操作
- 问题会显示在蓝色区域
- 生成的英文答案显示在绿色区域
- 可以手动点击 "Generate Answer" 重新生成
- 点击 "Stop Listening" 停止监听

## 参考软件功能对比

本软件参考了 Cheapest Interview 的设计，包含以下相似功能：

| 功能 | Cheapest Interview | Meeting Assistant |
|------|-------------------|-------------------|
| 语音识别 | ✓ | ✓ |
| AI 答案生成 | ✓ | ✓ |
| 知识库支持 | ✓ | ✓ |
| 自定义 API Key | ✓ | ✓ |
| 隐私窗口模式 | ✓ | ✓ |
| 悬浮窗 | ✓ | ✓ |
| 快捷键 | ✓ | ✓ |
| 多语言 | 部分 | ✓ |

## 注意事项

- 请确保 API Key 有效且余额充足
- 首次启动需等待本地服务就绪；首次监听会准备项目目录中的 Whisper base 模型
- 提词器窗口为透明悬浮窗，请合理摆放位置
- 本软件仅用于辅助会议，请合理使用

## 技术栈

- Electron 28
- FastAPI
- LangChain
- faster-whisper + PyAudio
- OpenAI API

## 开发计划

- [ ] 支持更多大模型提供商（通义千问、Kimi、Gemini）
- [ ] 优化语音识别准确率
- [ ] 增加答案历史记录
- [ ] 支持多语言答案输出
- [ ] 优化性能，降低延迟
