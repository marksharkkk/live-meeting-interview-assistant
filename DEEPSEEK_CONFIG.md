# DeepSeek API 和本地语音配置指南

## DeepSeek API 配置

DeepSeek API 完全兼容 OpenAI 格式，可以直接使用！

### 方法一：快捷选择（推荐）

在设置页面，点击 **DeepSeek** 快捷按钮即可自动配置。

### 方法二：手动配置

编辑 `backend\.env` 文件：

```
OPENAI_API_KEY=your-deepseek-api-key
OPENAI_API_BASE=https://api.deepseek.com/v1
OPENAI_MODEL=deepseek-chat
```

### 支持的模型

- `deepseek-chat` - DeepSeek 聊天模型
- `deepseek-coder` - DeepSeek 代码模型
- 其他 OpenAI 兼容模型

### 获取 DeepSeek API Key

访问 https://platform.deepseek.com 注册并获取 API Key

---

## 本地语音识别（离线使用）

语音识别统一使用 faster-whisper 本地模式，不会把会议音频发送给 Google 等在线语音服务。模型保存在项目的 `backend/whisper_models/base`。

**同步项目环境：**
```bash
uv sync --locked
```

**注意事项：**
- 项目使用 faster-whisper，依赖由 `uv.lock` 统一管理
- 首次启用语音识别时会准备项目目录中的 faster-whisper base 模型，之后直接复用该项目副本
- 需要较好的 CPU/GPU 性能
- 延迟比在线模式略高
- 完全离线，不依赖网络

---

## 支持的 AI 平台

本软件支持所有 OpenAI API 兼容的平台：

| 平台 | Base URL | 推荐模型 |
|------|----------|----------|
| OpenAI | https://api.openai.com/v1 | gpt-4, gpt-4o |
| DeepSeek | https://api.deepseek.com/v1 | deepseek-chat |
| Kimi | https://api.moonshot.cn/v1 | moonshot-v1-8k |
| 通义千问 | https://dashscope.aliyuncs.com/compatible-mode/v1 | qwen-turbo |
| 其他 | 根据文档配置 | - |

---

## 快速开始

### 使用 DeepSeek

1. 获取 DeepSeek API Key
2. 在设置页面点击 DeepSeek 快捷按钮
3. 填入 API Key
4. 点击"测试连接"
5. 打开提词器开始使用

### 使用本地语音

1. 同步锁定环境: `uv sync --locked`
2. 在设置中选择"本地模式"
3. 保存设置
4. 首次监听时等待项目内 base 模型准备完成
