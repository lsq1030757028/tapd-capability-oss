# tapd-capability

给 TAPD 用的字段基线与业务语义能力。**它解决一个具体的病**：项目里的字段编号（第 8 栏、第 10 栏……）
含义会被人改，而手写的映射表不会跟着变，也没有任何东西会报警——于是查询继续跑，
只是安静地给出错答案。实测中这种错配置带病运行了 98 天。

做法是：拉一条真实条目 → 你对着 TAPD 看一眼 → 存基线 → 之后机械查漂。

---

## 拿到这个目录后怎么开始

### 1. 准备令牌

去 TAPD 拿一个 access token，二选一放好：

- 环境变量 `TAPD_ACCESS_TOKEN`，或
- 写进 `<数据目录>/credentials/tapd_access_token`（纯文本一行）

**不要写在命令行参数里**——进程列表对其他程序可见。

数据目录默认在 `%LOCALAPPDATA%\tapd-capability`（Windows）或 `~/.tapd-capability`，
可用环境变量 `TAPD_CAPABILITY_HOME` 改。

### 2. 装依赖（只装一次，版本钉死）

```
uv venv <某处>/.venv --python 3.12
uv pip install --python <某处>/.venv/Scripts/python.exe -r adapters/requirements.txt
```

版本钉死是有意的：不在每次启动时去解析包源，那正是"有时能用有时不能"的根因。

### 3. 挂进你的 AI 客户端

在 MCP 配置里加一条，指向上面那个 venv 的 python 和 `adapters/mcp_server.py`。
这是 stdio 模式，一人一进程；多人共用一个服务见下面「两种传输模式」。

### 4. 第一次对话

直接问它你的 TAPD。它会发现本机还没有基线，列出你能访问的项目让你挑一个，
然后给你一条**真实条目 + 链接**，你打开对一眼那几栏对不对，确认后基线就建好了。

全程你不需要知道"字段编号"这回事。

---

## 这个目录里没有别人的数据

基线和凭据都存在**目录之外**，所以复制或打包这个目录不会把任何人的项目配置带走。
基线文件另外记了令牌指纹，换一个令牌就自动失效——即使文件真被带过去了也用不了。

这是有意的：同一个编号在不同项目、不同工作项类型里含义都不同，
把别人的配置搬过来只会把你带沟里。

---

## 工具面

| 工具 | 干什么 |
|---|---|
| `tapd_projects` | 列出这个令牌能访问的项目 |
| `tapd_context_status` | 读取当前令牌对应的非敏感用户上下文，检查默认项目是否仍可访问 |
| `tapd_context_save` | 保存默认项目、TAPD 身份和业务角色；**只写本地，不写 TAPD** |
| `tapd_context_resolve` | 按“本次明确项目 > 保存默认 > 唯一项目”确定本次项目；可用刚确认的身份/角色签发不落盘的短时 `session_context` |
| `tapd_baseline_status` | 基线在不在、还作不作数（**取数前先问它**） |
| `tapd_baseline_review` | 算出哪几类工作项要核，给真实条目让你对照 |
| `tapd_baseline_confirm` | 你对完了，存基线 |
| `tapd_baseline_drift` | 漂移明细：当时记的 vs 现在实际 |
| `tapd_field_evidence` | 你觉得某栏读错了 → 拉数据交你自己核，**不改基线** |
| `tapd_watchlist_recheck` | 以前没人填的栏位现在有值了，提示收录 |
| `tapd_semantic_status` | 语义是否已确认、是否失效，以及此刻能答/不能答的问题 |
| `tapd_semantic_review` | 用工作流业务状态 + 真实条目生成核对卡；不猜、不落库 |
| `tapd_semantic_confirm` | 确认刚看过的语义核对卡；只写本地基线文件，不写 TAPD |
| `tapd_business_query` | 用已确认语义与已确认身份回答业务问题；任何前提不满足就拒答 |
| `tapd_probe` | 连通性自检 |
| `tapd_workspace` / `tapd_field_config` | 底层诊断；输出含内部编号，禁止直接作为用户答复 |
| `tapd_list` | **仅保留为包内诊断函数，不注册成公共 MCP 工具**；业务取数只能走 `tapd_business_query` |

---

## 几条不会退让的规则

