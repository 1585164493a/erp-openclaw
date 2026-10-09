-- ============================================================================
-- Phase 1：Agent 长期记忆持久化（2026-09-20） ERP_OPENCLAW
-- ----------------------------------------------------------------------------
-- 背景：`src/agent/config.py` 里 `STORE = InMemoryStore()` 是**进程内存字典**，
--       后端重启 → 所有用户偏好（/memories/{user_id}/preferences.md）
--       与所有已持久化技能（/persisted-skills/）**全部归零**。
--       也就是说"长期记忆"此前从未真正落库。
--
-- 设计：用 MySQL 实现 LangGraph 的 BaseStore 接口，长期记忆落这里；Redis 只放
--       会话状态（任务台账 / 展示消息），MongoDB 退场。
--       完整方案见 docs/STORAGE_MIGRATION.md。
--
-- 为什么单独建库而不是塞进 motorparts_db：
--   访问模式不同（业务表按条件查 / 本表按 namespace+key 取整篇正文）、
--   权限边界不同、备份粒度不同。同实例、异库，兼顾运维便利与 schema 干净。
--
-- 幂等：CREATE DATABASE/TABLE IF NOT EXISTS，可重复执行。
-- 回滚：DROP DATABASE agent_state;   （只影响 Agent 记忆，不碰业务库）
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `agent_state`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `agent_state`;

-- ------------------------------------------------------------
-- LangGraph BaseStore 的落库表
-- ------------------------------------------------------------
-- 字段与 BaseStore 的语义一一对应：
--   namespace → LangGraph 的 namespace 元组，元组在 SQL 里拍平成
--               以 `/` 连接的字符串（如 ("user_001",) → 'user_001'，
--               ("skills",) → 'skills'）。取回时再 split 回元组。
--   key       → Store 内路径（如 '/user_001/preferences.md'）
--   value     → 任意 JSON。**长期记忆的正文就在这里**：
--               {"content": ["# 用户偏好", "- 常用供应商：...", ...],
--                "created_at": "...", "modified_at": "..."}
--               注意 content 是**行列表**（memory_update.py 里 split("\n") 过），
--               不是单个字符串 —— 与 deepagents 的 create_file_data 保持一致。
--
-- 为什么 key 用 varchar(512)：实际最长是 '/persisted-skills/{scope}/{skill}/...'
--   这类路径；512 有充足余量，且不超 InnoDB 索引长度限制（utf8mb4 下 512*4=2048B
--   接近但不超 3072B 上限）。

CREATE TABLE IF NOT EXISTS `langgraph_store` (
  `namespace`  varchar(255)  NOT NULL COMMENT 'BaseStore 命名空间（元组用 / 连接）',
  `key`        varchar(512)  NOT NULL COMMENT 'Store 内路径键',
  `value`      json          NOT NULL COMMENT '值（长期记忆正文在 value->content 里，是行数组）',
  `created_at` datetime(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6) COMMENT '创建时间',
  `updated_at` datetime(6)   NOT NULL DEFAULT CURRENT_TIMESTAMP(6)
                             ON UPDATE CURRENT_TIMESTAMP(6) COMMENT '更新时间',
  `expires_at` datetime(6)   NULL DEFAULT NULL COMMENT 'TTL 过期时间（本方案暂不使用）',
  PRIMARY KEY (`namespace`, `key`) USING BTREE,
  INDEX `idx_namespace` (`namespace`) USING BTREE,
  INDEX `idx_expires` (`expires_at`) USING BTREE
) ENGINE = InnoDB
  CHARACTER SET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'LangGraph BaseStore 持久化（Agent 长期记忆 + 持久化技能）';

-- ------------------------------------------------------------
-- 迁移记录：与 db/migrations/ 下既有迁移保持同一约定
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `_migration_log` (
  `id`          bigint       NOT NULL AUTO_INCREMENT,
  `migration`   varchar(128) NOT NULL COMMENT '迁移文件名',
  `applied_at`  datetime     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE INDEX `uk_migration` (`migration`) USING BTREE
) ENGINE = InnoDB
  CHARACTER SET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = '迁移执行记录';

INSERT IGNORE INTO `_migration_log` (`migration`) VALUES ('008_agent_state_store.sql');
