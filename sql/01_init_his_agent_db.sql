-- =========================================================================
-- 《私立医院智能业务管理 Agent 系统开发设计文档 (v1.2)》 第 9.1 节
-- 本地 SQL Server 2022 建表脚本（sql/01_init_his_agent_db.sql）
-- =========================================================================

IF DB_ID(N'his_agent_db') IS NULL
BEGIN
    CREATE DATABASE [his_agent_db] COLLATE Chinese_PRC_CI_AS;
END
GO

USE [his_agent_db];
GO

-- 1. 院内 DIP 病种分值规则表（支持多版本保留与审核状态控制）
IF OBJECT_ID(N'dbo.dip_rules', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.dip_rules (
        rule_id          INT IDENTITY(1, 1) PRIMARY KEY,
        region_code      NVARCHAR(50)    NOT NULL DEFAULT N'LOCAL_SAMPLE',
        rule_version     NVARCHAR(50)    NOT NULL,
        effective_date   DATE            NOT NULL,
        expiry_date      DATE            NULL,
        source_doc       NVARCHAR(200)   NOT NULL,
        review_status    NVARCHAR(20)    NOT NULL DEFAULT N'APPROVED',
        department       NVARCHAR(50)    NOT NULL,
        dip_group_name   NVARCHAR(300)   NOT NULL,
        icd_code         NVARCHAR(50)    NOT NULL,
        icd_name         NVARCHAR(200)   NOT NULL,
        op_code          NVARCHAR(200)   NOT NULL,
        op_name          NVARCHAR(300)   NOT NULL,
        catalog_type     NVARCHAR(20)    NOT NULL,
        aux_flag         TINYINT         NOT NULL DEFAULT 0,
        aux_detail       NVARCHAR(200)   NOT NULL DEFAULT N'',
        billing_unit     NVARCHAR(20)    NOT NULL DEFAULT N'PER_CASE',
        dip_score        DECIMAL(12, 4)  NOT NULL,
        resident_point   DECIMAL(10, 4)  NOT NULL DEFAULT 7.0000,
        employee_point   DECIMAL(10, 4)  NOT NULL DEFAULT 7.5000,
        hosp_coef        DECIMAL(10, 4)  NOT NULL DEFAULT 0.6000,
        low_threshold    DECIMAL(6, 4)   NOT NULL DEFAULT 0.9000,
        high_threshold   DECIMAL(6, 4)   NOT NULL DEFAULT 1.1000,
        rounding_mode    NVARCHAR(30)    NOT NULL DEFAULT N'HALF_UP_2DP',
        resident_base    DECIMAL(14, 4)  NOT NULL,
        resident_min     DECIMAL(14, 4)  NOT NULL,
        resident_max     DECIMAL(14, 4)  NOT NULL,
        employee_base    DECIMAL(14, 4)  NOT NULL,
        employee_min     DECIMAL(14, 4)  NOT NULL,
        employee_max     DECIMAL(14, 4)  NOT NULL,
        created_at       DATETIME2       NOT NULL DEFAULT SYSDATETIME(),
        updated_at       DATETIME2       NOT NULL DEFAULT SYSDATETIME()
    );
    CREATE INDEX IX_dip_rules_lookup ON dbo.dip_rules(rule_version, review_status, icd_code, department);
END
GO

-- 2. 在院患者 DIP 实时监控快照表（仅用于展示，不得作为当前授权依据）
IF OBJECT_ID(N'dbo.inpatient_dip_snapshot', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.inpatient_dip_snapshot (
        snapshot_id       BIGINT IDENTITY(1, 1) PRIMARY KEY,
        batch_id          NVARCHAR(64)   NOT NULL,
        visit_id          NVARCHAR(64)   NOT NULL,
        ward_name         NVARCHAR(50)   NOT NULL,
        bed_no            NVARCHAR(30)   NOT NULL,
        dept_name         NVARCHAR(50)   NOT NULL,
        doctor_id         NVARCHAR(50)   NOT NULL,
        age               INT            NULL,
        ins_type          NVARCHAR(20)   NOT NULL,
        primary_icd       NVARCHAR(50)   NULL,
        matched_rule_id   INT            NULL,
        rule_version      NVARCHAR(50)   NULL,
        current_fee       DECIMAL(14, 4) NOT NULL DEFAULT 0,
        target_base       DECIMAL(14, 4) NULL,
        fee_ratio         DECIMAL(12, 6) NULL,
        alert_level       NVARCHAR(20)   NOT NULL,
        pending_reason    NVARCHAR(200)  NULL,
        discharge_review  BIT            NOT NULL DEFAULT 0,
        op_audit_status   NVARCHAR(30)   NULL,
        data_cutoff_time  DATETIME2      NOT NULL,
        synced_at         DATETIME2      NOT NULL DEFAULT SYSDATETIME()
    );
    CREATE UNIQUE INDEX UX_snapshot_batch_visit ON dbo.inpatient_dip_snapshot(batch_id, visit_id);
END
GO

-- 3. Agent 审计日志表（默认不保存原始对话、患者自由文本与返回明细）
IF OBJECT_ID(N'dbo.agent_audit_log', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.agent_audit_log (
        log_id          BIGINT IDENTITY(1, 1) PRIMARY KEY,
        operator_id     NVARCHAR(64)   NOT NULL,
        role_type       NVARCHAR(30)   NOT NULL,
        channel         NVARCHAR(30)   NOT NULL,
        action_type     NVARCHAR(50)   NOT NULL,
        target_ref      NVARCHAR(100)  NULL,
        auth_result     NVARCHAR(20)   NOT NULL,
        rule_version    NVARCHAR(50)   NULL,
        batch_id        NVARCHAR(64)   NULL,
        created_at      DATETIME2      NOT NULL DEFAULT SYSDATETIME()
    );
END
GO

-- 4. 用户权限与身份映射表（统一服务端 RBAC 鉴权与钉钉映射）
IF OBJECT_ID(N'dbo.sys_user_role', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.sys_user_role (
        user_id         NVARCHAR(64)   PRIMARY KEY,
        dingtalk_userid NVARCHAR(100)  NULL,
        role_type       NVARCHAR(30)   NOT NULL,
        dept_scope      NVARCHAR(300)  NOT NULL DEFAULT N'',
        can_export_case BIT            NOT NULL DEFAULT 0,
        status          NVARCHAR(20)   NOT NULL DEFAULT N'ACTIVE',
        updated_at      DATETIME2      NOT NULL DEFAULT SYSDATETIME()
    );
END
GO

IF OBJECT_ID(N'dbo.sys_operator_account', N'U') IS NULL
BEGIN
    CREATE TABLE dbo.sys_operator_account (
        operator_id     NVARCHAR(64)   PRIMARY KEY,
        display_name    NVARCHAR(64)   NOT NULL,
        password_hash   NVARCHAR(256)  NOT NULL,
        role_type       NVARCHAR(30)   NOT NULL,
        dept_scope      NVARCHAR(300)  NOT NULL DEFAULT N'',
        dingtalk_userid NVARCHAR(100)  NULL,
        can_export_case BIT            NOT NULL DEFAULT 0,
        status          NVARCHAR(20)   NOT NULL DEFAULT N'ACTIVE',
        created_at      DATETIME2      NOT NULL DEFAULT SYSDATETIME(),
        updated_at      DATETIME2      NOT NULL DEFAULT SYSDATETIME()
    );
END
GO
