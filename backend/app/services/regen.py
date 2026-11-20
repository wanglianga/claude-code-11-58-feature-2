"""回馈去向推导:把"车载表上的制动回馈"还原为"系统实际吸收/耗散/未知"的去向。

两条路线回答同一问题——某回馈量在指定运行范围内去了哪里、有多少可计入线路节电:

- 路线A 守恒分配(CONSERVATION):在同一供电分区、同一时间窗内用能量守恒分配回馈。
  分区边界计量(受电/返送)完整时,才能确认"未上网即被分区内吸收或耗散";
  结合邻车牵引需求与吸收装置计量/状态,区分:
    已确认邻车吸收 / 已确认装置吸收 / 可能吸收 / 受限耗散 / 资料不足。
  物理清晰、无需训练;多车密集时因果归因不明,未定份额较宽。
- 路线B 学习预测(LEARNED):用"计量完整时期"的守恒结论作为训练标签,
  学习 吸收比例 ~ 负荷、车距、坡度、装置状态 的条件关系,对缺表运行给出预测区间。
  日常估计更细,但特殊调图/罕见故障下可能失配(以区间与外推警告表达)。

不可越界的约束:
- 邻车牵引需求高 ≠ 吸收关系成立:确认需分区边界计量闭合且因果单一。
- 不同供电分区不得跨界抵销:每个分区独立守恒,汇总只是求和。
- 装置状态晚到只产生新的去向版本,不追溯改写历史结论。
- 回馈累计值重启不得生成负耗散:所有去向量夹紧为非负。
"""
from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy.orm import Session

from ..config import (DEVICE_METER_COVERAGE, MIN_TRAIN_SAMPLES, PREDICTION_Z,
                      REGEN_BOUNDARY_COVERAGE)
from ..models import (AbsorptionDevice, AbsorptionStatus, Device, EnergyFragment,
                      MeterReading, RegenDerivation, Run, RunSegment, Section)
from .energy import band_add, band_sub, band_sum, diff_counter

ZERO = {"v": 0.0, "lo": 0.0, "hi": 0.0}
UNKNOWN = {"v": None, "lo": None, "hi": None}

# 五类去向(资料不足=unknown_source),外加"返送电网"这一物理必需项。
DEST_KEYS = ("neighbor_confirmed", "device_confirmed", "possible_absorption",
             "limited_dissipation", "unknown_source", "exported_to_grid")
DEST_LABEL = {
    "neighbor_confirmed": "已确认邻车吸收",
    "device_confirmed": "已确认储能/回馈装置吸收",
    "possible_absorption": "可能吸收(未能确认)",
    "limited_dissipation": "受限耗散(制动电阻)",
    "unknown_source": "资料不足",
    "exported_to_grid": "已返送电网",
}
# 节能指标资格
ELIGIBILITY = {
    "neighbor_confirmed": {
        "qualified": "YES",
        "statement": "同一供电分区内已由边界计量确认回馈替代邻车牵引取电,计入线路节电(已确认)。",
    },
    "device_confirmed": {
        "qualified": "YES",
        "statement": "同一供电分区内储能/回馈装置已计量吸收,回馈留在线路内可再利用,计入线路节电(已确认)。",
    },
    "possible_absorption": {
        "qualified": "CONDITIONAL",
        "statement": "存在吸收可能但未被确认(边界计量不完整、多车归因不明或装置状态未知),"
                     "只能以区间计入潜在节电,不得作为已确认节电对外宣称。",
    },
    "limited_dissipation": {
        "qualified": "NO",
        "statement": "回馈被制动电阻耗散,未上网、未被利用,不计入任何线路节电。",
    },
    "unknown_source": {
        "qualified": "UNDETERMINED",
        "statement": "资料不足以判定回馈去向,不得计入节电,也不得按 0 处理。",
    },
    "exported_to_grid": {
        "qualified": "GRID_ONLY",
        "statement": "回馈已返送电网,是否计入节电取决于上网结算口径,不计入本线牵引替代。",
    },
}


# ---------------- 区间工具 ----------------

def _clamp_nonneg(b: dict) -> dict:
    if b is None or b.get("v") is None:
        return dict(UNKNOWN)
    return {"v": round(max(0.0, b["v"]), 3),
            "lo": round(max(0.0, b["lo"]), 3),
            "hi": round(max(0.0, b["hi"]), 3)}


def _touch(b: dict) -> bool:
    return b is not None and b.get("v") is not None


def _make_band(v: float, r_band: dict, r_total: float = 0.0) -> dict:
    """由点值 v 构造区间:仅按该去向在产生回馈总量中的占比分摊回馈的不确定宽度。

    回馈累计值重启/失连会让 r_band 变成宽区间,按占比把这份不确定性传下去;
    不额外引入边界/装置表的宽度——那些作为"分类置信度"(complete/known)已体现在去向归属上。
    """
    v = max(0.0, v)
    if not _touch(r_band):
        return dict(UNKNOWN)
    r_width = max(0.0, (r_band["hi"] or 0.0) - (r_band["lo"] or 0.0))
    share = (v / r_total) if r_total > 0 else 0.0
    width = r_width * share
    return {"v": round(v, 3), "lo": round(max(0.0, v - width), 3),
            "hi": round(v + width, 3)}


