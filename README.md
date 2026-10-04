# VendFill 售货机补货

按货道容量、库存与在途量计算缺口，生成不超缺口、非负的补货单。

技术栈：Python 3.12 / FastAPI / SQLAlchemy / PostgreSQL / Vue 3 / TypeScript / Vite

## 启动

```bash
docker compose up --build
```

| 服务 | 地址 |
| --- | --- |
| 前端 | http://localhost:4800 |
| API | http://localhost:9800 |
| API 文档 | http://localhost:9800/docs |
| Postgres | localhost:5449 |

健康检查：`GET http://localhost:9800/api/health`

## 使用说明

1. 在「点位」「货道」查看售货机布局与库存。
2. 在「销量」了解近期出货。
3. 打开「补货单」按缺口生成建议补货量。
4. 在「满仓」「汇总」查看已满货道与补货合计。

## 开发与测试

```bash
docker compose exec api pytest -q
```

单测覆盖补货引擎口径；`tests/acceptance/` 是验收对账，直接打现网接口
（`POST /api/refills/run`、`GET /api/refills/full`、`GET /api/refills/summary`），
期望值一律按严格口径 `gap = 容量 − 库存 − 在途` 从当前货道现算，逐行精确比对：

- 库存 + 在途 > 容量的货道：补量必须 0、状态 `overbooked`，不得出现在满仓列表；
- 正补量行不得携带失败原因；
- 夹具改库存后再跑必须按新库存出数；
- 取最近一张单时若现网因无单又生成一张，本次核对判失败；
- 无论成败，跑完货道回绿仓种子、补货单清空。

环境变量：`API_BASE_URL`（默认 `http://localhost:9800`）、`DATABASE_URL`
（compose 的 api 容器内已注入，默认同 `app.config`）。
