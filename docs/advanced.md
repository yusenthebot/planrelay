# GPT Connector 技术说明

这里保留原来的详细说明。第一次使用请看 [极简指南](../README.md)。

连接网页 ChatGPT 规划与本机 Codex 开发的 MCP 小插件。

## 推荐：GitHub 私有仓库 workflow

不用部署服务器。网页使用已连接的 GitHub 工具，本机使用 Codex 插件和 `gh`：
选定背景 → 同一个私仓 → 网页规划 → 任务 Issue → 独立任务 branch → Codex 开发 → 结果回传。
首次可以用一句 prompt 配置本机绑定并生成网页引导；GitHub 授权点击不能省略。
没有网页写工具时明确停止，不假装接通，也不需要搜索 GPT Connector 网页应用。

在 Codex 说：

> 用 GPT Connector，把当前项目接到我的私仓 OWNER/gpt-connector-inbox，
> 背景只上传 README.md。我允许写入这些背景及任务结果，请生成网页引导 prompt。

只有三个入口 prompt，见 [复制使用](prompts.md)。每个新任务由 Codex
接收时自动创建 `gpt-connector/task-编号`，同一任务重复接收复用原分支。
分支保存精确背景、任务和结果，不推送业务代码。
完整步骤、协议和命令见 [GitHub workflow](github-workflow.md)。
默认不监听、不自动执行远端 Issue；接收开发用一句本机 prompt 完成。
GitHub 模式使用当前 Codex 任务权限，不继承下方 HTTP worker 的沙箱保证。

## 可选：自托管 MCP bridge（原实验模式）

网页读取背景并规划 → 用户批准后提交 → 本机 worker 自动开发、测试
→ 结果回传 → 网页或 Codex 查询。日常不需要复制答案或文件。

这是实验版自动接力实现，自动执行默认关闭，不是已上线的生产双端服务。
macOS 后台进程清理只能 best-effort，不能保证强制终止所有脱离父进程的后代。
要试用需显式加 --experimental-execution；生产运行应增加内核级进程容器边界。
首次需要自己的 OAuth
账号授权、设备配对和明确的项目绑定；同一个 ChatGPT 登录不等于 MCP 授权。
原手动脚本保留为可选 legacy 工具。

## 能做什么

- 网页 MCP：读背景、提交方案、查结果、取消、配对和撤销设备。
- Worker：用本机 ChatGPT 登录的 Codex CLI，在 detached worktree 开发。
- Codex 插件：读取已配对项目和结果，不重复启动开发。
- 持久化队列、去重、租约、取消和超时；失联任务不自动重跑。
- 明确选择上传文件，校验基线，限制内容及回传大小。
- 本地 GitHub 仓库检查、显式身份绑定、独立同步背景（不启动开发）。

要求干净 Git 项目，执行隔离验证失败就阻断。不自动提交、推送或部署业务
代码。工具联网被禁用，依赖须预先准备。凭证检测并不完备，上传前仍须检查机密。

## 安装与首次连接

需要 Python 3.12+、uv、已使用 ChatGPT 登录的 Codex CLI。

```sh
git clone -b main https://github.com/yusenthebot/planrelay.git gpt-connector
cd gpt-connector
uv sync --locked
uv run gpt-connector doctor
```

可以先只读检查本机已有的 clone，不需要 OAuth、配对或 GitHub token：

```sh
uv run gpt-connector inspect --project /absolute/local-clone
```

输出本地 origin 声明的 `owner/repository`、HEAD 和干净状态，不联网确认
仓库存在或权限，也不上传源码。支持 github.com HTTPS、Git SSH origin。
普通 Git 项目仍可不指定 GitHub 绑定；此 bridge 模式不把 GitHub Issues 当作任务队列。

1. 部署 HTTPS bridge，配置 OAuth issuer、JWKS 和你的 owner subject。
   见 [服务配置](setup.md)。不要公开本地测试 token。
2. 网页 ChatGPT 连接 `https://你的域名/mcp` 并完成 OAuth。
   调用 `pair_device` 获得一次性配对码。
