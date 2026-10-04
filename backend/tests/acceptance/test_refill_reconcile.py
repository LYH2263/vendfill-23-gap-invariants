"""验收对账：补货生成、满仓列表、汇总页的数字一律拿现网接口核对。

口径（与补货引擎同一套严格规则，禁止另写更松的缺口算法把验收刷绿）：
    gap = capacity - stock - in_transit
    gap < 0  -> overbooked，补量必须为 0，且不得出现在满仓列表
    gap == 0 -> full，补量必须为 0
    gap > 0  -> need_fill，补量 = min(想补, gap)，恒 >= 0

硬性规则（违反任意一条即整场失败，哪怕夹具其余部分全绿）：
    1. 期望值每轮都从现网货道状态现算；夹具改过库存再跑，必须按新库存出数，
       禁止沿用上一轮算过的补量。
    2. stock + in_transit > capacity 的货道，核对若把它判成满仓，整场失败。
    3. 正补量行不得再带一份失败原因（现网返回和本核对报告都不允许）。
    4. 取最近一张单对齐数字时，若当时还没有单、现网因此又生成一张，
       本次核对算失败，不得靠这种副作用把失败跑变成半成功落单。
    5. 无论成败，跑完把库和满仓列表一起恢复到绿仓种子：
       货道回种子值、补货单清空，不得多留下一张成功补货单。

运行方式（需要现网在跑）：docker compose exec api pytest -q
环境变量：API_BASE_URL（默认 http://localhost:9800）、
          DATABASE_URL（默认与 app 配置一致；compose 的 api 容器内已注入）。
"""
from __future__ import annotations

import os

import httpx
import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine

API_BASE_URL = os.environ.get("API_BASE_URL", "http://localhost:9800").rstrip("/")
DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql+psycopg2://vendfill:vendfill@localhost:5449/vendfill"
)
LOCATION_ID = 1

# 绿仓种子，与 app.services.seed.seed_if_empty 一致：
# (slot_no, sku_name, capacity, stock, in_transit)
SEED_LANES = [
    ("A1", "矿泉水", 20, 5, 0),
    ("A2", "可乐", 18, 18, 0),
    ("B1", "薯片", 12, 3, 2),
    ("B2", "巧克力", 15, 10, 5),
    ("C1", "能量棒", 10, 0, 0),
    ("C2", "口香糖", 24, 24, 2),
]

REASON_KEYS = ("reason", "fail_reason", "failure", "error", "message")


class HarnessError(AssertionError):
    """夹具自身违反硬性规则：整场失败，与单个用例无关。"""


class SideEffectOrderError(HarnessError):
    """取最近一张单时现网因无单又生成了一张：本次核对判失败。"""


def strict_expected_lines(lanes: list[dict], requested: dict[int, int] | None = None) -> list[dict]:
    """按严格口径从当前货道状态现算期望行（每轮调用都重算，不沿用上一轮）。"""
    expected = []
    for lane in lanes:
        cap = int(lane["capacity"])
        stock = int(lane["stock"])
        transit = int(lane["in_transit"])
        gap = cap - stock - transit
        if gap < 0:
            status, fill = "overbooked", 0
        elif gap == 0:
            status, fill = "full", 0
        else:
            status = "need_fill"
            desire = gap if requested is None else int(requested.get(lane["id"], gap))
            fill = max(0, min(desire, gap))
        # 硬性规则 2：库存+在途已超容量的货道，核对永远不得判满仓
        if stock + transit > cap and status == "full":
            raise HarnessError(f"货道 {lane['slot_no']} 库存+在途已超容量，核对不得判满仓")
        expected.append({
            "lane_id": lane["id"], "slot_no": lane["slot_no"],
            "capacity": cap, "stock": stock, "in_transit": transit,
            "gap": gap, "fill_qty": fill, "status": status,
        })
    return expected


def strict_totals(expected: list[dict]) -> dict:
    return {
        "total_fill": sum(e["fill_qty"] for e in expected),
        "need_fill_count": sum(1 for e in expected if e["status"] == "need_fill"),
        "full_count": sum(1 for e in expected if e["status"] == "full"),
        "overbooked_count": sum(1 for e in expected if e["status"] == "overbooked"),
    }