- **字段编号不出现在给人看的东西里**——一律用业务名。
- **人说"这栏不对"不改值**，只重新拉数据交人核。人也会记错，唯一权威是 TAPD 实测。
- **没核过的栏位不拿来筛**。基线缺失或漂移时拒绝出数，而不是给一个看着像对的答案。
- **没核过的状态不拿来筛**。`tapd_semantic_review` 第一次只列工作流里的精确业务状态；
  用户明确选择后才生成真实条目核对卡，confirm 后才允许业务查询，不做相似词匹配。
- **“我”只来自已确认上下文**。可使用 `tapd_context_save` 保存的 profile，或使用
  `tapd_context_resolve` 根据用户刚确认的身份/角色签发的短时 `session_context`；不从令牌、
  项目成员或返回数据里臆测当前用户，`tapd_business_query` 也不接收裸身份字符串。
- **原始列表不面向人**。`tapd_business_query` 不满足基线、身份、语义或漂移检查时返回
  `needs_review` / `blocked`，绝不退化为 `tapd_list` 转储。
- **TAPD 只读**。所有 TAPD 写操作在打到网络之前就被拒绝。本地写工具
  `tapd_context_save` 与 `tapd_semantic_confirm` 只把非敏感用户上下文/确认语义写到包外
  数据目录，返回值明确标记 `effect=workspace-write`、`tapd_write=false`。

### 当前业务查询候选

首个纵向问题是「最近分配给我且未开始测试的需求」。这里的“最近”明确回显为
按需求最近更新时间倒序，**不冒充“最近发生的指派动作”**；“分配给我”只匹配已确认
profile 或有效 `session_context` 中的 TAPD 身份；“未开始测试”只使用该项目经
review→confirm 固定的精确状态。

第一次运行 `tapd_semantic_review(workspace_id, "未开始测试")` 不会自动挑状态，只返回
TAPD 工作流里的业务状态名。再次传 `values=[...]` 后才会给出选中/未选中的真实需求核对卡。
状态图内容哈希变化、选中状态消失或返回结构未核实时，相关问题全部拒答。

查询只接受明确且前后一致的分页元数据；缺失、未知、游标不连续或跨页重复时返回
`PAGINATION_UNVERIFIED`，不会把一页误报成全量。完整抓取后才在本地解析 `modified` 并稳定倒序；
命中条目的时间不可解析时返回 `MODIFIED_TIME_UNVERIFIED`，不会宣称未经验证的“最近”。
若命中数大于 `limit`，会同时给出真实符合数、返回数和“已截断”；超过安全分页上限时
同样拒绝返回部分答案。

---

## 用户上下文：只在真缺信息时问

令牌仍由 MCP Services 配置，本工具不新增凭据入口。每次需要 TAPD 项目时，先复用
`user_participant_projects` 发现该令牌当前能访问的项目，再按固定顺序解析：

1. 用户本次明确说出的项目；
2. 当前令牌保存的默认项目；
3. 只有一个可访问项目时自动采用；
4. 仍有多个候选时，只返回业务名称让用户确认，不索要项目 ID。

用户档案只含默认项目、TAPD 身份、业务角色和验证时间，保存到
`<数据目录>/profiles/<令牌指纹>/profile.json`。stdio 和 HTTP **都**按令牌指纹隔离；
换令牌不会读取旧档案。令牌本身、令牌指纹和文件路径都不会出现在工具返回值中。

若用户选择“仅本次”或临时切换项目，调用
`tapd_context_resolve(project_hint, tapd_identity, business_role)`：它在重新核实当前令牌可访问
项目后签发 15 分钟的 opaque `session_context`，不会创建或修改 profile。把该值原样传给
`tapd_semantic_status(..., session_context=...)` 或
`tapd_business_query(..., session_context=...)`；两者会优先使用它，无该值时才回退已保存
profile。声明与当前令牌和项目绑定并带完整性签名；过期、篡改、换令牌、项目不符或撤权均
fail-closed。撤权只重查项目范围，不会继续读取工作流或业务条目。保存默认仍只能显式调用
`tapd_context_save`。

如果默认项目后来被撤权，解析会返回 `SAVED_DEFAULT_OUT_OF_SCOPE` 并停止，绝不静默
切到剩下的唯一项目。用户用业务名称重新确认后，才会保存新的默认项目。