def _readings(db: Session, device_id: int, start: datetime, end: datetime):
    return (db.query(MeterReading)
            .filter(MeterReading.device_id == device_id,
                    MeterReading.sampled_at >= start,
                    MeterReading.sampled_at <= end)
            .order_by(MeterReading.sampled_at, MeterReading.seq).all())


def _meter_band(db: Session, device_id: int, field: str, start, end,
                max_rate: float = 40.0) -> tuple[dict, float]:
    rows = _readings(db, device_id, start, end)
    res = diff_counter(rows, field, start, end, max_rate)
    return _clamp_nonneg(res.band()), res.coverage


# ---------------- 运行区间归集 ----------------

def _zone_windows(db: Session, run: Run) -> dict[str, dict]:
    """按供电分区归集本次运行的片段窗口与回馈量(分区间不合并、不抵销)。"""
    segments = (db.query(RunSegment).filter(RunSegment.run_id == run.id)
                .order_by(RunSegment.seq).all())
    out: dict[str, dict] = {}
    for seg in segments:
        section = db.get(Section, seg.section_id)
        if not section:
            continue
        zone = section.supply_zone or section.code
        frags = (db.query(EnergyFragment)
                 .filter(EnergyFragment.run_segment_id == seg.id,
                         EnergyFragment.source == "ONBOARD").all())
        regen = band_sum([_clamp_nonneg(f.quantities.get("regen", UNKNOWN))
                          for f in frags]) if frags else dict(UNKNOWN)
        z = out.setdefault(zone, {"start": seg.start_ts, "end": seg.end_ts,
                                  "regen_bands": [], "section": section,
                                  "sections": set()})
        z["start"] = min(z["start"], seg.start_ts)
        z["end"] = max(z["end"], seg.end_ts)
        z["regen_bands"].append(regen)
        z["sections"].add(section.id)
    for z in out.values():
        z["regen"] = band_sum(z["regen_bands"]) if z["regen_bands"] else dict(UNKNOWN)
        z["gradient"] = _zone_gradient(db, z["sections"])
    return out


def _zone_gradient(db: Session, section_ids: set[int]) -> float:
    secs = [db.get(Section, sid) for sid in section_ids]
    secs = [s for s in secs if s]
    if not secs:
        return 0.0
    tot = sum(s.length_km for s in secs) or 1.0
    return round(sum((s.gradient_permille or 0.0) * s.length_km for s in secs) / tot, 3)


def _zone_boundary_device(db: Session, zone_sections: set[int]) -> Device | None:
    for sid in zone_sections:
        sec = db.get(Section, sid)
        if sec and sec.substation_device_id:
            return db.get(Device, sec.substation_device_id)
    return None


def _neighbor_demand(db: Session, run: Run, zone_sections: set[int],
                     start, end) -> tuple[dict, int, bool, bool]:
    """同分区同时段其它运行的牵引需求(供参考,不自动等于吸收)。

    返回 (牵引需求区间, 并发邻车数量, 是否存在邻车牵引未知, 邻车是否也在回馈)。
    邻车需求高并不自动证明吸收关系:若邻车同时也在回馈,互为来源/汇,归因不明。
    """
    segs = (db.query(RunSegment)
            .filter(RunSegment.section_id.in_(zone_sections),
                    RunSegment.start_ts <= end, RunSegment.end_ts >= start,
                    RunSegment.run_id != run.id).all())
    # 每个邻车取最新版本,避免新旧版本重复计入
    latest_ids: dict[str, Run] = {}
    bands, unknown = [], False
    runs = set()
    mutual_regen = False
    for seg in segs:
        other = db.get(Run, seg.run_id)
        if not other:
            continue
        latest = (db.query(Run).filter(Run.run_key == other.run_key)
                  .order_by(Run.version.desc()).first())
        if not latest or latest.id != other.id:
            continue
        if other.started_at > end or other.ended_at < start:
            continue
        overlap = (min(end, seg.end_ts) - max(start, seg.start_ts)).total_seconds()
        if overlap <= 0:
            continue
        runs.add(other.id)
        frags = (db.query(EnergyFragment)
                 .filter(EnergyFragment.run_segment_id == seg.id,
                         EnergyFragment.source == "ONBOARD").all())
        ratio = overlap / max((seg.end_ts - seg.start_ts).total_seconds(), 1e-9)
        for f in frags:
            t = f.quantities.get("traction", UNKNOWN)
            rg = f.quantities.get("regen", UNKNOWN)
            if _touch(rg) and (rg["v"] or 0.0) > 1e-9:
                mutual_regen = True
            if not _touch(t):
                unknown = True
                continue
            bands.append({"v": t["v"] * ratio, "lo": t["lo"] * ratio,
                          "hi": t["hi"] * ratio})
    demand = band_sum(bands) if bands else dict(ZERO)
    if unknown:
        demand = dict(UNKNOWN)
    return _clamp_nonneg(demand), len(runs), unknown, mutual_regen


def _zone_devices(db: Session, zone: str) -> list[AbsorptionDevice]:
    return (db.query(AbsorptionDevice)
            .filter(AbsorptionDevice.supply_zone == zone).all())