class LiveApi:
    """现网接口客户端：所有数字都以接口返回为准，GET 类接口带副作用守卫。"""

    def __init__(self, engine: Engine):
        self.engine = engine
        self.client = httpx.Client(base_url=API_BASE_URL, timeout=10.0)

    # ---- 现网只读接口（带“副作用落单”守卫） ----

    def _order_count(self) -> int:
        with self.engine.connect() as conn:
            return conn.execute(
                text("SELECT COUNT(*) FROM refill_orders WHERE location_id = :loc"),
                {"loc": LOCATION_ID},
            ).scalar_one()

    def _get_aligned(self, path: str) -> dict:
        """取数对齐前先看有没有单；若这次 GET 让现网多生出一张单，本次核对判失败。"""
        before = self._order_count()
        data = self._get(path)
        after = self._order_count()
        if after != before:
            raise SideEffectOrderError(
                f"GET {path} 时现网还没有单，因这次取数又生成了第 {after} 张；"
                "本次核对判失败，不得靠副作用落单"
            )
        return data

    def _get(self, path: str) -> dict:
        resp = self.client.get(path)
        resp.raise_for_status()
        return resp.json()

    def lanes(self) -> list[dict]:
        return self._get(f"/api/lanes?location_id={LOCATION_ID}")

    def latest(self) -> dict:
        return self._get_aligned(f"/api/refills/latest?location_id={LOCATION_ID}")

    def full(self) -> dict:
        return self._get_aligned(f"/api/refills/full?location_id={LOCATION_ID}")

    def summary(self) -> dict:
        return self._get_aligned(f"/api/refills/summary?location_id={LOCATION_ID}")

    def run_refill(self, requested: dict[int, int] | None = None) -> dict:
        body = {"requested": requested} if requested is not None else None
        resp = self.client.post(
            "/api/refills/run", params={"location_id": LOCATION_ID}, json=body
        )
        resp.raise_for_status()
        return resp.json()

    # ---- 夹具：直接改库（改库存 / 清补货单 / 恢复绿仓种子） ----

    def set_lane(self, slot_no: str, *, stock: int | None = None, in_transit: int | None = None) -> None:
        sets, params = [], {"loc": LOCATION_ID, "slot": slot_no}
        if stock is not None:
            sets.append("stock = :stock")
            params["stock"] = stock
        if in_transit is not None:
            sets.append("in_transit = :transit")
            params["transit"] = in_transit
        if not sets:
            return
        with self.engine.begin() as conn:
            conn.execute(
                text(f"UPDATE lanes SET {', '.join(sets)} "
                     "WHERE location_id = :loc AND slot_no = :slot"),
                params,
            )

    def delete_orders(self) -> None:
        with self.engine.begin() as conn:
            conn.execute(
                text("DELETE FROM refill_orders WHERE location_id = :loc"),
                {"loc": LOCATION_ID},
            )

    def restore_green_seed(self) -> None:
        """货道回种子值、补货单清空（绿仓种子本来就没有补货单）。"""
        with self.engine.begin() as conn:
            for slot, sku, cap, stock, transit in SEED_LANES:
                result = conn.execute(
                    text(
                        "UPDATE lanes SET sku_name = :sku, capacity = :cap, "
                        "stock = :stock, in_transit = :transit "
                        "WHERE location_id = :loc AND slot_no = :slot"
                    ),
                    {"sku": sku, "cap": cap, "stock": stock, "transit": transit,
                     "loc": LOCATION_ID, "slot": slot},
                )
                if result.rowcount != 1:
                    raise HarnessError(f"绿仓种子缺货道 {slot}：请先启动现网让种子落库")
            conn.execute(
                text("DELETE FROM refill_orders WHERE location_id = :loc"),
                {"loc": LOCATION_ID},
            )

    def assert_green_seed(self) -> None:
        """恢复必须真的生效：库回种子、一张补货单都不许多留。"""
        with self.engine.connect() as conn:
            rows = conn.execute(
                text("SELECT slot_no, sku_name, capacity, stock, in_transit FROM lanes "
                     "WHERE location_id = :loc ORDER BY slot_no"),
                {"loc": LOCATION_ID},
            ).all()
            orders = conn.execute(
                text("SELECT COUNT(*) FROM refill_orders WHERE location_id = :loc"),
                {"loc": LOCATION_ID},
            ).scalar_one()
        actual = [(r[0], r[1], r[2], r[3], r[4]) for r in rows]
        if actual != SEED_LANES:
            raise HarnessError(f"跑完库未恢复到绿仓种子：{actual}")
        if orders != 0:
            raise HarnessError(f"验收结束不得多留下补货单，实际剩 {orders} 张")


