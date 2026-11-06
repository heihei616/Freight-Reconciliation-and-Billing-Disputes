# MVP 系统上线部署地址

> 系统：物流费对账与计费争议 MVP（轻量演示版）
> 交付日期：2026 年 11 月 6 日

## 1. 上线部署地址（分享链接）

**https://logistics-dispute-mvp.app.workbuddy.host/?sharecode=LZCSE3LQ74**

- 服务类型：`http-service`（FastAPI 后端）
- 域名前缀：`logistics-dispute-mvp`

## 2. 管理入口

前往 **「设置 — 数据管理 — 应用」** 管理已发布的应用（查看状态、更新、下线）。

## 3. 访问验证结果

上线后实测通过：

| 检查项 | 结果 |
|---|---|
| 健康检查 `/api/health` | `{"ok":true,"service":"logistics-dispute-mvp","policy_count":8,"review_points":3}` |
| 首页标题 | 「物流费对账助手 · MVP」 |
| 制度语料 | 8 条（P-01~P-08） |
| 人工审核点 | 3 个 |

## 4. 使用说明

打开链接即可体验完整闭环：

```
选账单 → 核对 → 差异/复算 → 制度问答 → 生成争议单 → 三级人工确认
```

默认展示顺丰账单（含 3 处差异，合计多收 **4,169.61 元**）。

## 5. 部署说明（独立可部署改造）

为让 MVP 独立上线（不连同主项目 `.env` 密钥、docs、docx 一起上传），做了 4 处改造：

1. **内置金额引擎**：`domain.py` 复制到 `mvp/core/domain.py`，金额结论不偏差；
2. **改导入**：`engine.py` 由路径注入改为 `from . import domain`，MVP 完全独立；
3. **精简依赖**：`mvp/requirements.txt` 仅 `fastapi` + `uvicorn`；
4. **支持云端端口**：`--port` 读 `PORT` 环境变量，绑定 `0.0.0.0`。

## 6. 注意事项

- 线上草稿/审核为**内存态**，服务重启后清空（符合演示定位）。
- 若修改主项目 `src/logistics_agent/core/domain.py`，需同步回 `mvp/core/domain.py`，避免金额结论分叉。
