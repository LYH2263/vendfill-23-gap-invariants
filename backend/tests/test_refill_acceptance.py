"""现网验收：补货生成、满仓列表、汇总页的数字全部拿 live 接口对账。

对账对象（与前端页面一一对应）：
  POST /api/refills/run      补货生成（补货单页）
  GET  /api/refills/full     满仓列表（满仓页）
  GET  /api/refills/summary  汇总数字（汇总页）

纪律（违反任何一条，整场失败）：
  1. 期望值一律按严格口径 gap = 容量 - 库存 - 在途 当场现算、等值比对；
     禁止另写一套更松的缺口算法（容差、gap<=0 算满仓、取绝对值等）把验收刷绿。
  2. 库存 + 在途 > 容量 的货道是超占：补量必须为 0，且不得出现在满仓列表；
     核对侧同样只把 gap == 0 判满仓，把这种道判成满仓即整场失败。
  3. 正补量行（fill_qty > 0）不得再带一份失败原因：一个货道只许一份结论。
  4. 夹具改过库存再跑，必须按新库存出数（每轮重新 POST /run、期望由当轮
     /lanes 现算），禁止沿用上一轮算过的补量。
  5. 对账前必须已有本轮显式 POST /run 生成的补货单；否则现网
     GET /refills/latest（full/summary 同源）在无单时会再生成一张，
     这种核对直接判失败，且不得靠这种副作用把失败跑变成半成功落单。
  6. 无论成败，收尾把货道和补货单一起恢复到绿仓种子（seed.SEED_LANES），
     不多留下一张成功补货单。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import create_engine, delete, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models.models import Lane, RefillOrder
from app.services.fill_engine import build_fill_lines
from app.services.seed import SEED_LANES, SEED_LOCATION

pytestmark = pytest.mark.acceptance

BASE_URL = os.environ.get("ACCEPTANCE_BASE_URL", "http://localhost:9800").rstrip("/")
API_BASE = BASE_URL + "/api"


# ---------------------------------------------------------------- 严格口径

def strict_expect(capacity: int, stock: int, in_transit: int) -> tuple[int, str, int]:
    """严格口径：gap = 容量 - 库存 - 在途；超占/满仓补量恒为 0，待补补量恒等于缺口。"""
    gap = capacity - stock - in_transit
    if gap < 0:
        return gap, "overbooked", 0
    if gap == 0:
        return gap, "full", 0
    return gap, "need_fill", gap


def expected_totals(exp: dict[str, tuple[int, str, int]]) -> dict:
    return {
        "total_fill": sum(fill for (_, _, fill) in exp.values()),
        "need_fill_count": sum(1 for (_, st, _) in exp.values() if st == "need_fill"),
        "full_count": sum(1 for (_, st, _) in exp.values() if st == "full"),
        "overbooked_count": sum(1 for (_, st, _) in exp.values() if st == "overbooked"),
    }


def seed_exp() -> dict[str, tuple[int, str, int]]:
    return {slot: strict_expect(cap, stock, transit) for slot, _sku, cap, stock, transit in SEED_LANES}


def evaluate_line(line: dict, exp_gap: int, exp_status: str, exp_fill: int) -> list[str]:
    """单行对账：返回失败原因列表，空列表表示该行核对通过。"""
    slot = line["slot_no"]
    reasons = []
    if line["gap"] != exp_gap:
        reasons.append(f"{slot}: 缺口 {line['gap']} ≠ 严格缺口 {exp_gap}")
    if line["status"] != exp_status:
        reasons.append(f"{slot}: 状态 {line['status']} ≠ 严格状态 {exp_status}")
    if line["fill_qty"] != exp_fill:
        reasons.append(f"{slot}: 补量 {line['fill_qty']} ≠ 严格补量 {exp_fill}")
    if line["fill_qty"] < 0:
        reasons.append(f"{slot}: 补量不得为负（{line['fill_qty']}）")
    if exp_status == "overbooked" and line["fill_qty"] != 0:
        reasons.append(f"{slot}: 库存+在途已大于容量，补量必须为 0，实际 {line['fill_qty']}")
    if line["fill_qty"] > 0 and line["status"] != "need_fill":
        reasons.append(f"{slot}: 正补量行不得再带一份失败原因（状态 {line['status']}）")
    return reasons


# ---------------------------------------------------------------- 对账器

@dataclass
class LaneVerdict:
    """一个货道只许一份结论：要么通过（reasons 为空），要么失败（reasons 非空）。"""
    slot_no: str
    fill_qty: int
    reasons: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.reasons


@dataclass
class ReconResult:
    failures: list[str] = field(default_factory=list)
    verdicts: list[LaneVerdict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures


def reconcile(client: httpx.Client, location_id: int, run_order: dict | None) -> ReconResult:
    """拿现网接口对账一轮。

    run_order 必须是本轮显式 POST /refills/run 的响应；传 None 表示本轮没有
    显式生成补货单 —— 此时取最近一张单会触发现网副作用再生成一张，本次核对
    直接判失败，并且一个 GET 都不发，绝不靠副作用落单。
    """
    res = ReconResult()
    if run_order is None:
        res.failures.append(
            "本轮没有显式生成的补货单，拒绝对账：现网 GET /refills/latest 在无单时"
            "会再生成一张，不得靠这种副作用把失败跑变成半成功落单"
        )
        return res

    lanes = _get(client, "/lanes", location_id)
    exp = {l["slot_no"]: strict_expect(l["capacity"], l["stock"], l["in_transit"]) for l in lanes}
    lanes_by_slot = {l["slot_no"]: l for l in lanes}
    for l in lanes:
        if l["gap"] != exp[l["slot_no"]][0]:
            res.failures.append(f"/lanes 货道 {l['slot_no']} 自报缺口 {l['gap']} ≠ 严格缺口 {exp[l['slot_no']][0]}")

    latest = _get(client, "/refills/latest", location_id)
    if latest["id"] != run_order["id"]:
        res.failures.append(
            f"GET /refills/latest 返回单号 {latest['id']}，不是本轮生成的 {run_order['id']}："
            "现网在对账期间重新生成了单，本次核对判失败"
        )
    for key in ("total_fill", "need_fill_count", "full_count", "overbooked_count"):
        if run_order[key] != latest[key]:
            res.failures.append(f"补货生成与最近一张单的 {key} 不一致：{run_order[key]} ≠ {latest[key]}")
    if run_order["lines"] != latest["lines"]:
        res.failures.append("补货生成响应的行与最近一张单落库的行不一致")

    # 逐行对账（补货生成）
    lines_by_slot: dict[str, dict] = {}
    for line in latest["lines"]:
        slot = line["slot_no"]
        if slot in lines_by_slot:
            res.failures.append(f"货道 {slot} 在补货单里重复出现")
        lines_by_slot[slot] = line
    for slot, (gap, status, fill) in exp.items():
        line = lines_by_slot.get(slot)
        if line is None:
            res.failures.append(f"货道 {slot} 缺少补货行")
            continue
        reasons = evaluate_line(line, gap, status, fill)
        res.verdicts.append(LaneVerdict(slot_no=slot, fill_qty=line["fill_qty"], reasons=reasons))
        res.failures.extend(reasons)
    for slot in sorted(set(lines_by_slot) - set(exp)):
        res.failures.append(f"补货单里出现未知货道 {slot}")

    # 满仓列表：必须正好等于严格满仓集合，超占道一道都不许混进来
    full = _get(client, "/refills/full", location_id)
    full_slots = set()
    for row in full["lanes"]:
        slot = row["slot_no"]
        full_slots.add(slot)
        cur = lanes_by_slot.get(slot)
        if cur is None:
            res.failures.append(f"满仓列表出现未知货道 {slot}")
            continue
        for k in ("capacity", "stock", "in_transit"):
            if row[k] != cur[k]:
                res.failures.append(f"满仓行 {slot} 的 {k}={row[k]} 与当前库存 {cur[k]} 不一致（疑似上一轮残单）")
        if row["status"] != "full" or row["gap"] != 0 or row["fill_qty"] != 0:
            res.failures.append(f"满仓行 {slot} 自身不干净：status={row['status']} gap={row['gap']} fill={row['fill_qty']}")
    expected_full = {s for s, (_, st, _) in exp.items() if st == "full"}
    expected_over = {s for s, (_, st, _) in exp.items() if st == "overbooked"}
    leaked = full_slots & expected_over
    if leaked:
        res.failures.append(f"超占道被判成满仓：{sorted(leaked)}（库存+在途>容量，补量必须为 0 且不得进满仓列表）")
    if full_slots != expected_full:
        res.failures.append(f"满仓列表 {sorted(full_slots)} ≠ 严格满仓集合 {sorted(expected_full)}")

    # 汇总页：四个数字与严格期望、补货生成三方一致
    summary = _get(client, "/refills/summary", location_id)
    want = expected_totals(exp)
    for key, w in want.items():
        if summary[key] != w:
            res.failures.append(f"汇总页 {key}={summary[key]} ≠ 严格期望 {w}")
        if run_order[key] != w:
            res.failures.append(f"补货生成 {key}={run_order[key]} ≠ 严格期望 {w}")

    # 夹具自检：一个货道一份结论；核对通过的正补量行不得再带失败原因
    seen: set[str] = set()
    for v in res.verdicts:
        if v.slot_no in seen:
            raise RuntimeError(f"夹具自检失败：货道 {v.slot_no} 被记了多份结论")
        seen.add(v.slot_no)
        if v.ok and v.fill_qty > 0 and v.reasons:
            raise RuntimeError(f"夹具自检失败：正补量行 {v.slot_no} 又带了一份失败原因")
    return res


# ---------------------------------------------------------------- 夹具

def reset_lanes_to_seed(s: Session, location_id: int) -> None:
    existing = {l.slot_no: l for l in s.scalars(select(Lane).where(Lane.location_id == location_id)).all()}
    for slot, sku, cap, stock, transit in SEED_LANES:
        lane = existing.get(slot)
        if lane is None:
            s.add(Lane(location_id=location_id, slot_no=slot, sku_name=sku,
                       capacity=cap, stock=stock, in_transit=transit))
        else:
            lane.sku_name, lane.capacity, lane.stock, lane.in_transit = sku, cap, stock, transit


def delete_test_orders(s: Session, location_id: int, keep_ids: set[int]) -> None:
    q = delete(RefillOrder).where(RefillOrder.location_id == location_id)
    if keep_ids:
        q = q.where(~RefillOrder.id.in_(keep_ids))
    s.execute(q)


def verify_green_seed(s: Session, location_id: int, initial_order_ids: set[int]) -> list[str]:
    problems = []
    lanes = {l.slot_no: l for l in s.scalars(select(Lane).where(Lane.location_id == location_id)).all()}
    for slot, sku, cap, stock, transit in SEED_LANES:
        lane = lanes.get(slot)
        if lane is None:
            problems.append(f"货道 {slot} 丢失")
        elif (lane.sku_name, lane.capacity, lane.stock, lane.in_transit) != (sku, cap, stock, transit):
            problems.append(
                f"货道 {slot} 未恢复：({lane.sku_name},{lane.capacity},{lane.stock},{lane.in_transit})"
                f" ≠ 种子 ({sku},{cap},{stock},{transit})"
            )
    order_ids = set(s.scalars(select(RefillOrder.id).where(RefillOrder.location_id == location_id)).all())
    if order_ids != set(initial_order_ids):
        problems.append(f"补货单未恢复：现有单号 {sorted(order_ids)} ≠ 验收前 {sorted(initial_order_ids)}")
    return problems


@pytest.fixture(scope="session")
def api_client():
    with httpx.Client(base_url=API_BASE, timeout=5.0) as client:
        try:
            r = client.get("/health")
            r.raise_for_status()
        except Exception as exc:
            raise AssertionError(
                f"现网 API 不可达（{API_BASE}）：{exc}。先 docker compose up 再跑验收；"
                "只跑纯单测请用 pytest -m 'not acceptance'"
            )
        yield client


@pytest.fixture(scope="session")
def db_engine():
    engine = create_engine(settings.database_url, pool_pre_ping=True)
    try:
        with engine.connect():
            pass
    except Exception as exc:
        raise AssertionError(f"验收数据库不可达（{settings.database_url}）：{exc}")
    yield engine
    engine.dispose()


@pytest.fixture(scope="session")
def env(api_client, db_engine):
    locs = _get(api_client, "/locations")
    loc = next((l for l in locs if l["code"] == SEED_LOCATION["code"]), None)
    if loc is None:
        raise AssertionError(f"现网缺少绿仓点位 {SEED_LOCATION['code']}，无法验收")
    location_id = loc["id"]
    with Session(db_engine) as s:
        reset_lanes_to_seed(s, location_id)
        s.commit()
        initial_order_ids = set(
            s.scalars(select(RefillOrder.id).where(RefillOrder.location_id == location_id)).all()
        )
    yield SimpleNamespace(location_id=location_id, initial_order_ids=initial_order_ids)
    # 整场收尾：恢复到绿仓种子并校验，恢复不了就让整场红
    with Session(db_engine) as s:
        reset_lanes_to_seed(s, location_id)
        delete_test_orders(s, location_id, initial_order_ids)
        s.commit()
        problems = verify_green_seed(s, location_id, initial_order_ids)
    if problems:
        raise AssertionError("验收收尾未恢复到绿仓种子：\n" + "\n".join(problems))


@pytest.fixture(autouse=True)
def _restore_green_seed(env, db_engine):
    """每个场景跑完都把货道和补货单恢复到绿仓种子，失败也一样恢复。"""
    yield
    with Session(db_engine) as s:
        reset_lanes_to_seed(s, env.location_id)
        delete_test_orders(s, env.location_id, env.initial_order_ids)
        s.commit()


# ---------------------------------------------------------------- 小工具

def _get(client: httpx.Client, path: str, location_id: int | None = None) -> dict:
    params = {} if location_id is None else {"location_id": location_id}
    r = client.get(path, params=params)
    assert r.status_code == 200, f"GET {path} → {r.status_code}: {r.text}"
    return r.json()


def _post(client: httpx.Client, path: str, location_id: int) -> dict:
    r = client.post(path, params={"location_id": location_id})
    assert r.status_code == 200, f"POST {path} → {r.status_code}: {r.text}"
    return r.json()


def _line(order: dict, slot_no: str) -> dict:
    for line in order["lines"]:
        if line["slot_no"] == slot_no:
            return line
    raise AssertionError(f"补货单缺少货道 {slot_no} 的行")


def _set_lane(db_engine, location_id: int, slot_no: str, **fields) -> None:
    with Session(db_engine) as s:
        lane = s.scalar(select(Lane).where(Lane.location_id == location_id, Lane.slot_no == slot_no))
        assert lane is not None, f"货道 {slot_no} 不存在"
        for k, v in fields.items():
            setattr(lane, k, v)
        s.commit()


# ---------------------------------------------------------------- 场景

def test_seed_baseline_reconciles(env, api_client):
    """绿仓种子基线：补货生成、满仓列表、汇总页三方数字与严格口径一致。"""
    run = _post(api_client, "/refills/run", env.location_id)
    res = reconcile(api_client, env.location_id, run)
    assert res.ok, "\n".join(res.failures)
    # 点名种子里的超占道：C2 库存24+在途2 > 容量24，是超占不是满仓
    full = _get(api_client, "/refills/full", env.location_id)
    assert {r["slot_no"] for r in full["lanes"]} == {"A2", "B2"}
    summary = _get(api_client, "/refills/summary", env.location_id)
    assert summary["overbooked_count"] == 1


def test_overbooked_in_transit_scenario(env, api_client, db_engine):
    """在途过大变成超占：补量为 0，不进满仓列表，汇总超占计数 +1。"""
    _set_lane(db_engine, env.location_id, "A1", in_transit=16)  # 库存5+在途16=21 > 容量20
    run = _post(api_client, "/refills/run", env.location_id)
    line = _line(run, "A1")
    assert line["status"] == "overbooked"
    assert line["fill_qty"] == 0, "超占道补量必须为 0"
    full = _get(api_client, "/refills/full", env.location_id)
    assert "A1" not in {r["slot_no"] for r in full["lanes"]}, "超占道不得出现在满仓列表"
    res = reconcile(api_client, env.location_id, run)
    assert res.ok, "\n".join(res.failures)


def test_full_stock_scenario(env, api_client, db_engine):
    """库存已经顶满：缺口为 0 判满仓，补量 0，出现在满仓列表。"""
    _set_lane(db_engine, env.location_id, "A1", stock=20, in_transit=0)  # 顶满容量20
    run = _post(api_client, "/refills/run", env.location_id)
    line = _line(run, "A1")
    assert line["status"] == "full"
    assert line["fill_qty"] == 0
    full = _get(api_client, "/refills/full", env.location_id)
    assert "A1" in {r["slot_no"] for r in full["lanes"]}
    res = reconcile(api_client, env.location_id, run)
    assert res.ok, "\n".join(res.failures)


def test_requested_beyond_gap_is_clamped():
    """想补的数量超过缺口时被截成缺口。走现网同一套 fill_engine，不另写算法。"""
    lanes = [{"id": 1, "slot_no": "T1", "sku_name": "水", "capacity": 20, "stock": 5, "in_transit": 0}]
    (line,) = build_fill_lines(lanes, requested={1: 999})
    assert line.gap == 15 and line.fill_qty == 15, "想补 999 必须截成缺口 15"
    (line,) = build_fill_lines(lanes, requested={1: 4})
    assert line.fill_qty == 4, "想补 4 小于缺口，照给 4"
    (line,) = build_fill_lines(lanes, requested={1: -3})
    assert line.fill_qty == 0, "想补为负必须截到 0"


def test_modified_stock_recomputes_fresh(env, api_client, db_engine):
    """夹具改过库存再跑：必须按新库存出数，禁止沿用上一轮算过的补量。"""
    run1 = _post(api_client, "/refills/run", env.location_id)
    assert _line(run1, "A1")["fill_qty"] == 15  # 种子：20-5-0

    _set_lane(db_engine, env.location_id, "A1", stock=9)
    run2 = _post(api_client, "/refills/run", env.location_id)
    assert run2["id"] != run1["id"], "改完库存必须重新生成补货单"
    line = _line(run2, "A1")
    assert line["stock"] == 9
    assert line["fill_qty"] == 11, "必须按新库存出数（20-9-0=11），禁止沿用上一轮的 15"
    res = reconcile(api_client, env.location_id, run2)
    assert res.ok, "\n".join(res.failures)


def test_reconcile_without_order_fails_and_leaves_no_order(env, api_client, db_engine):
    """还没有单时对账：判失败，且不得靠现网副作用多落一张单。"""
    if env.initial_order_ids:
        pytest.skip("环境已有历史补货单，无法安全模拟“还没有单”的现场")
    with Session(db_engine) as s:
        s.execute(delete(RefillOrder).where(RefillOrder.location_id == env.location_id))
        s.commit()
    res = reconcile(api_client, env.location_id, None)
    assert not res.ok, "没有本轮生成的单就取最近一张对账，必须判失败"
    assert any("副作用" in f for f in res.failures)
    with Session(db_engine) as s:
        remaining = set(s.scalars(select(RefillOrder.id).where(RefillOrder.location_id == env.location_id)).all())
    assert remaining == set(), "对账不得靠现网副作用多落一张单"


def test_positive_fill_line_carries_no_failure():
    """正补量行不得再带一份失败原因（对账器自检）。"""
    ok_line = {"slot_no": "A1", "gap": 15, "status": "need_fill", "fill_qty": 15}
    assert evaluate_line(ok_line, 15, "need_fill", 15) == []
    dirty = {"slot_no": "A1", "gap": 0, "status": "full", "fill_qty": 5}
    reasons = evaluate_line(dirty, 0, "full", 0)
    assert any("正补量行" in r for r in reasons)
    over = {"slot_no": "A1", "gap": -1, "status": "overbooked", "fill_qty": 3}
    assert any("补量必须为 0" in r for r in evaluate_line(over, -1, "overbooked", 0))


def test_restored_to_green_seed(env, api_client, db_engine):
    """跑完要把库和满仓列表一起恢复到绿仓种子（本用例必须排在最后）。"""
    with Session(db_engine) as s:
        problems = verify_green_seed(s, env.location_id, env.initial_order_ids)
    assert problems == [], "\n".join(problems)
    # 接口层面再验一次：按恢复后的库存现跑一张单，满仓列表必须回到种子满仓集合
    run = _post(api_client, "/refills/run", env.location_id)
    full = _get(api_client, "/refills/full", env.location_id)
    assert {r["slot_no"] for r in full["lanes"]} == {
        slot for slot, (_, st, _) in seed_exp().items() if st == "full"
    }
    summary = _get(api_client, "/refills/summary", env.location_id)
    assert summary == {"location_id": env.location_id, **expected_totals(seed_exp())}
    # 这张验收单同样是测试产物，当场删掉：验收无论成败都不得多留下一张成功补货单
    with Session(db_engine) as s:
        s.execute(delete(RefillOrder).where(RefillOrder.id == run["id"]))
        s.commit()
        remaining = set(s.scalars(select(RefillOrder.id).where(RefillOrder.location_id == env.location_id)).all())
    assert remaining == env.initial_order_ids