如果本地档案创建、写入或原子替换失败，工具只返回稳定错误码
`PROFILE_WRITE_FAILED` 和安全提示；操作系统异常里的完整数据目录、令牌指纹和临时文件名
不会进入 MCP 返回或 Agent 对话。

---

## 两种传输模式

同一套工具，两种部署形态。区别只有一处，但那一处是承重的：**凭证从哪来**。

| | stdio（默认） | streamable-http |
|---|---|---|
| 起法 | `python adapters/mcp_server.py` | `python adapters/mcp_server.py --transport http` |
| 谁在用 | 一人一进程，你的 AI 客户端拉起 | 一个服务，很多人 |
| 凭证来源 | 进程自己的 `TAPD_ACCESS_TOKEN`，或包外凭据文件 | **每个 `/mcp` HTTP 请求**都从该请求的 Bearer 头取 |
| 服务持有凭证吗 | 进程即租户，等于持有 | **不持有**：不缓存、不落盘、不进日志 |
| 没凭证时 | 报错说清楚该往哪放 | **拒绝**该次调用（不回退到环境变量） |
| 基线存放 | `<数据目录>/baselines/<项目>.json`（一直如此） | `<数据目录>/baselines/<令牌指纹>/<项目>.json` |

默认永远是 stdio：不传参、不设环境变量，行为和改造前逐字一致。

### HTTP 模式怎么起

```
python adapters/mcp_server.py --transport http --port 3796
```

等价环境变量：`TAPD_MCP_TRANSPORT=http`、`TAPD_MCP_HOST`、`TAPD_MCP_PORT`。
命令行优先于环境变量。端点是 `http://<host>:<port>/mcp`。默认 `127.0.0.1:3796`；
只有在容器和反向代理已经限制来源时，才显式加 `--host 0.0.0.0`。

调用方每次 `/mcp` 请求都带自己的 Bearer 令牌：

```
Authorization: Bearer <你的 TAPD 令牌>
```

缺失、空值或其它认证 scheme 都在进入 MCP 协议处理前返回 401。即使进程环境里
误设了 `TAPD_ACCESS_TOKEN` 也不会回退使用。底层凭证解析器仍保留
`x-tapd-access-token` 的兼容分支供受控适配器调用，但原生 HTTP 服务入口只接受
标准 Bearer；公网 staging 不能依赖兼容头。

`GET /healthz` 是唯一无需认证的运行态端点，只返回固定的 `{"status":"ok"}`，
不读取请求头、配置、数据目录或 TAPD。公网 HTTPS staging 的容器、只读根文件系统、
回环发布、Nginx TLS 模板、上线前置和回滚合同见 [`deploy/README.md`](deploy/README.md)。

### DeepTutor per-user MCP 配置

写进该用户自己的 `data/system/user-mcp/<owner>.json`（或在 MCP Services 页里填），
令牌只写引用，值由 DeepTutor 在连接时解析注入，配置文件里永远不出现明文：

```json
{
  "servers": {
    "tapd-capability": {
      "type": "streamableHttp",
      "url": "https://tapd-mcp.example.com/mcp",
      "headers": {
        "Authorization": "${secret:tapd-capability/header.Authorization}"
      },
      "tool_timeout": 60,
      "enabled_tools": ["*"],
      "enabled": true
    }
  }
}
```

`${secret:<服务名>/<字段名>}` 必须占满整个 header value；DeepTutor resolver 不会替换
`Bearer ${secret:...}` 这种字符串内嵌引用。服务名要和服务器条目的 key 一致。

在通用 MCP Services 表单里，header key 填 `Authorization`，value 一次性输入完整的
`Bearer <你的 TAPD 令牌>`；后端会把完整值抽到该 owner 的 secrets 目录，配置文件只留
`${secret:tapd-capability/header.Authorization}`。如果后续把 TAPD 做成 curated catalog，
则凭据输入只收裸令牌，catalog 可使用字段名 `token` 与
`value_template: "Bearer {value}"`，此时配置会生成
`${secret:tapd-capability/token}` 这一整值引用。两种方式都不会把令牌写进 JSON，二者的
字段名不能混用。

### 为什么不再需要网关代管

本机开发期曾用一个常驻网关把这个 stdio 服务包成 HTTP，好让 DeepTutor 连上——
因为 DeepTutor 的 per-user MCP 明确拒收 stdio（共享部署上，stdio 服务是以应用用户身份
在宿主上执行命令，没有任何权限标志能让把它交给学生变得安全）。