def _device_status(db: Session, dev: AbsorptionDevice, window_end: datetime,
                   cutoff: datetime) -> AbsorptionStatus | None:
    """取 cutoff 之前已到达、且在窗口结束前已观测到的最新状态。"""
    return (db.query(AbsorptionStatus)
            .filter(AbsorptionStatus.device_id == dev.id,
                    AbsorptionStatus.observed_at <= window_end,
                    AbsorptionStatus.received_at <= cutoff)
            .order_by(AbsorptionStatus.observed_at.desc(),
                      AbsorptionStatus.received_at.desc()).first())


def _device_absorption(db: Session, dev: AbsorptionDevice, start, end,
                       ) -> tuple[dict, float]:
    if not dev.meter_device_id:
        return dict(UNKNOWN), 0.0
    return _meter_band(db, dev.meter_device_id, "absorb_cum", start, end,
                       max_rate=40.0)


# ---------------- 路线A:分区守恒分配 ----------------

def _allocate_zone(zone: str, section_ids: set[int], regen: dict,
                   boundary_complete: bool, export: dict,
                   neighbor: dict, neighbor_count: int, mutual_regen: bool,
                   device_confirmed_band: dict, device_capacity: dict,
                   device_present: bool, device_state_known: bool,
                   device_state: str | None, device_meter_ok: bool,
                   device_has_meter: bool, reasons: list) -> dict:
    """在一个供电分区、一个时间窗内分配回馈(点值守恒,按占比给区间)。

    顺序:返送电网 -> 装置已计量吸收 -> 邻车吸收(需因果唯一) -> 可能吸收 -> 耗散/未知。
    """
    R = max(0.0, regen["v"]) if _touch(regen) else 0.0
    res = {k: 0.0 for k in DEST_KEYS}
    remaining = R
    N = max(0.0, neighbor["v"]) if _touch(neighbor) else 0.0
    can_absorb = device_present and (not device_state_known
                                     or device_state in ("AVAILABLE", "LIMITED"))
    cannot_absorb = device_state_known and device_state in ("FAULT", "OFFLINE")

    # 1) 已返送电网(仅边界计量闭合时可确认;返送量不得跨区抵销)
    if boundary_complete and _touch(export):
        exported = min(remaining, max(0.0, export["v"]))
        res["exported_to_grid"] = exported
        remaining -= exported
        if exported > 0:
            reasons.append(f"分区{zone}:边界返送 {exported:.2f} kWh 离开分区,"
                           "返送量不得用于抵扣其它分区")
    elif not boundary_complete:
        reasons.append(f"分区{zone}:边界计量不闭合,无法确认回馈是否返送电网")

    # 2) 已确认装置吸收:装置自有计量是直接证据,分区边界缺失不推翻它
    if (device_meter_ok and device_present and not cannot_absorb
            and _touch(device_confirmed_band)):
        absorbed = min(remaining, max(0.0, device_confirmed_band["v"]))
        res["device_confirmed"] = absorbed
        remaining -= absorbed
        reasons.append(f"分区{zone}:储能/回馈装置自有计量确认吸收 {absorbed:.2f} kWh")
    elif cannot_absorb:
        reasons.append(f"分区{zone}:吸收装置状态 {device_state},不能吸收回馈")
    elif device_present and not device_state_known:
        reasons.append(f"分区{zone}:吸收装置状态未知(可能晚到),装置吸收无法确认")

    # 3) 剩余回馈归因
    if boundary_complete:
        cap = min(remaining, N)
        if neighbor_count == 1 and cap > 0 and not mutual_regen:
            res["neighbor_confirmed"] = cap
            remaining -= cap
            reasons.append(f"分区{zone}:单一邻车牵引需求且边界闭合,确认回馈被邻车吸收")
        elif neighbor_count > 0 and cap > 0:
            res["possible_absorption"] = cap
            remaining -= cap
            if mutual_regen:
                reasons.append(f"分区{zone}:邻车同窗也在回馈,互为来源/汇,"
                               "需求高不证明吸收关系,只能列为可能吸收")
            else:
                reasons.append(f"分区{zone}:{neighbor_count} 辆邻车并发,回馈去向在分区内"
                               "守恒但归因不明,只能列为可能吸收(未定份额较宽)")
                # 装置可吸收但未完整计量:按容量列为可能
        if can_absorb and not device_meter_ok and remaining > 0:
            add = min(remaining, max(0.0, device_capacity["v"]))
            res["possible_absorption"] += add
            remaining -= add
            reasons.append(f"分区{zone}:装置可用但未完整计量,{add:.2f} kWh 只能列为可能吸收")
        if not device_state_known and device_present and remaining > 0:
            # 边界闭合但装置状态未知:无法区分装置吸收与电阻耗散,只能列为可能
            res["possible_absorption"] += remaining
            reasons.append(f"分区{zone}:装置状态未知,{remaining:.2f} kWh 可能被装置吸收,"
                           "也可能电阻耗散,不能确认")
            remaining = 0.0
        if remaining > 1e-9:
            res["limited_dissipation"] = remaining
            reasons.append(f"分区{zone}:边界闭合且无可吸收对象,{remaining:.2f} kWh 受限耗散")
        remaining = 0.0
    else:
        # 边界不闭合:邻车/装置吸收都无法与"是否返送"区分,至多列为可能
        cap = min(remaining, N)
        if cap > 0:
            res["possible_absorption"] = cap
            remaining -= cap
            reasons.append(f"分区{zone}:边界不闭合,邻车需求 {cap:.2f} kWh 只能列为可能吸收")
        if can_absorb and not device_has_meter and remaining > 0:
            # 完全没有装置计量时才按容量给"可能吸收";有表但缺测则宁可留作资料不足
            add = min(remaining, max(0.0, device_capacity["v"]))
            res["possible_absorption"] += add
            remaining -= add
            reasons.append(f"分区{zone}:边界不闭合,装置可吸收 {add:.2f} kWh 只能列为可能吸收")
        if remaining > 1e-9:
            res["unknown_source"] = remaining
            reasons.append(f"分区{zone}:边界/装置资料不足,{remaining:.2f} kWh 去向不能判定")
        remaining = 0.0

    # 所有去向夹紧非负,防止累计值重启/区间下溢产生负耗散
    for k in DEST_KEYS:
        res[k] = max(0.0, res[k])
    return res


