# 乱序执行轨迹复核器（OOO Trace Reviewer）

在向星载计算载荷上传微码补丁前，复核乱序执行轨迹，确认**错误预测分支**与
**精确异常**都不会让错误路径的寄存器结果进入已提交体系结构状态。

* 纯 Python 标准库实现（无第三方运行时依赖）
* 维护：ROB 顺序、重命名映射表、空闲物理寄存器表、每个条件分支的检查点
* 自动定位**首个违规事件**，展开事件前后映射表、ROB、检查点与回收寄存器证据
* 网页 + REST API（支持一次性复核与逐步真实 API 查看状态）
* Docker / Compose 一键起页面；`verify` 服务运行构建检查、代码测试与 API/HTTP
  冒烟后以结果码退出

## 微体系结构模型

* 8 个体系结构寄存器 R0–R7，初始固定映射到 P0–P7；其余物理寄存器按配置数量
  （默认 16 个：P8–P15）从空闲表分配。
* **dispatch（按程序序）**：目的寄存器从空闲表取下一个标签，旧标签记入 ROB；
  源寄存器按**当时映射**读取；条件分支建立检查点（分支本身不占用目的标签）。
* **writeback**：标记 ROB 项完成；指令一旦被更老分支回滚或异常回滚清除，
  随后到达的写回一律拒绝（`WRITEBACK_AFTER_SQUASH`）。
* **resolve**：条件分支解析。
  * 预测正确 → 释放该分支检查点；
  * 误预测 → 清除 ROB 中全部更年轻指令、回收其物理标签、映射表/空闲表恢复到
    该分支检查点。
* **commit**：只能提交 ROB 队首，且必须已写回完成；分支队首还必须已解析。
  提交时回收旧物理标签。
* **exception**：故障指令携带异常标记；当其到达 ROB 队首时，**故障指令自身
  目标与全部更年轻结果都不提交**，按 young→old 逆序遍历 old-tag 恢复异常前的
  精确映射并回收全部标签。

### 违规码（首个即终止）

| 代码 | 含义 |
|---|---|
| `DISPATCH_OUT_OF_ORDER` / `DISPATCH_DUPLICATE` / `DISPATCH_UNKNOWN_SEQ` | 分派序错误 |
| `NO_FREE_PHYS_REG` | 空闲物理寄存器耗尽 |
| `WRITEBACK_NOT_DISPATCHED` / `WRITEBACK_DUPLICATE` | 写回时序错误 |
| `WRITEBACK_AFTER_SQUASH` | 已被回滚清除的指令写回到达（错误路径结果） |
| `RESOLVE_NOT_BRANCH` / `RESOLVE_UNFINISHED` / `RESOLVE_DUPLICATE` / `RESOLVE_STALE` | 分支解析错误 |
| `COMMIT_EMPTY_ROB` / `COMMIT_NOT_HEAD` / `COMMIT_UNFINISHED` / `COMMIT_UNRESOLVED_BRANCH` | 提交序/完成度违规 |
| `EXCEPTION_NOT_DISPATCHED` / `EXCEPTION_DUPLICATE` / `EXCEPTION_AFTER_COMMIT` / `EXCEPTION_AFTER_SQUASH` | 异常边界违规 |

## 输入格式

至多 **24 条指令**、至多 **96 个事件**（按发生顺序）。

指令：

```
ADD R1,R0,R0          # ALU: ADD SUB AND OR XOR SLT MUL  Rd,Rs,Rt
BEQ R1,R2 predict=taken        # 条件分支: BEQ BNE Rs,Rt predict=taken|not-taken
I0: ADD R1,R0,R0               # 可选 I序号: 前缀；# 或 // 为注释
```

事件：

```
dispatch I0
writeback I0
resolve I0 taken            # 或 not-taken
commit I0                   # 序号可省略，表示提交当前 ROB 队首
exception I2
```

## 本地运行

```bash
python3 -m app                # 默认 0.0.0.0:8080
PORT=9090 python3 -m app     # 可配置端口
# 打开 http://localhost:8080/
```

## REST API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康响应 `{"status":"ok"}` |
| POST | `/api/validate` | 只解析程序与事件 |
| POST | `/api/simulate` | 一次性复核，返回逐步 before/after 报告与首个违规 |
| POST | `/api/sessions` | 建立逐步复核会话 |
| POST | `/api/sessions/<id>/step` | 推进一个事件（真实逐步状态） |
| POST | `/api/sessions/<id>/reset` | 复位到事件 0 |
| GET | `/api/sessions/<id>` | 查看会话当前状态 |

请求体：`{"program": "...", "events": "...", "num_phys": 16}`。

## Docker / Compose

宿主端口可配置（默认 8080）：

```bash
docker compose up web                 # http://localhost:8080
WEB_PORT=9090 docker compose up web   # http://localhost:9090
```

`verify` 服务对**分支回滚**与**异常边界**运行代码测试、构建检查
（compileall）与 API/HTTP 冒烟，随后退出并透传结果码：

```bash
docker compose build
docker compose run --rm verify        # 自动等待 web 健康；退出码 0 = 全部通过
# 或：构建后等待 web 健康再执行 verify，verify 退出即收敛
docker compose up --build --abort-on-container-exit verify
```

## 不使用容器的复核门禁

```bash
bash scripts/verify.sh                      # 自启临时服务器并冒烟
TARGET_URL=http://host:port bash scripts/verify.sh
python3 -m unittest discover -s tests -v    # 仅单元/API 测试（45 项）
python3 scripts/smoke.py http://127.0.0.1:8080
```

## 目录

```
app/simulator.py        # ROB/重命名/空闲表/检查点/精确异常核心
app/server.py           # 标准库 HTTP + REST API
app/static/             # 复核页面（时间线、前后映射、ROB、回收证据）
tests/                  # 分支回滚与异常边界单元测试 + HTTP/API 测试
scripts/verify.sh       # 构建检查 + 测试 + 冒烟编排（Compose verify 入口）
scripts/smoke.py        # API/HTTP 冒烟
Dockerfile, docker-compose.yml
```
