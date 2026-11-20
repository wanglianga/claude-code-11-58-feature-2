"""回馈去向推导验收:五类去向分类、守恒范围、节能资格、两条路线与边界约束。"""
from app.services.regen import DEST_KEYS


def _runs(client, auth):
    return client.get("/api/runs?all_versions=true", headers=auth).json()


def _run_id(runs, key):
    cands = [r for r in runs if r["run_key"] == key]
    return max(cands, key=lambda r: r["version"])["id"]


def _regen(client, auth, run_id, route=None):
    params = {"route": route} if route else {}
    r = client.get(f"/api/runs/{run_id}/regen", params=params, headers=auth)
    assert r.status_code == 200
    return r.json()


def _dest(resp, route):
    return resp["routes"][route]["destinations"]


def _total(dest):
    return sum(dest[k]["v"] or 0.0 for k in DEST_KEYS)


def test_regen_conservation_identities(client, auth):
    """任一运行、任一路线:六类去向之和 = 产生回馈量;无负值;区间不倒挂。"""
    runs = _runs(client, auth)
    for r in runs:
        data = _regen(client, auth, r["id"])
        for route, d in data["routes"].items():
            dest = d["destinations"]
            regen = d["regen_available"]["v"]
            if regen is not None:
                assert abs(_total(dest) - regen) < 0.01, (r["run_key"], route)
            for k in DEST_KEYS:
                b = dest[k]
                if b["v"] is None:
                    continue
                assert b["v"] >= -1e-9 and b["lo"] >= -1e-9       # 不产生负耗散
                assert b["lo"] <= b["hi"] + 1e-9                   # 区间不倒挂
            assert d["basis"]["consistency"]["ok"] is True


def test_eligibility_statement_per_destination(client, auth):
    """每类去向都带节能指标资格说明;耗散/资料不足不计入节电。"""
    runs = _runs(client, auth)
    data = _regen(client, auth, _run_id(runs, "RUN-2001"))
    elig = data["routes"]["CONSERVATION"]["eligibility"]
    # 五类去向 + 返送 + 镜像都需要资格说明
    assert set(DEST_KEYS) <= set(elig)
    assert elig["neighbor_confirmed"]["qualified"] == "YES"
    assert elig["device_confirmed"]["qualified"] == "YES"
    assert elig["limited_dissipation"]["qualified"] == "NO"
    assert elig["unknown_source"]["qualified"] == "UNDETERMINED"
    assert elig["possible_absorption"]["qualified"] == "CONDITIONAL"
    summary = data["routes"]["CONSERVATION"]["basis"]["summary"]
    # 只有已确认类进入"已确认节电";耗散不进
    assert summary["eligible_confirmed"]["v"] == summary["absorbed_confirmed"]["v"]


def test_night_low_load_confirmed_dissipation(client, auth):
    """夜间低负荷、无邻车、边界闭合 -> 回馈确认受限耗散,不计入节电。"""
    runs = _runs(client, auth)
    d = _regen(client, auth, _run_id(runs, "RUN-2001"))["routes"]["CONSERVATION"]
    dest = d["destinations"]
    assert dest["limited_dissipation"]["v"] == 12.0
    assert dest["neighbor_confirmed"]["v"] == 0.0
    assert dest["device_confirmed"]["v"] == 0.0
    assert d["basis"]["summary"]["eligible_confirmed"]["v"] == 0.0
    assert d["status"] == "READY"


def test_morning_peak_possible_not_confirmed(client, auth):
    """早高峰多车:邻车需求高但不自动确认吸收,只列可能吸收。"""
    runs = _runs(client, auth)
    d = _regen(client, auth, _run_id(runs, "RUN-2002A"))["routes"]["CONSERVATION"]
    dest = d["destinations"]
    assert dest["neighbor_confirmed"]["v"] == 0.0
    assert dest["possible_absorption"]["v"] > 0
    assert dest["limited_dissipation"]["v"] > 0
    # 可能吸收不计入"已确认节电"
    assert d["basis"]["summary"]["eligible_confirmed"]["v"] == 0.0


def test_confirmed_neighbor_requires_unique_causal(client, auth):
    """单一邻车牵引(且邻车不回馈)且边界闭合 -> 确认邻车吸收。"""
    runs = _runs(client, auth)
    d = _regen(client, auth, _run_id(runs, "RUN-2006B"))["routes"]["CONSERVATION"]
    assert d["destinations"]["neighbor_confirmed"]["v"] == 12.0
    assert d["basis"]["summary"]["eligible_confirmed"]["v"] == 12.0


def test_confirmed_device_absorption(client, auth):
    """储能装置自有计量完整 + 状态可用 -> 已确认装置吸收,可计入节电。"""
    runs = _runs(client, auth)
    d = _regen(client, auth, _run_id(runs, "RUN-2006A"))["routes"]["CONSERVATION"]
    assert d["destinations"]["device_confirmed"]["v"] == 10.0
    assert d["eligibility"]["device_confirmed"]["qualified"] == "YES"


def test_cross_zone_no_netting(client, auth):
    """跨供电分区:各区独立守恒,返送只在产生区确认,不跨区抵销。"""
    runs = _runs(client, auth)
    d = _regen(client, auth, _run_id(runs, "RUN-2003"))["routes"]["CONSERVATION"]
    allocs = {a["zone"]: a for a in d["allocations"]}
    assert set(allocs) == {"P1", "P2"}
    # 返送只在 P1 确认;P2 边界不闭合,只能可能吸收/资料不足
    p1_export = allocs["P1"]["destinations"]["exported_to_grid"]["v"]
    p2_export = allocs["P2"]["destinations"]["exported_to_grid"]["v"]
    assert p1_export > 0
    assert p2_export == 0.0
    assert allocs["P2"]["boundary"]["complete"] is False
    assert d["status"] == "PARTIAL"