def derive_conservation(db: Session, run: Run, cutoff: datetime | None = None,
                        ) -> tuple[dict, dict, list[str], dict]:
    """路线A:按供电分区逐区守恒分配,返回(去向区间, 分区明细, 警告, 依据)。"""
    cutoff = cutoff or datetime.utcnow()
    zones = _zone_windows(db, run)
    dest_bands = {k: [] for k in DEST_KEYS}
    allocations, warnings, reasons = [], [], []
    total_regen = dict(ZERO)
    regen_unknown = False

    for zone, z in zones.items():
        regen = z["regen"]
        if _touch(regen):
            total_regen = band_add(total_regen, regen)
        else:
            regen_unknown = True
        dev = _zone_boundary_device(db, z["sections"])
        if dev:
            imp, cov_i = _meter_band(db, dev.id, "import_cum", z["start"], z["end"])
            exp, cov_e = _meter_band(db, dev.id, "export_cum", z["start"], z["end"])
            boundary_complete = (cov_i >= REGEN_BOUNDARY_COVERAGE
                                 and cov_e >= REGEN_BOUNDARY_COVERAGE
                                 and _touch(exp))
        else:
            imp, exp = dict(UNKNOWN), dict(UNKNOWN)
            cov_i = cov_e = 0.0
            boundary_complete = False
            warnings.append({"code": "NO_BOUNDARY_METER",
                             "detail": f"分区{zone}缺少分区边界计量"})

        neighbor, n_count, n_unknown, mutual = _neighbor_demand(
            db, run, z["sections"], z["start"], z["end"])
        if n_unknown:
            warnings.append({"code": "NEIGHBOR_DEMAND_UNKNOWN",
                             "detail": f"分区{zone}存在邻车牵引未知,邻车吸收无法确认"})

        devices = _zone_devices(db, zone)
        dev_absorb_bands, dev_cap_bands = [], []
        state_known = True
        states = []
        meter_ok = False
        has_meter = False
        for d in devices:
            if d.meter_device_id:
                has_meter = True
            st = _device_status(db, d, z["end"], cutoff)
            if st is None:
                state_known = False
                states.append(None)
                # 状态未知:不授予计量确认,但按默认功率估可吸收上限
                minutes = (z["end"] - z["start"]).total_seconds() / 60.0
                dev_cap_bands.append({"v": 5.0 * minutes, "lo": 0.0, "hi": 5.0 * minutes})
            else:
                states.append(st.state)
                if st.state in ("AVAILABLE", "LIMITED"):
                    minutes = (z["end"] - z["start"]).total_seconds() / 60.0
                    rate = st.max_absorb_kwh_per_min or 5.0
                    dev_cap_bands.append({"v": rate * minutes, "lo": 0.0,
                                          "hi": rate * minutes})
            ab, cov = _device_absorption(db, d, z["start"], z["end"])
            if cov >= DEVICE_METER_COVERAGE and _touch(ab):
                meter_ok = True
            dev_absorb_bands.append(ab if _touch(ab) else dict(ZERO))

        device_state = None
        if devices and state_known:
            # 任一台故障即认为该分区装置在该窗口不可全量吸收
            device_state = ("FAULT" if any(s in ("FAULT", "OFFLINE") for s in states)
                            else "LIMITED" if "LIMITED" in states else "AVAILABLE")
        # 分区内没有吸收装置时,装置侧约束是"明确没有",不算资料不足
        device_state_known = (len(devices) == 0) or state_known
        dev_absorb = band_sum(dev_absorb_bands) if dev_absorb_bands else dict(ZERO)
        dev_cap = band_sum(dev_cap_bands) if dev_cap_bands else dict(ZERO)

        alloc = _allocate_zone(zone, z["sections"], regen, boundary_complete, exp,
                               neighbor, n_count, mutual, dev_absorb, dev_cap,
                               bool(devices), device_state_known, device_state,
                               bool(devices) and meter_ok, has_meter, reasons)
        R = max(0.0, regen["v"]) if _touch(regen) else 0.0
        alloc_bands = {k: _make_band(v, regen, R) for k, v in alloc.items()}
        for k, v in alloc_bands.items():
            dest_bands[k].append(v)

        allocations.append({
            "zone": zone, "range_start": z["start"].isoformat(),
            "range_end": z["end"].isoformat(), "sections": sorted(z["sections"]),
            "regen_available": _clamp_nonneg(regen),
            "boundary": {"device_id": dev.id if dev else None,
                         "complete": boundary_complete,
                         "import_coverage": round(cov_i, 4),
                         "export_coverage": round(cov_e, 4),
                         "export": _clamp_nonneg(exp)},
            "neighbor": {"demand": _clamp_nonneg(neighbor), "count": n_count,
                         "unknown": n_unknown},
            "device": {"count": len(devices),
                       "state_known": device_state_known,
                       "state": device_state, "meter_ok": bool(devices) and meter_ok,
                       "has_meter": has_meter,
                       "absorbed": _clamp_nonneg(dev_absorb),
                       "capacity": _clamp_nonneg(dev_cap)},
            "_gradient": z["gradient"],
            "destinations": alloc_bands,
        })

    destinations = {k: band_sum(v) if v else dict(ZERO) for k, v in dest_bands.items()}
    destinations = {k: _clamp_nonneg(b) for k, b in destinations.items()}
    if regen_unknown:
        total_regen = dict(UNKNOWN)
    basis = {"route": "CONSERVATION", "cutoff": cutoff.isoformat(),
             "boundary_threshold": REGEN_BOUNDARY_COVERAGE,
             "device_meter_threshold": DEVICE_METER_COVERAGE, "reasons": reasons}
    return destinations, allocations, warnings, basis, total_regen