class ReconcileResult:
    def __init__(self, expected: list[dict], run: dict, full: dict, summary: dict, latest: dict):
        self.expected = expected
        self.run = run
        self.full = full
        self.summary = summary
        self.latest = latest

    def run_line(self, slot_no: str) -> dict:
        return next(l for l in self.run["lines"] if l["slot_no"] == slot_no)

    def full_slots(self) -> set[str]:
        return {l["slot_no"] for l in self.full["lanes"]}


def reconcile(api: LiveApi, requested: dict[int, int] | None = None) -> ReconcileResult:
    """一轮完整对账：生成 -> 满仓 -> 汇总 -> 最近单，全部与现网接口精确对齐。"""
    # 1) 期望值从现网货道状态现算（夹具改过库存这里自然按新库存出数）
    lanes = api.lanes()
    expected = strict_expected_lines(lanes, requested)
    totals = strict_totals(expected)
    lane_state = {l["id"]: l for l in lanes}

    # 2) 现网生成补货单，逐行精确对账
    run = api.run_refill(requested)
    actual_by_id = {l["lane_id"]: l for l in run["lines"]}
    assert set(actual_by_id) == {e["lane_id"] for e in expected}, (
        "补货单货道集合与现网货道不一致"
    )
    # 报告行每轮现算：reason 只可能来自本轮真实比对，对得上的行（含正补量行）
    # 不得再带一份失败原因，禁止把上一轮的补量或原因沿用到这一轮。
    rows = []
    for e in expected:
        a = actual_by_id[e["lane_id"]]
        reason = ""
        if (a["gap"], a["fill_qty"], a["status"]) != (e["gap"], e["fill_qty"], e["status"]):
            reason = (f"{e['slot_no']} 期望 gap={e['gap']}/补量={e['fill_qty']}/{e['status']}，"
                      f"实得 gap={a['gap']}/补量={a['fill_qty']}/{a['status']}")
        rows.append({"slot_no": e["slot_no"], "fill_qty": a["fill_qty"], "reason": reason})
    failures = [r for r in rows if r["reason"]]
    assert not failures, "补货单与严格口径不一致：" + "；".join(r["reason"] for r in failures)

    # 现网返回里，正补量行不得携带失败原因字段
    for line in run["lines"]:
        if line["fill_qty"] > 0:
            leaked = {k: line[k] for k in REASON_KEYS if line.get(k)}
            assert not leaked, f"正补量行不得携带失败原因：{line}"

    # 超占道：补量必须为 0，且现网不得判满仓（硬性规则 2）
    for line in run["lines"]:
        st = lane_state[line["lane_id"]]
        if st["stock"] + st["in_transit"] > st["capacity"]:
            assert line["fill_qty"] == 0, f"超占道 {line['slot_no']} 补量必须为 0"
            assert line["status"] == "overbooked", (
                f"超占道 {line['slot_no']} 被判成 {line['status']}，整场失败"
            )

    # 3) 满仓列表对账：与当次补货单同源、超占道不得混入
    full = api.full()
    expected_full = {e["lane_id"] for e in expected if e["status"] == "full"}
    expected_over = {e["lane_id"] for e in expected if e["status"] == "overbooked"}
    actual_full = {l["lane_id"] for l in full["lanes"]}
    assert actual_full == expected_full, (
        f"满仓列表应为 {sorted(expected_full)}，实为 {sorted(actual_full)}"
    )
    assert not (actual_full & expected_over), "超占道不得出现在满仓列表"
    for l in full["lanes"]:
        assert l["fill_qty"] == 0 and l["status"] == "full" and l["gap"] == 0, (
            f"满仓行数据异常：{l}"
        )

    # 4) 汇总对账：既要符合严格口径，也要与当次补货单一致（两个接口不得打架）
    summary = api.summary()
    for key, want in totals.items():
        assert summary[key] == want, f"汇总 {key} 期望 {want}，实得 {summary[key]}"
        assert run[key] == summary[key], (
            f"补货单与汇总不一致：{key} 单={run[key]} 汇总={summary[key]}"
        )
    assert summary["full_count"] == len(full["lanes"]), "汇总满仓数与满仓列表行数不一致"

    # 5) 最近一张单必须就是本轮生成的这张（无单时现网副作用落单已在守卫里判失败）
    latest = api.latest()
    assert latest["id"] == run["id"], (
        f"最近一张单应为 #{run['id']}，实为 #{latest['id']}：核对没对上本轮生成的单"
    )
    assert latest["lines"] == run["lines"], "落库补货单与生成返回值不一致"

    return ReconcileResult(expected, run, full, summary, latest)