那个网关不提供能力，只在绕限制，代价是绑死单机、绑死一个网关进程、
注册在部署级而不是用户级——上云即失效。本服务原生说 HTTP 之后，这一层整个消失：
每个用户在自己的 MCP Services 页里加一条，填自己的令牌，就完了。

### 安全姿态（逐条）

- **令牌不出现在返回值、日志、异常信息里。** 返回值只是 Tool 信封；uvicorn 访问日志
  只记方法/路径/状态码，不记请求头；异常消息一律不回显头内容——最常见的错法是
  「忘了写 Bearer，把裸令牌塞进 Authorization」，回显它等于把一个活令牌抄进日志。
  有用例钉这条（`tests/test_transport_modes.py`）。
- **宽绑 0.0.0.0 的暴露面。** 默认只绑 `127.0.0.1`，SDK 同时开启 DNS rebinding
  防护。只有容器位于反向代理和网络策略之后时才显式宽绑；否则本机端口会向局域网开放。
  服务虽不持有公共凭证，但未受控的监听面仍不应被当成安全边界。
- **没凭证是拒绝，不是降级。** `/mcp` 的每个 HTTP 请求先过 Bearer 形状闸；
  HTTP 模式下不存在「回退到环境变量」这条路径。
  回退意味着：谁都能发一个不带头的请求，然后用运维那份令牌读 TAPD——横向越权。
  用例里专门起了一个环境变量已设好的服务，再发无头请求，验证它照样被拒。
- **租户之间不共享状态。** 待确认的核对卡和基线文件都按令牌指纹（SHA-256 前 12 位，
  不可逆、不存令牌本身）分隔：A 走完 review，B confirm 不到 A 的卡；
  A、B 都在同一个项目上建基线，也不会互相覆盖。

## 跑测试

```
python -m unittest discover -s tests
```

不需要网络、不需要令牌——测试全部走 fixture 适配器。

---

## CI 与门禁现状

本仓的必过质量证据是 GitHub Actions（[`ci.yml`](.github/workflows/ci.yml)）：在
GitHub-hosted `ubuntu-latest` 标准 Runner 上按事件绑定的固定 40 位 SHA 检出，依次执行
凭据泄漏扫描、固定版本 MCP 全套离线测试、Docker `test`/`runtime` 两个 target 构建、
runtime revision label 核验与镜像身份元数据归档。workflow 只有 `contents: read` 权限，
不使用 `pull_request_target`，不引用任何 secrets，没有 registry push、部署或发布副作用。

仓内 [`Jenkinsfile`](Jenkinsfile) 是历史 CODING（Jenkins）门禁的存档证据，现已不再作为
必过 CI 使用；保留它只为门禁演进可追溯，其契约仍被静态测试覆盖。

成功构建的 `ci-artifacts/image-metadata.json` 只记录本机 Docker image ID、来源 ref/SHA 和
runtime revision label，并明确 `registry_digest=null`、`registry_push=false`、
`deployment=false`。它不会把未发生的 registry push 冒充为可部署制品。后续若另获发布批准，
必须先把 runtime image 推入获批 registry，回读 `repository@sha256:...`，再把该不可变 digest
绑定到部署回执；本流水线本身不会做这些动作。

本地 `.githooks/pre-push` 仍是可绕过的补偿闸，不等价于 Actions 构建或平台强制；非作者审查、
Actions 绿灯、部署、运行态验证和用户 UAT 继续是相互独立的证据层。

提交非作者审查前，候选回执必须按 `origin/main...<候选 SHA>` 说明整包意图，并附完整的
`git log origin/main..<候选 SHA>` 提交清单与 `git diff --name-status origin/main...<候选 SHA>`
文件清单，不能只描述最后一个增量提交。所有该范围内新增、复制、修改或重命名的 Python
文件统一走 Ruff 两闸：

```
python ci/check_changed_python.py --base origin/main --ruff <固定版本 Ruff 可执行文件>
```

脚本会先跑 `ruff check`，再跑 `ruff format --check`；任一非零即失败，避免“只检查最后一批
手改文件”形成旧代码假绿。

克隆后启用本地闸：

```
git config core.hooksPath .githooks
```