# ---------------- 路线B:条件关系学习 ----------------

def _features_from_alloc(alloc: dict, zone_window_minutes: float) -> list[float]:
    demand = alloc["neighbor"]["demand"]["v"] or 0.0
    load_per_min = demand / max(zone_window_minutes, 1e-9)
    # 车距:并发邻车越少,平均车距越大(以分钟近似)
    count = max(1, alloc["neighbor"]["count"])
    headway = zone_window_minutes / count
    dev = alloc["device"]
    dev_count = dev.get("count", 1)
    if dev_count == 0:
        avail = 0.0          # 分区内没有吸收装置:装置可用度 0
    elif not dev["state_known"]:
        avail = 0.5          # 状态缺失:按一半可用度处理,并在结果中提示
    elif dev["state"] in ("FAULT", "OFFLINE"):
        avail = 0.0
    elif dev["state"] == "LIMITED":
        avail = 0.5
    else:
        avail = 1.0
    return [load_per_min, headway, alloc.get("_gradient", 0.0), avail]


def _absorbed_fraction(alloc: dict) -> float | None:
    d = alloc["destinations"]
    r = alloc["regen_available"]["v"]
    if not r or r <= 0:
        return None
    absorbed = (d["neighbor_confirmed"]["v"] + d["device_confirmed"]["v"]
                + d["possible_absorption"]["v"])
    return max(0.0, min(1.0, absorbed / r))


def _ols(X: list[list[float]], y: list[float]):
    """带截距的最小二乘(纯 Python 高斯消元),返回系数与残差标准差。"""
    n = len(X)
    p = len(X[0]) + 1
    A = [[1.0] + row[:] for row in X]
    # 正规方程 A^T A beta = A^T y
    ata = [[sum(A[k][i] * A[k][j] for k in range(n)) for j in range(p)] for i in range(p)]
    aty = [sum(A[k][i] * y[k] for k in range(n)) for i in range(p)]
    # 高斯消元
    M = [ata[i][:] + [aty[i]] for i in range(p)]
    for col in range(p):
        piv = max(range(col, p), key=lambda r: abs(M[r][col]))
        if abs(M[piv][col]) < 1e-12:
            M[piv][col] = 1e-12
        M[col], M[piv] = M[piv], M[col]
        pv = M[col][col]
        M[col] = [x / pv for x in M[col]]
        for r in range(p):
            if r != col and abs(M[r][col]) > 1e-15:
                f = M[r][col]
                M[r] = [a - f * b for a, b in zip(M[r], M[col])]
    beta = [M[i][p] for i in range(p)]
    preds = [beta[0] + sum(beta[j + 1] * X[k][j] for j in range(p - 1)) for k in range(n)]
    resid = [y[k] - preds[k] for k in range(n)]
    dof = max(1, n - p)
    sigma = math.sqrt(sum(r * r for r in resid) / dof)
    ss_tot = sum((yk - sum(y) / n) ** 2 for yk in y) or 1e-12
    ss_res = sum(r * r for r in resid)
    r2 = 1 - ss_res / ss_tot
    return beta, sigma, r2, n


