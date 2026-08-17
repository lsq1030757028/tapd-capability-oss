# 公网 HTTPS staging 部署合同（本地审查包）

本目录只定义一个候选部署形态，不执行部署：独立公网域名在 Nginx 终止 TLS，
只把精确的 `/mcp` 和 `/healthz` 转发给同机、仅回环可达的容器端口。
DeepTutor 继续使用现有的 per-user secret 注入完整
`Authorization: Bearer <TAPD token>`；本包不新增 token 页面、共享 token、token
环境变量或凭据文件。

## 固定边界

- 来源身份没有默认值。发布系统必须在候选 commit 固定后，把该 commit 的完整
  40 位 SHA 显式注入 `SOURCE_REVISION`；缺失、缩写或非小写十六进制都会停止构建。
  同一值写入 OCI `org.opencontainers.image.revision` label 和容器运行元数据
  `TAPD_CAPABILITY_SOURCE_REVISION`。
- 容器内 MCP 显式监听 `0.0.0.0:3796`；宿主只发布
  `127.0.0.1:3796:3796`，公网入口只能是 TLS 反向代理。
- 不使用 8080、8081 或任何现有业务域名；模板也不包含真实域名、证书路径、
  主机地址或凭据。
- `/healthz` 无需认证，只返回固定健康状态；不得解析请求头、读取配置、访问
  `/data` 或调用 TAPD。
- `/mcp` 的每一个 HTTP 请求都必须携带 `Authorization: Bearer ...`。
  缺失或格式错误直接返回 401；即使进程环境里误放了 `TAPD_ACCESS_TOKEN` 也不得
  回退使用。反向代理必须原样转发 Authorization，但访问日志不得记录它。
- `TAPD_CAPABILITY_HOME=/data` 是唯一持久数据根；`/data` 使用独立持久卷并按
  token 指纹隔离。容器根文件系统只读，`/tmp` 使用 tmpfs。
- 运行用户固定为非 root，删除全部 Linux capabilities，并启用
  `no-new-privileges`。CPU、内存、进程数和日志轮转均设上限。
- 容器不持有 TAPD 凭据。Compose 文件不得声明 `TAPD_ACCESS_TOKEN`、
  `env_file`、secret 值或 credential volume。

## 包内文件

| 文件 | 用途 |
| --- | --- |
| `../Dockerfile` | 固定 Python 运行时、离线测试 stage、最小非 root runtime |
| `../.dockerignore` | 排除 Git、缓存、测试产物和本地凭据 |
| `compose.local.yml` | 只绑定宿主回环地址的本地候选运行合同 |
| `nginx.conf.template` | TLS upstream 审查模板；占位符必须由目标环境配置替换 |
| `deploy_local.py` | 可 dry-run、可注入 runner 验证的部署与自动回滚状态机 |
| `rollback.local.ps1` | 校验上一版 digest + revision label 的纯回滚入口 |
| `../tests/test_deployment_package.py` | 对上述安全边界做静态与适配器级回归 |
| `../tests/test_deployment_orchestrator.py` | 状态机成功、健康失败、回滚失败的 fixture 故障注入 |

## 本地审查与验证（不接触真实 TAPD）

```powershell
$env:TAPD_CAPABILITY_SOURCE_REVISION = '<待构建 commit 的完整 40 位 SHA>'
docker build --build-arg SOURCE_REVISION=$env:TAPD_CAPABILITY_SOURCE_REVISION --target test -t tapd-capability:staging-test .
docker build --build-arg SOURCE_REVISION=$env:TAPD_CAPABILITY_SOURCE_REVISION --target runtime -t tapd-capability:staging-local .
docker compose -f deploy/compose.local.yml config
docker compose -f deploy/compose.local.yml up -d --build
```

这里不能把“修改 Dockerfile 前的某个 SHA”写成默认值，也不能先把“这次修改后将产生的
SHA”硬编码进同一个 commit。正确顺序是：代码提交固定 → 发布系统读取该完整 SHA →
把 SHA 作为 build arg 注入 → 回读 image label。Compose 的
`TAPD_CAPABILITY_SOURCE_REVISION` 同样没有默认值，缺失时配置解析直接失败。

启动后只允许从本机检查：

```powershell
Invoke-RestMethod http://127.0.0.1:3796/healthz
```

预期固定为 `{"status":"ok"}`。向 `/mcp` 发无 Authorization 的请求必须得到
401；测试中可注入明确标注为 fixture 的假 token，证明无头请求不会回退到环境变量。
两枚不同 fixture token 的持久命名空间隔离由离线单元测试验证，测试 transport
不会发出 TAPD 网络请求。

本地审查完成后应执行 `docker compose ... down`。默认不删除命名卷；只有确认数据
可丢弃时才由人显式删除。

## 可审查部署与自动回滚

候选和上一版都必须是不可变 `repository@sha256:<64 hex>`，且都提供对应完整 source
revision。先运行 dry-run；它只写一份不含凭据的规范化状态快照并打印计划，不执行 Docker
或 HTTP：

```powershell
python -X utf8 deploy/deploy_local.py `
  --candidate-image '<candidate@sha256:...>' `
  --candidate-revision '<candidate 40-char SHA>' `
  --previous-image '<previous@sha256:...>' `
  --previous-revision '<previous 40-char SHA>' `
  --state-dir '<包外审计目录>' `
  --dry-run
```