@pytest.fixture(scope="module")
def live():
    """现网可达性 + 绿仓种子：开场恢复一次，跑完无论成败都恢复并校验。"""
    try:
        httpx.get(f"{API_BASE_URL}/api/health", timeout=5.0).raise_for_status()
    except Exception as exc:
        raise HarnessError(f"现网接口不可达（{API_BASE_URL}）：请先 docker compose up。{exc}")
    engine = create_engine(DATABASE_URL, pool_pre_ping=True)
    try:
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HarnessError(f"数据库不可达（{DATABASE_URL}）：{exc}")
    api = LiveApi(engine)
    api.restore_green_seed()
    try:
        yield api
    finally:
        # 验收失败时也不得留下夹具产生的成功补货单
        api.restore_green_seed()
        api.assert_green_seed()
        engine.dispose()


@pytest.fixture(autouse=True)
def green_seed(live):
    """每个场景开跑前都回到绿仓种子：夹具改的库存只作用于本场景这一轮。"""
    live.restore_green_seed()


def test_seed_state_reconciles(live):
    """绿仓种子：A1/B1/C1 待补，A2/B2 满仓，C2 在途过大超占（24+2>24）。"""
    res = reconcile(live)
    assert res.run_line("A1")["fill_qty"] == 15
    assert res.run_line("B1")["fill_qty"] == 7
    assert res.run_line("C1")["fill_qty"] == 10
    assert res.run_line("A2")["status"] == "full"
    assert res.run_line("B2")["status"] == "full"
    c2 = res.run_line("C2")
    assert c2["status"] == "overbooked" and c2["fill_qty"] == 0
    assert "C2" not in res.full_slots()
    assert res.full_slots() == {"A2", "B2"}
    assert res.summary["total_fill"] == 32
    assert res.summary["overbooked_count"] == 1


def test_overbooked_when_in_transit_overflows(live):
    """场景：在途过大变成超占 —— 补量 0、状态 overbooked、不得进满仓列表。"""
    live.set_lane("A1", in_transit=20)  # 库存 5 + 在途 20 > 容量 20
    res = reconcile(live)
    a1 = res.run_line("A1")
    assert a1["status"] == "overbooked" and a1["fill_qty"] == 0 and a1["gap"] == -5
    assert "A1" not in res.full_slots()
    assert res.summary["overbooked_count"] == 2  # A1 + C2


def test_full_when_stock_tops_capacity(live):
    """场景：库存已经顶满 —— 补量 0、状态 full、出现在满仓列表。"""
    live.set_lane("C1", stock=10, in_transit=0)  # 顶满容量 10
    res = reconcile(live)
    c1 = res.run_line("C1")
    assert c1["status"] == "full" and c1["fill_qty"] == 0 and c1["gap"] == 0
    assert "C1" in res.full_slots()
    assert res.summary["full_count"] == 3  # A2 + B2 + C1


def test_requested_above_gap_is_truncated(live):
    """场景：想补的数量超过缺口时被截成缺口；低于缺口按想补。"""
    lane_id = {l["slot_no"]: l["id"] for l in live.lanes()}
    res = reconcile(live, requested={lane_id["A1"]: 9999, lane_id["B1"]: 1})
    a1 = res.run_line("A1")
    assert a1["fill_qty"] == 15 == a1["gap"], "想补超过缺口必须截成缺口"
    assert res.run_line("B1")["fill_qty"] == 1, "想补低于缺口应按想补出数"
    assert res.summary["total_fill"] == 15 + 1 + 10


def test_rerun_after_stock_change_uses_new_stock(live):
    """夹具改过库存再跑：必须按新库存出数，禁止沿用上一轮算过的补量。"""
    first = reconcile(live)
    assert first.run_line("A1")["fill_qty"] == 15
    live.set_lane("A1", stock=8)  # 缺口 15 -> 12
    second = reconcile(live)
    a1 = second.run_line("A1")
    assert a1["gap"] == 12 and a1["fill_qty"] == 12, "必须按新库存出数"
    assert second.run["id"] != first.run["id"], "再跑必须生成新单"
    assert second.summary["total_fill"] == 12 + 7 + 10


def test_latest_without_order_fails_instead_of_side_effect(live):
    """取最近一张单时还没有单：现网会副作用再生成一张，本次核对必须判失败。"""
    live.delete_orders()
    with pytest.raises(SideEffectOrderError):
        live.latest()
    live.delete_orders()  # 清掉副作用落的那张单，不得留“半成功”补货单