def derive_learned(db: Session, run: Run, cutoff: datetime | None = None,
                   ) -> tuple[dict, dict, list[str], dict, dict]:
    """路线B:用完整计量时期学习吸收比例,对目标运行给预测区间。"""
    cutoff = cutoff or datetime.utcnow()
    # 1) 训练集:其它运行中边界计量闭合的分区窗口,以路线A守恒结论为标签
    train_X, train_y, train_refs = [], [], []
    seen = set()
    for other in db.query(Run).filter(Run.id != run.id).all():
        latest = (db.query(Run).filter(Run.run_key == other.run_key)
                  .order_by(Run.version.desc()).first())
        if not latest or latest.id != other.id or latest.id in seen:
            continue
        seen.add(latest.id)
        zones = _zone_windows(db, latest)
        for zone, z in zones.items():
            minutes = (z["end"] - z["start"]).total_seconds() / 60.0
            dest, allocs, _, _, _ = derive_conservation(db, latest, cutoff=cutoff)
            for alloc in allocs:
                if alloc["zone"] != zone:
                    continue
                if not alloc["boundary"]["complete"] or not alloc["device"]["state_known"]:
                    continue
                yv = _absorbed_fraction(alloc)
                if yv is None:
                    continue
                alloc["_gradient"] = z["gradient"]
                train_X.append(_features_from_alloc(alloc, minutes))
                train_y.append(yv)
                train_refs.append({"run_key": latest.run_key, "zone": zone})

    zones = _zone_windows(db, run)
    warnings = []
    total_regen = dict(ZERO)
    for z in zones.values():
        if _touch(z["regen"]):
            total_regen = band_add(total_regen, z["regen"])

    target_X, target_meta = [], []
    for zone, z in zones.items():
        minutes = (z["end"] - z["start"]).total_seconds() / 60.0
        neighbor, n_count, n_unknown, mutual = _neighbor_demand(
            db, run, z["sections"], z["start"], z["end"])
        device_known = False
        states = []
        devs = _zone_devices(db, zone)
        for d in devs:
            st = _device_status(db, d, z["end"], cutoff)
            if st is not None:
                device_known = True
                states.append(st.state)
        dev_state = ("FAULT" if any(s in ("FAULT", "OFFLINE") for s in states)
                     else "LIMITED" if "LIMITED" in states else "AVAILABLE") \
            if states else (None if devs else "NONE")
        pseudo = {"neighbor": {"demand": neighbor, "count": n_count},
                  "device": {"state_known": device_known or not devs,
                             "state": dev_state, "count": len(devs)},
                  "_gradient": z["gradient"]}
        target_X.append(_features_from_alloc(pseudo, minutes))
        target_meta.append({"zone": zone, "minutes": minutes, "neighbor": neighbor,
                            "count": n_count, "device_state": dev_state,
                            "regen": _clamp_nonneg(z["regen"])})

    dest = {k: dict(ZERO) for k in DEST_KEYS}
    basis = {"route": "LEARNED", "cutoff": cutoff.isoformat()}
    if len(train_X) < MIN_TRAIN_SAMPLES:
        # 训练样本不足:不能编造预测,全部归入资料不足
        dest["unknown_source"] = _clamp_nonneg(total_regen)
        basis.update({"model": None, "train_samples": len(train_X),
                      "min_samples": MIN_TRAIN_SAMPLES,
                      "note": "完整计量历史不足,无法学习条件关系"})
        warnings.append({"code": "INSUFFICIENT_TRAINING",
                         "detail": f"完整计量窗口仅 {len(train_X)} 个,"
                                   f"少于 {MIN_TRAIN_SAMPLES},不给预测"})
        allocations = [{"zone": m["zone"], "predicted_fraction": None,
                        "regen_available": m["regen"]} for m in target_meta]
        return dest, allocations, warnings, basis, {"ready": False}

    beta, sigma, r2, n_train = _ols(train_X, train_y)
    # 训练特征范围,用于外推警告
    mins = [min(row[j] for row in train_X) for j in range(len(train_X[0]))]
    maxs = [max(row[j] for row in train_X) for j in range(len(train_X[0]))]

    allocations, reasons = [], []
    for meta, x in zip(target_meta, target_X):
        R = max(0.0, meta["regen"]["v"]) if _touch(meta["regen"]) else 0.0
        frac = beta[0] + sum(beta[j + 1] * x[j] for j in range(len(x)))
        pi = PREDICTION_Z * sigma * math.sqrt(1 + 1.0 / n_train)
        frac_c = max(0.0, min(1.0, frac))
        lo_f, hi_f = max(0.0, frac_c - pi), min(1.0, frac_c + pi)
        for j, val in enumerate(x):
            if val < mins[j] - 1e-9 or val > maxs[j] + 1e-9:
                warnings.append({"code": "EXTRAPOLATION",
                                 "detail": f"分区{meta['zone']} 特征{['负荷','车距','坡度','装置可用'][j]}="
                                           f"{val:.3g} 超出训练范围 [{mins[j]:.3g},{maxs[j]:.3g}],"
                                           "预测在特殊调图/罕见故障下可能失配"})
        if meta["device_state"] is None:
            warnings.append({"code": "DEVICE_STATE_UNKNOWN",
                             "detail": f"分区{meta['zone']} 装置状态缺失,预测仅按历史条件外推"})
        # 路线B为预测,不能宣称"已确认":吸收部分入可能吸收,其余入受限耗散。
        # 能量区间同时体现回馈量自身宽度与比例预测区间(能量 = 比例 × 回馈量)。
        r_lo = meta["regen"]["lo"] if meta["regen"]["lo"] is not None else R
        r_hi = meta["regen"]["hi"] if meta["regen"]["hi"] is not None else R
        lo_abs = min(R, max(0.0, lo_f * r_lo))
        hi_abs = min(R, max(lo_abs, hi_f * r_hi))
        w_lo = max(0.0, R - hi_abs)
        w_hi = min(R, max(w_lo, R - lo_abs))
        dest["possible_absorption"] = band_add(
            dest["possible_absorption"],
            {"v": round(frac_c * R, 3), "lo": round(lo_abs, 3), "hi": round(hi_abs, 3)})
        dest["limited_dissipation"] = band_add(
            dest["limited_dissipation"],
            {"v": round((1 - frac_c) * R, 3),
             "lo": round(w_lo, 3), "hi": round(w_hi, 3)})
        allocations.append({
            "zone": meta["zone"], "predicted_fraction": round(frac_c, 4),
            "prediction_interval": [round(lo_f, 4), round(hi_f, 4)],
            "regen_available": meta["regen"],
            "features": {"load_per_min": round(x[0], 4), "headway_min": round(x[1], 2),
                         "gradient_permille": round(x[2], 3),
                         "device_available": x[3]},
            "neighbor": {"demand": _clamp_nonneg(meta["neighbor"]),
                         "count": meta["count"], "unknown": False},
            "device_state": meta["device_state"],
        })
        reasons.append(f"分区{meta['zone']}:预测吸收比例 {frac_c:.2%} "
                       f"(区间 {lo_f:.2%}~{hi_f:.2%})")
    dest = {k: _clamp_nonneg(b) for k, b in dest.items()}
    basis.update({
        "model": "OLS(吸收比例 ~ 负荷/车距/坡度/装置可用)",
        "coefficients": {"intercept": round(beta[0], 4),
                         **{name: round(beta[j + 1], 4) for j, name in enumerate(
                             ["load_per_min", "headway_min", "gradient_permille",
                              "device_available"])}},
        "train_samples": n_train, "residual_std": round(sigma, 4),
        "r2": round(r2, 4), "prediction_z": PREDICTION_Z,
        "training_range": {"mins": [round(m, 4) for m in mins],
                           "maxs": [round(m, 4) for m in maxs]},
        "reasons": reasons,
        "note": "路线B结果为条件预测,不是直接计量确认;缺表或特殊调图时应以区间与警告为准",
    })
    return dest, allocations, warnings, basis, {"ready": True}