移除 `--dry-run` 才会实际执行。`--compose-file` 只接受包内
`deploy/compose.local.yml` 的 canonical 路径，并要求内容与代码中经审查的 SHA-256 完全
一致；任意副本、修改或替代文件都会在创建状态目录和调用 runner 前失败。状态机在任何
Compose `up` 之前只保存固定 schema 的非敏感元数据：candidate/previous 不可变 digest、
二者 source revision、canonical Compose 身份与哈希、固定 service、loopback 端口、持久卷
名称/目标、只读 rootfs 标志和 health URL。

状态目录不会保存 Compose 原文，也不会复制任意 `environment`、`env_file`、`secrets`、
`configs`、labels、build args 或 volume 来源。canonical 校验失败时不创建状态目录、不留下
临时/半成品文件，也不调用 Docker/HTTP runner。运行 Compose 时仍从子进程环境移除
`TAPD_ACCESS_TOKEN`；快照合同不依赖凭据名称 denylist。

正向 deploy 和 `rollback.local.ps1` 在任何 Docker、health 或状态写入之前取得同一个
包外、host-scoped 非阻塞锁：Linux 为 `/var/lock/tapd-capability/staging.lock`，Windows 为
OS Known Folder API 返回的 Common Application Data 下的 `tapd-capability/locks/staging.lock`；
Python 与 PowerShell 均不信任进程的 `PROGRAMDATA`。路径没有命令行或环境变量覆盖口，避免
两个执行者选不同锁而绕过互斥。第二执行者立即得到固定 `deployment busy`，
Python 入口退出 75；错误不回显锁路径、锁内容、镜像参数或凭据。所有普通成功和异常路径都在
`finally` 释放自己持有的 marker，并以随机 ownership token 防止误删别人的锁；不等待、不重试，
因此不会形成锁顺序死锁。若主机断电或进程被强杀留下 stale marker，禁止自动抢锁；运维先确认
没有 deploy/rollback 进程和 Docker 操作，再按事故恢复流程删除 marker 并留档。

执行顺序固定：

1. 校验两个本地镜像的 OCI revision label；不匹配时停止，不启动容器。
2. `docker compose up -d --no-build` 启动候选，然后检查固定 loopback `/healthz`。
3. 候选启动或 health 失败时，自动用 previous digest + previous revision 恢复，再次检查
   `/healthz`。
4. 回滚启动失败退出 3，回滚后 health 失败退出 4；候选失败但成功恢复也退出 2，避免
   把“已恢复旧版”误报为部署成功。只有候选健康才退出 0。

所有路径都只执行 Compose `up` 和健康检查，从不执行 `down -v`、删除 volume 或改写
`/data`。`rollback.local.ps1` 是独立的人工纯回滚入口，同样要求 previous digest 与
previous source revision，并在启动前核对 image label。

## 上线前强制项（本包不代办）

以下任一项未完成都不得部署：

1. 在 Project Atlas 新增并核实该 staging 的独立 `Resource`，`belongsTo` 必须是
   `component:tapd-capability`；目标主机、服务名、镜像 digest 和固定 commit 要可回读。
2. 申请全新的专用域名和有效证书；不得复用现有业务域名，不得开放 8080/8081。
3. 入站 ACL 只允许 443；应用端口只监听回环。出站 ACL 只允许 TAPD 必需的 HTTPS
   目的地，拒绝其它公网出口，并用阻断测试确认。
4. 将最终运行镜像改为按 digest 引用，保存 SBOM/扫描结果，并准备上一版不可变
   镜像 digest。基础镜像与应用镜像都不得只用浮动 tag。
5. 为 `/data` 建立独立持久卷、权限、加密、定期备份与恢复演练；备份不得包含
   TAPD token（正常设计也不会写入）。
6. 先准备并演练一条命令回滚；回滚只切回上一镜像，不删除或回退 `/data`。
7. Nginx 模板中的域名/证书占位符由目标环境配置系统替换；证书和私钥不进入仓库。
   校验精确路由、无 30x、TLS、安全头、限流、超时、body 上限及日志格式。
8. DeepTutor 仍使用现有 MCP Services per-user secret。用两个 DeepTutor 账号、两枚
   不同 TAPD token 完成隔离 UAT：项目范围、profile、baseline、pending review 与
   session context 均不得串租户。
9. 分别验证无 token、错误 Bearer、过期/轮换 token、A token 携带 B session context、
   TAPD 撤权和出网拒绝；所有失败都必须 fail-closed，且不回退共享凭据。
10. 扫描 Nginx、容器和 DeepTutor 日志：不得出现 Authorization、token、token 指纹、
    session context、请求 body、配置文件内容或凭据路径；完成 token 轮换后复扫。
11. 按 `DEPLOY_SPEC` 留部署事件：执行者、commit、镜像 digest、Atlas Resource、配置
    差异、冒烟、UAT、回滚镜像及结果。该记录不等于发布或用户验收。

## 回滚合同

部署前记录 `PREVIOUS_IMAGE`（必须是 `repository@sha256:<64 hex>`）和它的完整
`PREVIOUS_SOURCE_REVISION`。若部署后冒烟、
隔离 UAT 或日志扫描任一失败，立即运行本目录的回滚脚本切回该镜像，并复验旧版本
`/healthz`。回滚保留 `/data`，不执行数据删除。脚本只覆盖本地 Compose 入口；共享
staging 的主机编排、审批和留档仍须在已登记 Resource 上另行完成。
