# 三个 prompt

安装插件后，选一个自己账号下的 private repo，长期用作通讯邮箱。
不需要飞书、不需要自己的中转服务器，也不要分享 token 或 cookies。
本地与网页的 GitHub 账号分别授权；prompt 不能代替首次授权点击。

## 1. Codex 首次连接

在要接入的项目任务里发送，替换 OWNER 和允许上传的文件列表：

> 使用 GPT Connector，把当前项目接到我的私仓 OWNER/gpt-connector-inbox。
> 背景只上传 README.md；我允许把这些选定背景、批准的任务和验证结果写入
> 此仓库。检查真实 GitHub 登录，保存绑定并生成另外两个 prompt。
> 每个接收的新任务自动开一个独立中转 branch；不要启动后台执行，也不要
> commit、push 业务代码或部署。仓库不存在时先询问是否创建。

插件保存长期仓库与项目绑定，不需要每次重新设置。已有不同绑定不覆盖。
业务文件不会因为“连接”而全部上传，Git 基线有未提交变化时也不会代你提交。

## 2. 网页 GPT Chat 连接与规划

选择真正可用的 GitHub 工具，粘贴 Codex 返回的 `web_connect`。
其完整生成版会携带实际仓库、项目 ID、协议及校验要求。短入口如下：

> 通过我的私有仓库 OWNER/gpt-connector-inbox 使用 GPT Connector workflow。
> 先检查真正可用的 GitHub 读取、创建 Issue、读取评论工具，再读取默认分支
> 的 README.md 和 gpt-connector/context.json，确认协议及项目。之后帮我规划新需求，等我明确
> 批准交给 Codex 再创建标准任务 Issue；返回真实编号。同一任务不要重复创建，
> 新任务开新 Issue。Codex 接收后自动创建对应的 gpt-connector/task-编号 分支。
> 缺少工具时停止，不假装已连接，不要求我粘贴 token。

日常直接在该聊天说“新任务：……”；方案满意后说“批准，交给 Codex”。
标准任务正文是 `gpt-connector/v1` JSON，具体结构见 [协议](github-workflow.md)。
生成的完整 prompt 已包含结构，不需要用户手工拼 JSON。

## 3. Codex 接收并执行

粘贴生成的 `codex_receive`，将编号替换为网页返回的真实 Issue 编号：

> 使用 GPT Connector 接收已绑定私仓的 Issue #编号。自动创建或复用此任务
> 的独立中转分支，验证背景和任务基线。我授权执行此任务批准的范围，实际
> 测试，并把结果回写到同一分支的 gpt-connector/result.json 和 Issue 评论。
> 不自动 commit、push 业务代码或部署。基线不匹配或写入结果不确定时停止报告。

这段 prompt 由 skill 映射为 `github start → 开发与验证 → github result`。
用户无需自己执行 CLI、创建 branch 或搬运方案。只传真实任务编号，避免收到
多个待执行任务时猜测用户选择；普通网页聊天不能自动启动本机 Codex。

## 每个任务的分支

```text
同一个 private repo
├── 默认分支：共享的选定背景与网页引导
├── gpt-connector/task-12：context.json + task.json + result.json
└── gpt-connector/task-13：context.json + task.json + result.json
```

三个文件都在任务分支的 `gpt-connector/` 下。新分支在 Codex 收到任务时创建，
不是网页创建 Issue 的瞬间。重复接收同一 Issue 不另开分支；新需求用新 Issue。
这是中转分支，不是业务源码分支；业务代码分支/worktree 由开发步骤另行管理。
邮箱写入会产生 GitHub API 提交，但不会因此提交或推送业务仓库。

验证网页结果时，只需说“读取任务 #编号的实际回传结果，不要写入”。
没有结果就报告待执行或待回传，不能推测成功。