# ---------------- 汇总、资格与持久化 ----------------

def _band_summary(dest: dict, regen: dict) -> dict:
    def add(*keys):
        out = dict(ZERO)
        for k in keys:
            out = band_add(out, dest[k])
        return _clamp_nonneg(out)

    absorbed_confirmed = add("neighbor_confirmed", "device_confirmed")
    absorbed_possible = add("possible_absorption")
    unutilized = add("limited_dissipation", "unknown_source")
    returned_to_grid = add("exported_to_grid")
    return {
        "regen_available": _clamp_nonneg(regen),
        "absorbed_confirmed": absorbed_confirmed,
        "absorbed_possible": absorbed_possible,
        "unutilized": unutilized,
        "exported_to_grid": returned_to_grid,
        "eligible_confirmed": absorbed_confirmed,
        "eligible_max": add("neighbor_confirmed", "device_confirmed",
                            "possible_absorption"),
    }


def _eligibility(dest: dict) -> dict:
    out = {}
    for k in DEST_KEYS:
        info = dict(ELIGIBILITY[k])
        info["kwh"] = dest[k]
        out[k] = info
    return out


def _check_conservation(dest: dict, regen: dict) -> list[str]:
    issues = []
    if not _touch(regen):
        return issues  # 回馈量未知时不做守恒断言(去向也只能是未知)
    total = sum(dest[k]["v"] for k in DEST_KEYS if dest[k]["v"] is not None)
    if abs(total - regen["v"]) > 0.01:
        issues.append(f"去向之和 {total:.3f} 与回馈产生量 {regen['v']:.3f} 不守恒")
    for k in DEST_KEYS:
        if dest[k]["v"] is not None and (dest[k]["v"] < -1e-9
                                         or dest[k]["lo"] < -1e-9):
            issues.append(f"{DEST_LABEL[k]} 出现负值(可能源于累计值重启求差)")
    return issues


def derive(db: Session, run: Run, route: str = "CONSERVATION",
           cutoff: datetime | None = None) -> RegenDerivation:
    """推导并落库。同一 (run, route) 再次推导生成 version+1,旧版本保留。"""
    cutoff = cutoff or datetime.utcnow()
    if route == "LEARNED":
        dest, allocations, warnings, basis, info = derive_learned(db, run, cutoff)
        bands = [a["regen_available"] for a in allocations]
        regen = band_sum(bands) if bands else dict(ZERO)
        status = "READY" if info.get("ready") else "INSUFFICIENT"
    else:
        dest, allocations, warnings, basis, regen = derive_conservation(db, run, cutoff)
        status = "READY"
        if any(not a["boundary"]["complete"] for a in allocations):
            status = "PARTIAL"

    dest = {k: _clamp_nonneg(v) for k, v in dest.items()}
    issues = _check_conservation(dest, regen)
    for msg in issues:
        warnings.append({"code": "CONSERVATION_VIOLATION", "detail": msg})
    if not _touch(regen):
        status = "INSUFFICIENT"
        warnings.append({"code": "REGEN_UNKNOWN",
                         "detail": "车载回馈累计值不闭合,产生回馈量为未知"})
    elif regen.get("v") == 0 and status == "READY":
        # 本次运行没有产生回馈:去向分类为空是正确结果,不是资料不足
        status = "READY"
        warnings.append({"code": "NO_REGEN",
                         "detail": "本次运行车载回馈为 0,无需分配去向"})

    summary = _band_summary(dest, regen)
    prev = (db.query(RegenDerivation)
            .filter(RegenDerivation.run_id == run.id, RegenDerivation.route == route)
            .order_by(RegenDerivation.version.desc()).first())
    rec = RegenDerivation(
        run_id=run.id, run_version=run.version, route=route,
        version=(prev.version + 1) if prev else 1,
        range_start=run.started_at, range_end=run.ended_at, status=status,
        regen_available=_clamp_nonneg(regen), destinations=dest,
        eligibility=_eligibility(dest), allocations=allocations,
        basis={**basis, "summary": summary,
               "consistency": {"ok": not issues, "issues": issues}},
        warnings=warnings, supersedes_id=prev.id if prev else None)
    db.add(rec)
    db.flush()
    return rec


