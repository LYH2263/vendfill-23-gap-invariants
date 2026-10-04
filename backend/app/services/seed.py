from datetime import datetime, timedelta
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from app.models.models import Lane, Location, Sale

# 绿仓种子：验收夹具据此恢复现场，改动这里要同步核对验收基线。
SEED_LOCATION = {"code": "VM-01", "name": "地铁口 A 点位", "address": "城东地铁 1 号口"}
SEED_LANES = [
    # (slot_no, sku_name, capacity, stock, in_transit)
    ("A1", "矿泉水", 20, 5, 0),
    ("A2", "可乐", 18, 18, 0),
    ("B1", "薯片", 12, 3, 2),
    ("B2", "巧克力", 15, 10, 5),
    ("C1", "能量棒", 10, 0, 0),
    ("C2", "口香糖", 24, 24, 2),
]

def seed_if_empty(db: Session) -> None:
    if (db.scalar(select(func.count()).select_from(Location)) or 0) > 0:
        return
    loc = Location(**SEED_LOCATION)
    db.add(loc); db.flush()
    lane_ids = []
    for slot, sku, cap, stock, transit in SEED_LANES:
        lane = Lane(location_id=loc.id, slot_no=slot, sku_name=sku, capacity=cap, stock=stock, in_transit=transit)
        db.add(lane); db.flush()
        lane_ids.append(lane.id)
    now = datetime(2026, 9, 16, 12, 0, 0)
    for i, lid in enumerate(lane_ids):
        db.add(Sale(lane_id=lid, qty=2 + i, sold_at=now - timedelta(hours=i)))
    db.commit()
