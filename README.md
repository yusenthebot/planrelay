# GPT Connector

网页 GPT 想方案，Codex 写代码。用你自己的 GitHub 私有仓库交接，不用复制整段方案。

先在 Codex 安装 GPT Connector，网页端启用 GitHub。然后按下面三步使用。

## 1. 第一次：发给 Codex

在你要开发的项目里，新开一个 Codex 任务，发送：

> 用 GPT Connector 连接这个项目。先让我确认私有仓库和要上传的背景文件，配置好后给我网页端的连接 prompt。

## 2. 想方案：发给网页 GPT

在网页 ChatGPT 选择 GitHub 插件，粘贴 Codex 给你的连接 prompt。首次可能需要点击授权。

然后直接说你的需求，例如：

> 新任务：我想增加一个登录功能，先帮我规划。

方案满意后说：

> 批准，交给 Codex。

网页会给你一个任务编号。

## 3. 写代码：发给 Codex

把下面的编号换成网页给你的编号：

> 用 GPT Connector 执行任务 #编号。我允许开发、测试和回传结果，但不要提交、推送或部署业务代码。

Codex 接收时会自动开一个独立 branch；新任务新 branch，同一个任务不重复开。

完成后回到网页说：

> 读取任务 #编号的结果。

以后只重复第 2、3 步。不用重新连接，但仍需要你发消息让 Codex 接手。

[详细说明](docs/advanced.md) · [完整 prompts](docs/prompts.md)
