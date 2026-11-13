# SP3-1 验收回执（裁决协议实施）

> 依 GPT 任务书「正式授权：开始 SP3-1。完成后暂停，提交完整验收回执，等待下一轮终审。」
> 本文按 E 段 8 项门槛逐项交付；§9 列出**需终审裁定的 3 项**（我不自行裁定）。

---

## §0 结论摘要

| 项 | 结果 |
|---|---|
| A 段 DDL 修订 | 完成（17 条：2 表 + 7 索引 + 8 触发器） |
| B 段 `decide()` 实现 | 完成（单事务，失败全回滚） |
| C 段强制测试矩阵 | 完成（新增 50 用例，全通过） |
| D 段全量回归 | **1135/1135 OK**（原 1083 + 新增 50 + 守卫净增 2） |
| 发现并修复的实现缺陷 | 2 项（见 §7） |
| 需终审裁定 | 3 项（见 §9） |

**未触碰**：G6-HOTFIX 逻辑、takeover 生产调用点、自动重新 dispatch、`CONFIRMED_UNKNOWN` 入槽位、生产库迁移、git commit/push/tag。

---

## §1 实际修改文件清单

| 文件 | 性质 | 变更 |
|---|---|---|
| `scripts/sp_errors.py` | 修改 | +3 错误码：`SP_ACT_014` ADJUDICATION_CONSUMED / `SP_ACT_015` ADJUDICATION_DECISION_INVALID / `SP_ACT_016` ADJUDICATION_STATE_INVALID |
| `scripts/actionledger.py` | 修改 | ① `_apply_pragmas()` 新增，三处连接点（`__init__` / `_reconnect` / `_write_delegated_once`）统一调用并读回断言；② `_ADJUDICATION_DDL` 17 条；③ `_adj_hash()` + 4 个常量元组；④ `_init_schema()` 末尾追加 DDL 重放；⑤ 新增 `decide_adjudication` / `consume_adjudication` / `revoke_adjudication` |
| `tests/test_p2_sp3_adjudication.py` | 新增 | 50 用例（A–J 十组） |
| `tests/test_p2_sp1a.py` | 修改 | 守卫语义更新：1 条删除、3 条新增（见 §9.3） |
| `tests/test_p2_g6_hotfix.py` | 修改 | H3 用例：**前提变更、结论不降**（见 §9.3） |
| `project-04-p2-3-work/sp31_replace_probe.py` | 新增 | REPLACE 实验脚本 |
| `project-04-p2-3-work/sp31_supersede_probe.py` | 新增 | supersedes 边界实验脚本 |

**说明**：`actionledger.py` 及 P2 系列文件在仓库中处于**未跟踪**状态（`??`），因此无法提供 git diff。改动以上表 + §2/§3 全文为准。这是既有状态，非本轮造成，也未擅自提交。

---

## §2 最终 DDL（17 条，结构命名未变）

| # | 类型 | 名称 | 作用 |
|---|---|---|---|
| 1 | TABLE | `adjudications` | 裁决主表，含 4 条新增 CHECK（`consumed_by`/`revoked_by` 非空；`consumed_at`/`revoked_at` 必须有 `sealed_at`） |
| 2 | TABLE | `adjudication_evidence` | 证据子表（append-only） |
| 3 | INDEX | `ux_adj_consumable` | **唯一槽位**：每 action 至多 1 条可消费裁决 |
| 4 | INDEX | `ix_adj_action` | 按 action 时间检索 |
| 5 | INDEX | `ix_adj_pending` | 待消费集合 |
| 6 | INDEX | `ix_adj_supersedes` | 取代链 |
| 7 | INDEX | `ix_adj_revoked` | 撤销集合 |
| 8 | INDEX | `ix_ev_adj` | 证据按裁决 |
| 9 | INDEX | `ix_ev_type` | 证据按类型 + 时间 |
| 10 | TRIGGER | `trg_adj_insert_unsealed` | 插入必须未封存 |
| 11 | TRIGGER | `trg_adj_seal_requires_evidence` | 封存要求 ≥1 条证据 |
| 12 | TRIGGER | `trg_adj_seal_exclusive` | **新增**：封存不得与消费/撤销同一条 UPDATE |
| 13 | TRIGGER | `trg_ev_no_insert_after_seal` | 封存后不得追加证据 |
| 14 | TRIGGER | `trg_ev_no_update` | 证据不可改 |
| 15 | TRIGGER | `trg_ev_no_delete` | 证据不可删（REPLACE 的第二道防线） |
| 16 | TRIGGER | `trg_adj_immutable_cols` | 16 个业务/生命周期字段不可变 |
| 17 | TRIGGER | `trg_adj_no_delete` | 主表 append-only |

