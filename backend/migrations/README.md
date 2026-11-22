# 结构升级

`src/models/tables.py` 定义关系结构。API 启动时先用 `MetaData.create_all` 创建演示库中缺失的表，再运行 `src/models/upgrade.py` 的 `ensure_schema`：它通过 inspector 比对列，幂等地为已存在的旧表补上发布后新增的列，PostgreSQL 与 MySQL 均适用。

## 版本记录

- 初始版本：`accounts`、`business_cases`、`business_runs`、`business_evidence`、`audit_events`。
- 到期清理：`business_cases` 增加可空列 `cleaned_at VARCHAR(32)`。清理后 `payload` 替换为 `{"cleaned": true}` 标记，未签证据行删除，已签证据行保留。
