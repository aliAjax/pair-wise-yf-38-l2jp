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

- `dataset`：受控数据集；`application`：访问申请（可绑定多个数据集）；`grant`：限时数据使用凭证。

## 访问范围与凭证

- 申请创建时提交 `dataset_ids`（数组，自动去重排序）；旧字段 `dataset_id` 仍被接受，旧数据在启动时自动补全为 `dataset_ids`。
- 审批通过的瞬间，申请范围冻结到 `approved_scope`（同时记录 `scope_application_version`）。之后再用 `update_scope` 修改申请，不会改变已发凭证的范围。
- 创建 `grant` 时无需重复提交数据集，凭证自动复制审批时的冻结范围；显式提交的范围若超出冻结范围会被拒绝。
- 数据集被 `restrict`、申请被 `withdraw` 时，引用相关范围且处于 `active` 的凭证自动转为 `suspended`（停用但可查看、可 `revoke`）；停用审计明细中保留原冻结范围。
- `update_scope` 必须携带 `expected_version`：先提交者生效，后提交者收到 `409 ConflictError`，重新读取最新版本后再确认提交。
- 同一 `applicant_id` 对完全相同的数据集范围和 `purpose` 重复提交在途申请（draft/submitted/under_review/approved）时，直接返回第一次生成的申请，不新建。


## 主要接口

- `GET /health`：健康检查。
- `GET /api/<kind>`：按对象类型查询，可用`?status=`过滤。
- `POST /api/<kind>`：创建对象；请求体为JSON。
- `GET /api/entities/<id>`：读取对象当前版本。
- `POST /api/entities/<id>/actions`：提交`{"action":"动作名","data":{...},"expected_version":数字}`。
- `GET /api/audit`：读取审计记录。

请求身份通过`X-User-Id`和`X-Role`请求头传入。创建和动作的可执行角色由规则引擎控制。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 局限

数据目录和授权凭证是治理流程演示，不包含真实数据下载、加密或机构身份联邦。