主表新增 CHECK（相对 SP3-0R3-FINAL 的增量，即任务书点名的两项）：

```sql
CHECK (consumed_by IS NULL OR length(trim(consumed_by)) > 0),
CHECK (revoked_by  IS NULL OR length(trim(revoked_by))  > 0),
CHECK (consumed_at IS NULL OR sealed_at IS NOT NULL),   -- P1a
CHECK (revoked_at  IS NULL OR sealed_at IS NOT NULL)    -- P1a
```

全文见 `scripts/actionledger.py:150-282`（可由 `python -c "import actionledger as A; print(chr(10).join(A._ADJUDICATION_DDL))"` 无损导出）。

---

## §3 `decide()` 实现与调用链

### 3.1 两阶段结构（P0 分工的落地方式）

```
调用方
  └─ decide_adjudication(action_id, decision, evidence, operator,
                          authorization_ref, reason, scope, ...)
       ├─ 阶段 A（事务外，只读校验，零写入）
       │     decision ∈ 5 值枚举
       │     evidence 非空 + type ∈ 5 类白名单 + locator 非空 + ms > 0
       │     operator 非空 且 ∉ {system, bot, auto}
       │     authorization_ref / scope 非空；reason ≥ 20 字
       │     DECIDED_NOT_SENT → reviewer 必填、≠ operator、reviewed_at 必填
       │     captured_at_ms ∈ [decided_at-24h, decided_at]
       │     digest = _adj_hash(rows)
       ├─ 阶段 B（BEGIN IMMEDIATE，单事务）
       │     查 action → 不存在 SP-ACT-005 / 已 COMPLETED → SP-ACT-016
       │     fencing：账本有则必须声明且相等（011/012，与 commit_outcome 同规则）
       │     supersedes：存在 → 同 action → 已封存 → **已退出可消费槽位** → 链无环（<64）
       │     INSERT 主表（sealed_at = NULL）
       │     INSERT 证据（seq = 1..n）
       │     COUNT(evidence) ≥ 1
       │     由**落库证据集合**复算 hash 并与主表比对（D14）
       │     UPDATE sealed_at
       │     写 `adjudicated` 事件
       └─ COMMIT；任一步失败一律 ROLLBACK（含 IntegrityError 分支）
```

### 3.2 解锁路径唯一

```
consume_adjudication(action_id, adjudication_id, result_tag, expected_fencing)
  ├─ 裁决存在 / 属该 action / 已封存
  ├─ decision ∈ {DECIDED_SENT, DECIDED_NOT_SENT}
  ├─ decision 与 result_tag 必须配对
  ├─ action.state ∈ {SEND_UNKNOWN, ANTI_REPLAY_LOCKED}
  ├─ fencing 三方一致（action / adjudication / 调用方）
  ├─ intent_key + business_target_norm 绑定一致
  ├─ 证据 hash 复算（不信任已存值）
  ├─ 条件 UPDATE 占位 → rowcount ≠ 1 → SP-ACT-014
  ├─ 条件 UPDATE actions（state 白名单）→ rowcount ≠ 1 → SP-ACT-016
  └─ 写 `adjudication_consumed` + `outcome` 事件
```

