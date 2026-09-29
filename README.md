# 基因组数据访问治理

这是一个只使用Python标准库和SQLite的模块化项目，默认端口为`8304`。所有业务规则集中在`src/rules.py`，`app.py`只负责组装依赖和启动服务。

## 模块结构

- `app.py`：命令行参数、依赖组装、启动和信号处理。
- `src/domain.py`：角色、数据结构、领域异常和基础校验。
- `src/rules.py`：状态机、权限、领域计算、冲突和跨对象校验。
- `src/repository.py`：SQLite建表、查询、事务和乐观锁。
- `src/service.py`：用例编排、幂等处理、版本控制和审计写入。
- `src/http_api.py`：HTTP路由、请求解析和统一错误响应。
- `src/audit.py`：实体操作审计时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则和失败场景测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8304
```

服务启动时会自动建表。`--host`可修改监听地址，`--db`可指定其他SQLite文件。

## 核心对象

- `dataset`：受控数据集；`application`：访问申请；`grant`：限时数据使用凭证。

### 申请与凭证的范围治理

- 一个申请通过 `dataset_ids` 绑定多个数据集（旧单数据集字段 `dataset_id` 仍被读取兼容，读取响应会同时给出规范化字段）。
- 申请在 `draft/submitted/under_review` 阶段可用 `update_scope` 动作调整范围；该动作必须携带 `expected_version`，先提交者生效，后提交者收到 `409 ConflictError`，重新读取最新版本后可再次确认提交。
- 审批通过时把当时范围写入申请的 `approved_scope` 快照（含 `terms`、`expires_at`、`application_version`、`frozen_at`）；之后申请的任何修改都不会扩大已发权限。
- 凭证（grant）创建时从已批准申请复制不可变的 `scope` 快照；请求超出批准范围的数据集会被拒绝。批准后不允许再编辑申请范围。
- 同一申请人对仍未终结（`draft/submitted/under_review`）的申请，以相同范围和目的重复提交时，直接返回第一次生成的申请。
- 数据集被 `restrict`、申请被 `withdraw` 时，关联的 `issued/active` 凭证自动转为 `suspended`（`revoke` 接受 `active/suspended`）。凭证行和审计记录中的原始范围始终保留，可继续查看与撤销。

## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。申请支持`Idempotency-Key`请求头，重复提交也会按申请人+范围+目的去重。
- `GET /api/entities/<id>`：读取对象当前版本（含规范化的`dataset_ids`/`scope`字段）。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。`update_scope`必须带`expected_version`。
- `GET /api/audit`：读取审计记录；凭证级联停用时审计详情记录原因、来源对象和冻结时的原始范围。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

数据目录和授权凭证是治理流程演示，不包含真实数据下载、加密或机构身份联邦。
