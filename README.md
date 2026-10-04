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
docker compose exec api pytest -q                      # 单测 + 现网验收
docker compose exec api pytest -m "not acceptance" -q  # 只跑纯单测
```

`backend/tests/test_refill_acceptance.py` 是现网验收：补货生成、满仓列表、汇总页的
数字全部拿 live 接口对账（严格口径 gap = 容量 − 库存 − 在途，等值比对，没有更松的
第二套算法）。覆盖超占、顶满、想补超缺口被截断等场景；验收会临时改库存，跑完自动
把货道和补货单恢复到绿仓种子，失败也不会多留一张补货单。API 地址可用
`ACCEPTANCE_BASE_URL` 覆盖（默认 `http://localhost:9800`）。