`commit_outcome` **未做任何修改**，对 UNKNOWN 仍无条件拒绝（由 SP1-A 静态断言
`test_commit_outcome_never_reads_adjudications` 持续守住）。

---

## §4 新增测试逐项结果（50/50 OK）

| 组 | 用例 | 结果 |
|---|---|---|
| A 生命周期 | 未封存消费 / 未封存撤销 / 封存+消费同更新 / API 拒未封存 / 消费撤销互斥 | 5/5 |
| B 不可变性 | 16 字段逐改 / `sealed_at` 不可逆 / 生命周期只写一次 / 主表 DELETE / 证据改删 | 5/5 |
| C REPLACE | 主表 OFF 泄漏 / 主表 ON 拦截 / 证据 OFF 泄漏 / 证据 ON 拦截 / 已封存双拒 / 生产连接 ON | 6/6 |
| D 证据封存 | 空证据封存拒 / 直接插已封存拒 / 封存后追加拒 / API 空证据拒 / hash 绑定落库集合 | 5/5 |
| E supersedes | 不存在 / 跨 action / 未封存 / 自引用 / 环 / **占槽位拒** / 撤销后可取代 | 7/7 |
| F 原子性 | 主键冲突零残留 / 12 种预检失败零残留 / 双人复核 | 3/3 |
| G 并发 | 同 action 并发裁决 `wins=1` / 同裁决并发消费 `wins=1` | 2/2 |
| H 外键 | 孤儿证据拒（ON）/ 放行（OFF 对照）/ PRAGMA 连接局部性 | 3/3 |
| I 回滚 | FK=ON 时 DROP 失败 / 维护连接 FK=OFF 成功 + 结构重建 | 2/2 |
| J 不变量 | decide 不改状态 / G6 未削弱 / 消费完成 / 二次消费拒 / tag 配对 / 非可消费决策 / fencing / 错误 fencing / 已完成不可裁 / hash 向量 / `_by` 非空 / 撤销不解锁 | 12/12 |

---

## §5 回归结果

```
Ran 1135 tests in 294.888s
OK
```

- 基线 1083 + 新增 50 = 1133，另 SP1-A 守卫由 1 条拆为 3 条（+2）= **1135**。
- 首轮回归曾出现 2 条 FAIL：`test_h3_no_adjudication_table_exists`、
  `test_no_sp2_sp3_symbols_in_ledger`。二者均为「SP3 不得实施」的阶段守卫，
  其前提被本次授权变更取代。处理方式见 §9.3（**不降强度**）。
- 日志：`project-04-p2-3-work/regression-sp31.log`

---

## §6 REPLACE 专项实验（`sp31-replace-probe.log`）

| 冲突面 | `recursive_triggers=OFF` | `recursive_triggers=ON` |
|---|---|---|
| 主表 REPLACE | **ACCEPTED**，`operator` → `attacker`，`sealed_at` 被抹为 NULL | REJECTED `adjudication_append_only` |
| 证据 REPLACE（未封存） | **ACCEPTED**，`locator` → `HACKED` | REJECTED `evidence_immutable` |
| 证据 REPLACE（已封存） | REJECTED `evidence_sealed` | REJECTED `evidence_sealed` |

结论：OFF 是**真实的失败开放面**（可整行替换已封存裁决）；已封存证据因 INSERT
侧触发器独立生效而双拒。故生产连接强制 `ON`，并在 `_apply_pragmas()` 中**读回断言**，
未生效即抛错（fail-closed）。

生产代码静态扫描：`scripts/` 下 `INSERT OR REPLACE` / `REPLACE INTO` **零命中**
（唯一命中是 `_apply_pragmas` 的说明注释）。

---

## §7 发现并修复的 2 个缺陷

### 7.1 `revoke_adjudication` 事件挂错审计轴

