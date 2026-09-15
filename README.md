![PlanRelay — Think on the web. Build in Codex.](assets/readme-hero.png)

# PlanRelay

连接网页 ChatGPT 规划与本机 Codex 开发的 MCP 小插件。

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

要求干净 Git 项目，执行隔离验证失败就阻断。不自动提交、推送或部署业务
代码。工具联网被禁用，依赖须预先准备。凭证检测并不完备，上传前仍须检查机密。

## 安装与首次连接

需要 Python 3.12+、uv、已使用 ChatGPT 登录的 Codex CLI。

```sh
git clone -b feat/automatic-mcp-relay https://github.com/yusenthebot/planrelay.git
cd planrelay
uv sync --locked
uv run planrelay doctor
```

1. 部署 HTTPS bridge，配置 OAuth issuer、JWKS 和你的 owner subject。
   见 [服务配置](docs/setup.md)。不要公开本地测试 token。
2. 网页 ChatGPT 连接 `https://你的域名/mcp` 并完成 OAuth。
   调用 `pair_device` 获得一次性配对码。
3. 创建独立 artifacts 和私有配置目录，配对授权的 Git 项目：

   ```sh
   uv run planrelay pair --bridge https://你的域名 \
     --project /absolute/authorized-project --project-id my-project \
     --file README.md --file src/example.py \
     --artifacts /absolute/artifacts --config /absolute/private/worker.json
   ```

   按提示输入配对码，配置不得提交到 Git。确认接受实验版限制后，可在配对
   时额外加 --experimental-execution；否则任务回传 blocked，不调用模型。
4. 保持 worker 运行：

   ```sh
   uv run planrelay worker --config /absolute/private/worker.json
   ```

   默认最多完成 1 个任务，配置 max_runs 可设 1–32。不是无限无人值守服务；
   操作者管理进程及重启，不隐式安装后台 daemon。仅接受实际验证的 CLI
   版本；本机的独立测试运行时为 0.154.0，不替换全局旧 CLI。
5. 把仓库加入自己的 Codex marketplace，安装插件并设置 PLANRELAY_CONFIG，
   然后开启新任务。本作者机器的命令为 `codex plugin add planrelay@personal`；
   personal 不是公共商店。只复制 skill 不会安装 MCP。

## 日常使用

先在网页说：“用 PlanRelay 读取 my-project 背景，帮我规划这个功能。”
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
uv run bandit -r src skills/planrelay/scripts
uv run pip-audit
```

测试包括 SDK MCP 生命周期、owner/audience/scope、设备撤销、并发绑定、
租约、文件校验、受限执行与回传。公网 ChatGPT OAuth 须由部署者单独验收。
配图通过 ImageGen 生成；[原设计说明](docs/design.md)。

本机已经用独立 CLI 0.154.0 完成两次合成项目真实模型测试，以及重复提交、
取消和实际 stdio 插件代理查询。它们验证本地接力，不等于公网网页 OAuth
已连接，也不证明 macOS 的严格进程容器隔离。

MIT License. Not affiliated with OpenAI.