def test_device_status_late_creates_new_version(client, auth):
    """装置状态晚到只形成新的去向版本,旧版本保留且不被改写。"""
    runs = _runs(client, auth)
    run_id = _run_id(runs, "RUN-2004")
    data = _regen(client, auth, run_id, route="CONSERVATION")
    history = [h for h in data["history"] if h["route"] == "CONSERVATION"]
    assert len(history) == 2
    versions = sorted(h["version"] for h in history)
    assert versions == [1, 2]
    latest = data["routes"]["CONSERVATION"]
    assert latest["version"] == 2
    assert latest["supersedes_id"] is not None
    # v1:故障未到达 -> 可能吸收;v2:故障确认 -> 受限耗散
    assert latest["destinations"]["limited_dissipation"]["v"] == 10.0
    assert latest["basis"]["summary"]["eligible_confirmed"]["v"] == 0.0


def test_missing_meter_unknown_vs_learned_prediction(client, auth):
    """缺表:路线A资料不足;路线B用历史完整计量给预测区间。"""
    runs = _runs(client, auth)
    data = _regen(client, auth, _run_id(runs, "RUN-2005"))
    a = data["routes"]["CONSERVATION"]
    b = data["routes"]["LEARNED"]
    assert a["status"] == "PARTIAL"
    assert a["destinations"]["unknown_source"]["v"] == 10.0
    assert a["basis"]["summary"]["eligible_confirmed"]["v"] == 0.0
    # 路线B:可学习(训练样本充足),以可能吸收 + 受限耗散给预测,不给"已确认"
    assert b["status"] == "READY"
    assert b["destinations"]["possible_absorption"]["v"] > 0
    assert b["destinations"]["neighbor_confirmed"]["v"] == 0.0
    assert b["destinations"]["device_confirmed"]["v"] == 0.0
    assert b["basis"]["model"] is not None
    assert b["basis"]["train_samples"] >= 6


def test_learned_route_extrapolation_interval(client, auth):
    """路线B在超出训练范围时给外推警告且保留预测区间(可能失配需可识别)。"""
    runs = _runs(client, auth)
    data = _regen(client, auth, _run_id(runs, "RUN-2004"), route="LEARNED")
    b = data["routes"]["LEARNED"]
    assert b["basis"]["model"] is not None
    codes = {w["code"] for w in b["warnings"]}
    assert "EXTRAPOLATION" in codes
    # 预测区间记录在每个分区 allocation 上,lo<=hi
    for a in b["allocations"]:
        lo, hi = a["prediction_interval"]
        assert lo <= hi


def test_route_advice_tracks_metering_completeness(client, auth):
    """路线建议:计量缺失运行倾向学习路线,完整计量运行倾向守恒路线。"""
    runs = _runs(client, auth)
    adv_missing = client.get(f"/api/runs/{_run_id(runs, 'RUN-2005')}/regen/advice",
                             headers=auth).json()
    adv_full = client.get(f"/api/runs/{_run_id(runs, 'RUN-2001')}/regen/advice",
                          headers=auth).json()
    assert adv_missing["recommended_route"] == "LEARNED"
    assert adv_missing["metering_completeness"] < 0.5
    assert adv_full["recommended_route"] == "CONSERVATION"
    assert adv_full["metering_completeness"] >= 0.95
    assert "rationale" in adv_full and adv_full["rationale"]


def test_ingest_absorption_status_late_creates_version(client, auth):
    """通过接口补报吸收装置状态 -> 受影响分区已发布运行生成新去向版本。"""
    runs = _runs(client, auth)
    run_id = _run_id(runs, "RUN-2001")
    before = _regen(client, auth, run_id, route="CONSERVATION")["routes"]["CONSERVATION"]
    payload = {"device_serial": "ESS-P1", "state": "FAULT",
               "observed_at": "2026-11-12T01:00:00",
               "received_at": "2026-11-12T05:00:00",  # 迟到 4 小时
               "note": "事后发现夜间储能故障"}
    r = client.post("/api/absorption-status", json=payload, headers=auth)
    assert r.status_code == 200
    assert r.json()["late"] is True
    assert any(x["run_key"] == "RUN-2001" for x in r.json()["new_derivations"])
    after = _regen(client, auth, run_id, route="CONSERVATION")["routes"]["CONSERVATION"]
    assert after["version"] == before["version"] + 1
    # 故障确认后,夜间回馈只能受限耗散(仍不产生负值)
    assert after["destinations"]["limited_dissipation"]["v"] == 12.0
    assert after["basis"]["summary"]["eligible_confirmed"]["v"] == 0.0


def test_rederive_is_versioned_not_destructive(client, auth):
    """重复推导生成递增版本,旧版本保留。"""
    runs = _runs(client, auth)
    run_id = _run_id(runs, "RUN-2006B")
    base = _regen(client, auth, run_id, route="CONSERVATION")["routes"]["CONSERVATION"]
    r = client.post(f"/api/runs/{run_id}/regen/derive?route=CONSERVATION", headers=auth)
    assert r.status_code == 200
    new = r.json()["CONSERVATION"]
    assert new["version"] == base["version"] + 1
    hist = [h for h in _regen(client, auth, run_id, route="CONSERVATION")["history"]
            if h["route"] == "CONSERVATION"]
    assert len(hist) >= 2