原实现 `self._ev(adjudication_id, "adjudication_revoked", ...)` 把**裁决 ID** 当作
`action_id` 写入 `events`（该表无外键，不会报错，但会凭空造出不存在的 action_id，
且按 action 追溯时该事件不可见）。已改为先取所属 `action_id`，事件落在正确轴上，
并在 detail 中保留 `adj=<id>`。

### 7.2 supersedes 撞唯一槽位时返回原始 SQLite 文本

`ux_adj_consumable` 要求每 action 至多一条「已封存且未消费未撤销」的裁决；因此
**旧裁决还在槽位上时无法被取代**，原实现直接撞索引并回 `UNIQUE constraint failed:
adjudications.action_id`，调用方无从判断原因。已在事务内补前置检查，返回
`supersedes target still holds the consumable slot — revoke it before superseding`
（`data.occupies_slot = true`）。

---

## §8 Git 基线

```
HEAD   25743ae7a7facbe429d60e315ae5d741d96a27c2
branch main
M  scripts/statelock.py        （既有改动，非本轮）
M  tests/test_platformlock.py  （既有改动，非本轮）
M  tests/test_statelock.py     （既有改动，非本轮）
```

本轮**未执行**任何 `git add / commit / push / tag / branch`。P2 系列文件（含
`actionledger.py`）本身仍为未跟踪状态，属项目既有基线，未擅自纳入版本控制。

---

## §9 需终审裁定的 3 项（我不自行裁定）

### 9.1 supersedes 的「两段式」语义是否接受？

实测（`sp31-supersede-probe.log`）：

| 场景 | 结果 |
|---|---|
| 旧裁决仍占槽位时 supersede | **拒绝**（唯一槽位索引，D05） |
| 先 `revoke_adjudication` 再 supersede | 成功，链建立，新裁决可消费 |

即：**D05「槽位唯一」与 D11「supersedes」的交点 = 取代必须显式先撤销**。
我未擅自加「自动撤销旧裁决」的隐式行为（那会让撤销不再由人显式发起）。
若终审认为应由 `decide()` 在同一事务内自动撤销旧裁决，请明示——这是设计变更，需授权。

### 9.2 `_adj_hash` 的 `seq` 是否纳入摘要？

当前按 SP3-0R2 §2.2 冻结的规范化编码为
`type ‖ 0x1F ‖ NFC(trim(locator)) ‖ 0x1F ‖ str(ms) ‖ 0x1E`，**不含 seq**；
`seq` 仅用于排序与主键。因此「删除中间证据后重编号」在理论上不改变摘要
（但证据不可删，此路径已被触发器封闭）。若要求把 `seq` 纳入摘要，需变更算法前缀 `adj-v1`。

### 9.3 两条阶段守卫的改法是否认可？

- `tests/test_p2_g6_hotfix.py`：原 `test_h3_no_adjudication_table_exists` 断言
  「裁决表不存在」。改为 `test_h3_adjudication_table_exists_yet_g6_still_rejects`：
  **前提变了（表存在），结论不降**（断言两种 unlock 形态仍被 `SP-ACT-003` 全拒）。
- `tests/test_p2_sp1a.py`：原 `test_no_sp2_sp3_symbols_in_ledger` 断言生产代码
  不得出现 `adjudications`。改为 3 条更强断言：
  ① SP2 符号仍禁用；② SP3 仅允许 3 个公开入口（各恰好 1 处定义）；
  ③ **`commit_outcome` 函数体内不得出现对裁决表的读取或裁决 API 调用**。

这两处是本轮**唯一触碰既有测试**的地方，属「阶段守卫随阶段推进而换语义」，
已尽量做到强度不降。若终审认为应改为其他形式（例如加 `@skipUnless(SP3_ENABLED)`），请示下。

---

## §10 停止状态

依任务书「完成后暂停」：SP3-1 代码与测试已落地，**未进入 SP3-2**，未触碰任何
生产数据库，未做任何 git 写操作。等待终审。