3. 创建独立 artifacts 和私有配置目录，配对授权的 Git 项目：

   ```sh
   uv run gpt-connector pair --bridge https://你的域名 \
     --project /absolute/authorized-project --project-id my-project \
     --github your-owner/your-repository \
     --file README.md --file src/example.py \
     --artifacts /absolute/artifacts --config /absolute/private/worker.json
   ```

   按提示输入配对码，配置不得提交到 Git。确认接受实验版限制后，可在配对
   时额外加 --experimental-execution；否则任务回传 blocked，不调用模型。
4. 保持 worker 运行：

   ```sh
   uv run gpt-connector worker --config /absolute/private/worker.json
   ```

   默认最多完成 1 个任务，配置 max_runs 可设 1–32。不是无限无人值守服务；
   操作者管理进程及重启，不隐式安装后台 daemon。仅接受实际验证的 CLI
   版本；本机的独立测试运行时为 0.154.0，不替换全局旧 CLI。
5. 把仓库加入自己的 Codex marketplace，安装插件并设置 PLANRELAY_CONFIG，
   然后开启新任务。本作者机器的命令为 `codex plugin add gpt-connector@personal`；
   personal 不是公共商店。只复制 skill 不会安装 MCP。

不需要飞书或任何特定企业账号。网页端使用符合 MCP 要求的外部 OAuth
身份服务，GitHub 登录可以作为其可选登录入口；GPT Connector 不内置 GitHub
登录服务，也不把 GitHub token 或本机 gh 凭据作为网页 MCP 的授权凭据。

### 独立同步背景

配对后，在干净 Git 基线上可只同步选定文件，不启动模型或领取任务：

```sh
uv run gpt-connector sync --config /absolute/private/worker.json
```

这里的“同步”指本地 allowlist 背景 → MCP bridge，不是 GitHub push/pull。
`--github` 将身份保存在本机私有配置，每次快照都检查 origin 是否仍匹配；
仓库不匹配、origin 多值或危险配置会在上传前拒绝。绑定身份只在本机
检查，不改变原 MCP payload/revision。不要把它当作远端所有权证明。

`sync` 不改 Git 项目、不覆盖配置、不创建开发 worktree、不 push/pull。
你自行更新并提交本机基线后，再同步新背景；dirty 基线会被拒绝。

## 日常使用

为兼容旧连接，Python 包、`planport` / `planrelay` 命令别名、`PLANRELAY_*`
配置变量、OAuth scope `planrelay` 和现有 GitHub 地址不变；新插件标识为
`gpt-connector`。新版位于 GitHub 的 main 分支。

先在网页说：“用 GPT Connector 读取 my-project 背景，帮我规划这个功能。”
方案确认后说：“按原始需求提交已批准的方案。”网页调用 submit_plan，
本机 worker 自动接手。之后说：“查询刚才的任务结果。”读取 get_run。

不抓聊天、不共享 cookies、不上传 Codex 凭据、不模拟网页点击。
回传是保存结果，不是唤醒原网页聊天或主动发消息。CLI 执行不保证出现在
桌面侧栏。网页和 Codex 仍按各自用量规则计费，不承诺额度优惠。

## 开发与验证

```sh
uv sync --locked
uv run pytest --cov=planrelay --cov=planrelay_bridge --cov-report=term-missing
uv run ruff check .
uv run mypy
uv run bandit -r src skills/gpt-connector/scripts
uv run pip-audit
```

真实本地 HTTP/MCP 配对与同步回读测试（只用临时项目，不调用模型）：

```sh
PLANPORT_REAL_SYNC=1 uv run pytest tests/test_repository.py -q
```

测试包括 SDK MCP 生命周期、owner/audience/scope、设备撤销、并发绑定、
租约、文件校验、受限执行与回传。公网 ChatGPT OAuth 须由部署者单独验收。
旧名称配图不再展示，历史素材保留；[原设计说明](design.md)。

本机已经用独立 CLI 0.154.0 完成两次合成项目真实模型测试，以及重复提交、
取消和实际 stdio 插件代理查询。它们验证本地接力，不等于公网网页 OAuth
已连接，也不证明 macOS 的严格进程容器隔离。

历史验收（此前 PlanPort 版本）：普通测试 159 项通过、2 项 opt-in 跳过，覆盖率
86%；开启真实沙箱的 worker 测试 44 项通过；真实模型 HTTP 接力和已安装
PlanPort stdio 代理的结果回读测试 1 项通过。测试仅使用临时合成项目。

MIT License. Not affiliated with OpenAI.