# ---------------- 路线建议 ----------------

def route_advice(db: Session, run: Run, cutoff: datetime | None = None) -> dict:
    """依据分区计量完整度、运行模式稳定性与容许不确定度推荐路线。"""
    cutoff = cutoff or datetime.utcnow()
    zones = _zone_windows(db, run)
    zone_info = []
    complete_zones = 0
    for zone, z in zones.items():
        dev = _zone_boundary_device(db, z["sections"])
        cov = 0.0
        if dev:
            _, ci = _meter_band(db, dev.id, "import_cum", z["start"], z["end"])
            _, ce = _meter_band(db, dev.id, "export_cum", z["start"], z["end"])
            cov = min(ci, ce)
        complete = cov >= REGEN_BOUNDARY_COVERAGE
        complete_zones += int(complete)
        neighbor, n_count, _, _ = _neighbor_demand(db, run, z["sections"],
                                                   z["start"], z["end"])
        zone_info.append({"zone": zone, "boundary_coverage": round(cov, 4),
                          "boundary_complete": complete,
                          "concurrent_trains": n_count,
                          "neighbor_demand": _clamp_nonneg(neighbor)})
    n_zones = max(1, len(zones))
    completeness = round(complete_zones / n_zones, 4)
    # 运行模式稳定性:并发列车数波动越小越稳定(用并发数相对均值近似)
    counts = [x["concurrent_trains"] for x in zone_info] or [0]
    mean = sum(counts) / len(counts)
    var = sum((c - mean) ** 2 for c in counts) / len(counts)
    stability = round(max(0.0, 1.0 - var / (mean + 1) ** 2), 4)
    train_n = len(train_X_refs(db, run, cutoff))

    if completeness < 0.5:
        recommended = "LEARNED"
        rationale = ("分区边界计量完整度不足,守恒分配会留下大量未定份额;"
                     "优先用完整计量时期学习的条件关系给预测区间")
        if train_n < MIN_TRAIN_SAMPLES:
            rationale += ";但历史完整计量样本也不足,路线B只能输出资料不足,需先补装表计"
    elif completeness >= 0.95 and stability >= 0.5:
        recommended = "CONSERVATION"
        rationale = "分区边界计量完整、运行模式稳定,守恒分配可直接确认去向,不确定性最小"
    else:
        recommended = "CONSERVATION"
        rationale = ("分区边界计量基本完整,建议以守恒分配为主;"
                     "多车密集时未定份额偏宽,可对照路线B预测区间作交叉验证")
    return {
        "run_id": run.id, "run_key": run.run_key, "run_version": run.version,
        "metering_completeness": completeness,
        "operation_stability": stability,
        "training_samples": train_n,
        "recommended_route": recommended,
        "rationale": rationale,
        "zones": zone_info,
        "uncertainty_guidance": {
            "CONSERVATION": "物理守恒、可审计;密集多车时未定份额宽。",
            "LEARNED": "日常估计更细;特殊调图/罕见故障下可能失配,需看区间与外推警告。",
        },
    }


def train_X_refs(db: Session, run: Run, cutoff: datetime) -> list[dict]:
    refs = []
    for other in db.query(Run).filter(Run.id != run.id).all():
        latest = (db.query(Run).filter(Run.run_key == other.run_key)
                  .order_by(Run.version.desc()).first())
        if not latest or latest.id != other.id:
            continue
        _, allocs, _, _, _ = derive_conservation(db, latest, cutoff=cutoff)
        for a in allocs:
            if a["boundary"]["complete"] and a["device"]["state_known"]:
                refs.append({"run_key": latest.run_key, "zone": a["zone"]})
    return refs


def derive_both(db: Session, run: Run) -> dict[str, RegenDerivation]:
    return {"CONSERVATION": derive(db, run, "CONSERVATION"),
            "LEARNED": derive(db, run, "LEARNED")}


def auto_rederive_after_status(db: Session, status_row: AbsorptionStatus) -> list[dict]:
    """装置状态到达后:对受影响分区内已发布运行重推,生成新去向版本(不追溯修订)。"""
    dev = db.get(AbsorptionDevice, status_row.device_id)
    if not dev:
        return []
    zone = dev.supply_zone
    affected = []
    for run in db.query(Run).filter(Run.status == "PUBLISHED").all():
        zones = _zone_windows(db, run)
        if zone not in zones:
            continue
        for route in ("CONSERVATION", "LEARNED"):
            rec = derive(db, run, route)
            affected.append({"run_key": run.run_key, "route": route,
                             "version": rec.version, "status": rec.status})
    return affected
