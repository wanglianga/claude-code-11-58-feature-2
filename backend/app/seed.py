"""种子数据:五类验收场景 + 回馈去向推导场景。

RUN-1001 跨区段折返(T01, L1, S1->S2 上行折返后下行返回)
RUN-1002 设备重启 + 倍率区间切换 + 运行中换编组(T02, L1)
RUN-1003 回馈受限(T03, L2/S3,变电所不接受回馈上网)
RUN-1004 车载与供电表差异(T01, L1,差异=线损+未明残差)
RUN-1005 补传读数(T02, L1,先按缺测发布 v1,迟到数据触发 v2)
RUN-1006 供电表缺失(T03, L2,供电侧输出未知范围,不以车载冒充闭合)

回馈去向验收(新增):
RUN-2001 夜间低负荷(T01, S1/S2):几乎无邻车,边界闭合 -> 回馈多进入受限耗散
RUN-2002 早高峰多车(T01/T02 同区并发):多车归因不明 -> 可能吸收,未定份额宽
RUN-2003 跨分区(T01, S1(Z1)->S2(Z2)):两分区各自守恒,返送不跨区抵销
RUN-2004 装置故障(T02, S1):储能装置在线计量,晚到故障状态生成新去向版本
RUN-2005 缺表(T03, S3 分区 P3 供电表缺失,储能表在):路线A资料不足,路线B给预测区间
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy.orm import Session

from .db import SessionLocal
from .models import (AbsorptionDevice, AbsorptionStatus, ConfigVersion, Consist,
                     Device, Line, MeterReading, Run, RunConsist, RunEvent,
                     Section, Train, User, create_all, drop_all)
from .services.evaluate import evaluate_run, rebuild_grid
from .services.metrics import publish_run_metrics, reprocess_run
from .services.reconcile import reconcile_grid_aligned, reconcile_run_aligned
from .services.regen import derive, derive_both

D1 = datetime(2026, 10, 20, 8, 0)   # 场景1/2
D4 = datetime(2026, 10, 21, 14, 0)  # 场景4
D5 = datetime(2026, 10, 22, 16, 0)  # 场景5
D3 = datetime(2026, 11, 18, 7, 0)   # 场景3(回馈受限配置生效后)
D6 = datetime(2026, 11, 19, 9, 0)   # 场景6

N1 = datetime(2026, 11, 12, 1, 0)   # 去向-夜间低负荷
MP = datetime(2026, 11, 12, 7, 30)  # 去向-早高峰多车
XZ = datetime(2026, 11, 12, 10, 0)  # 去向-跨分区
FA = datetime(2026, 11, 12, 13, 0)  # 去向-装置故障
MM = datetime(2026, 11, 12, 15, 0)  # 去向-缺表

STEP = timedelta(minutes=5)


class Counter:
    """每台设备的累计表码状态(跨运行连续累计)。"""
    def __init__(self):
        self.v = {"traction": 1000.0, "regen": 200.0, "aux": 500.0,
                  "import": 5000.0, "export": 300.0, "absorb": 0.0}
        self.seq = 0


def gen_onboard(db: Session, device: Device, st: Counter, t0, t1,
                rates=(0.5, 0.1, 0.05), multiplier=1.0,
                reset_at=None, mult_change_at=None, mult_after=2.0,
                skip_after=None, received_delay_min=0):
    """生成车载读数。reset_at: 该时刻设备重启,计数器归零;
    mult_change_at: 倍率切换时刻;skip_after: 该时刻后缺测(通信中断)。"""
    rows = []
    t, mult = t0, multiplier
    while t <= t1:
        if skip_after and t > skip_after:
            break
        if mult_change_at and t >= mult_change_at:
            mult = mult_after
        if reset_at and t == reset_at:
            st.v["traction"] = st.v["regen"] = st.v["aux"] = 0.0
        st.seq += 1
        rows.append(MeterReading(
            device_id=device.id, sampled_at=t,
            received_at=t + timedelta(minutes=received_delay_min),
            seq=st.seq, traction_cum=round(st.v["traction"], 3),
            regen_cum=round(st.v["regen"], 3), aux_cum=round(st.v["aux"], 3),
            multiplier=mult, comm_status="OK"))
        st.v["traction"] += rates[0] * 5
        st.v["regen"] += rates[1] * 5
        st.v["aux"] += rates[2] * 5
        t += STEP
    db.add_all(rows)
    return rows


def gen_supply(db: Session, device: Device, st: Counter, t0, t1,
               imp_rate=0.62, exp_rate=0.12):
    rows, t = [], t0
    while t <= t1:
        st.seq += 1
        rows.append(MeterReading(
            device_id=device.id, sampled_at=t, received_at=t + timedelta(seconds=30),
            seq=st.seq, import_cum=round(st.v["import"], 3),
            export_cum=round(st.v["export"], 3), multiplier=1.0, comm_status="OK"))
        st.v["import"] += imp_rate * 5
        st.v["export"] += exp_rate * 5
        t += STEP
    db.add_all(rows)
    return rows


def gen_absorb(db: Session, device: Device, st: Counter, t0, t1,
               rate=0.4, start_at=None):
    """储能/回馈装置吸收量累计表。start_at 之前不吸收(保持平台期)。"""
    rows, t = [], t0
    while t <= t1:
        st.seq += 1
        rows.append(MeterReading(
            device_id=device.id, sampled_at=t, received_at=t + timedelta(seconds=30),
            seq=st.seq, absorb_cum=round(st.v["absorb"], 3),
            multiplier=1.0, comm_status="OK"))
        if start_at is None or t >= start_at:
            st.v["absorb"] += rate * 5
        t += STEP
    db.add_all(rows)
    return rows


def mk_run(db, key, train, line, start, end, dist, pax, shift, mode="AM"):
    run = Run(run_key=key, version=1, train_id=train.id, line_id=line.id,
              driver_mode=mode, started_at=start, ended_at=end,
              distance_km=dist, passengers=pax, shift=shift)
    db.add(run)
    db.flush()
    return run


def ev(db, run, ts, type_, section=None, direction=None, payload=None):
    db.add(RunEvent(run_id=run.id, ts=ts, type=type_,
                    section_id=section.id if section else None,
                    direction=direction, payload=payload))


def process(db, run, note=None):
    db.flush()  # 会话关闭 autoflush,先落库再切分求值
    evaluate_run(db, run)
    rebuild_grid(db)
    reconcile_run_aligned(db, run)
    reconcile_grid_aligned(db, run)
    publish_run_metrics(db, run, note=note)


def seed():
    drop_all()
    create_all()
    db: Session = SessionLocal()
    try:
        # ---- 用户 ----
        db.add_all([
            User(username="admin", password="admin123", role="admin",
                 token="tok-admin"),
            User(username="viewer", password="viewer123", role="viewer",
                 token="tok-viewer"),
        ])
        # ---- 线路/区段 ----
        l1 = Line(code="L1", name="1号线")
        l2 = Line(code="L2", name="2号线")
        db.add_all([l1, l2]); db.flush()
        # ---- 列车/编组/设备 ----
        t01 = Train(code="T01", vehicle_model="M1")
        t02 = Train(code="T02", vehicle_model="M1")
        t03 = Train(code="T03", vehicle_model="M2")
        db.add_all([t01, t02, t03]); db.flush()
        c01 = Consist(train_id=t01.id, code="6B", valid_from=datetime(2026, 1, 1))
        c02a = Consist(train_id=t02.id, code="6B", valid_from=datetime(2026, 1, 1),
                       valid_to=datetime(2026, 10, 20, 10, 15))
        c02b = Consist(train_id=t02.id, code="4B", valid_from=datetime(2026, 10, 20, 10, 15))
        c03 = Consist(train_id=t03.id, code="6A", valid_from=datetime(2026, 1, 1))
        db.add_all([c01, c02a, c02b, c03]); db.flush()
        ob1 = Device(serial="OB-T01", kind="ONBOARD", train_id=t01.id)
        ob2 = Device(serial="OB-T02", kind="ONBOARD", train_id=t02.id)
        ob3 = Device(serial="OB-T03", kind="ONBOARD", train_id=t03.id)
        sub1 = Device(serial="SUB-S1", kind="SUBSTATION", station_name="A站牵引所")
        sub2 = Device(serial="SUB-S2", kind="SUBSTATION", station_name="B站牵引所")
        sub3 = Device(serial="SUB-S3", kind="SUBSTATION", station_name="D站牵引所")
        db.add_all([ob1, ob2, ob3, sub1, sub2, sub3]); db.flush()
        s1 = Section(line_id=l1.id, code="S1", name="A站-B站", seq=1, length_km=10,
                     multiplier_zone="Z1", supply_zone="P1", gradient_permille=8.0,
                     substation_device_id=sub1.id)
        s2 = Section(line_id=l1.id, code="S2", name="B站-C站", seq=2, length_km=12,
                     multiplier_zone="Z1", supply_zone="P2", gradient_permille=-6.0,
                     substation_device_id=sub2.id)
        s3 = Section(line_id=l2.id, code="S3", name="D站-E站", seq=1, length_km=8,
                     multiplier_zone="Z2", supply_zone="P3", gradient_permille=5.0,
                     substation_device_id=sub3.id)
        db.add_all([s1, s2, s3]); db.flush()
        # ---- 吸收装置(储能)----
        sto_meter = Device(serial="MET-ESS1", kind="SUBSTATION", station_name="A站储能计量")
        sto3_meter = Device(serial="MET-ESS3", kind="SUBSTATION", station_name="D站储能计量")
        db.add_all([sto_meter, sto3_meter]); db.flush()
        ess1 = AbsorptionDevice(serial="ESS-P1", kind="STORAGE", supply_zone="P1",
                                station_name="A站储能", rated_kwh=400.0,
                                meter_device_id=sto_meter.id)
        ess3 = AbsorptionDevice(serial="ESS-P3", kind="STORAGE", supply_zone="P3",
                                station_name="D站储能", rated_kwh=300.0,
                                meter_device_id=sto3_meter.id)
        # P2 回馈装置无独立计量表:用于"状态晚到"场景,状态未到时无法确认吸收
        ess2 = AbsorptionDevice(serial="ESS-P2", kind="REGEN_UNIT", supply_zone="P2",
                                station_name="C站回馈装置", rated_kwh=200.0,
                                meter_device_id=None)
        db.add_all([ess1, ess2, ess3]); db.flush()
        # P1 储能自年初可用;P3 储能初始无状态记录(为"状态晚到"场景保留未知)
        db.add(AbsorptionStatus(device_id=ess1.id, state="AVAILABLE",
                                observed_at=datetime(2026, 1, 1),
                                received_at=datetime(2026, 1, 1),
                                max_absorb_kwh_per_min=6.0, note="储能投运"))
        # ---- 配置版本 ----
        db.add_all([
            ConfigVersion(kind="MULTIPLIER", valid_from=datetime(2026, 1, 1),
                          payload={"OB-T01": 1.0, "OB-T02": 1.0, "OB-T03": 1.0},
                          note="车载表初始倍率"),
            ConfigVersion(kind="MULTIPLIER", valid_from=datetime(2026, 10, 20, 10, 40),
                          payload={"OB-T02": 2.0},
                          note="T02 进入倍率区间 Z2,倍率 1.0->2.0"),
            ConfigVersion(kind="REGEN_ACCEPT", valid_from=datetime(2026, 1, 1),
                          payload={"default": True, "stations": {}},
                          note="默认接受回馈上网"),
            ConfigVersion(kind="REGEN_ACCEPT", valid_from=datetime(2026, 11, 1),
                          payload={"default": True, "stations": {"SUB-S3": False}},
                          note="D站牵引所回馈装置检修,不接受回馈上网"),
            ConfigVersion(kind="LOSS_RATE", valid_from=datetime(2026, 1, 1),
                          payload={"rate": 0.06}, note="线网损耗经验值 6%"),
        ])

        st = {d.serial: Counter() for d in [ob1, ob2, ob3, sub1, sub2, sub3,
                                            sto_meter, sto3_meter]}

        # ============ RUN-1001 跨区段折返 ============
        r1 = mk_run(db, "RUN-1001", t01, l1, D1, D1 + timedelta(minutes=85),
                    44, 1200, "早高峰-1")
        db.add(RunConsist(run_id=r1.id, consist_id=c01.id, valid_from=D1))
        ev(db, r1, D1, "ENTER_SECTION", s1, "UP")
        ev(db, r1, D1 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r1, D1 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=30), "STOP", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=35), "START_MOVE", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=45), "TURNBACK", s2, "UP")
        ev(db, r1, D1 + timedelta(minutes=65), "EXIT_SECTION", s2, "DOWN")
        ev(db, r1, D1 + timedelta(minutes=65), "ENTER_SECTION", s1, "DOWN")
        ev(db, r1, D1 + timedelta(minutes=85), "EXIT_SECTION", s1, "DOWN")
        gen_onboard(db, ob1, st["OB-T01"], D1, D1 + timedelta(minutes=85))
        gen_supply(db, sub1, st["SUB-S1"], D1, D1 + timedelta(minutes=90),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], D1 + timedelta(minutes=20),
                   D1 + timedelta(minutes=70), imp_rate=0.60, exp_rate=0.10)
        process(db, r1)

        # ============ RUN-1002 设备重启+倍率切换+换编组 ============
        t0 = D1 + timedelta(hours=2)  # 10:00-10:50,避开 RUN-1001 的供电窗口
        r2 = mk_run(db, "RUN-1002", t02, l1, t0, t0 + timedelta(minutes=50),
                    22, 800, "早高峰-1", mode="CM")
        db.add_all([
            RunConsist(run_id=r2.id, consist_id=c02a.id, valid_from=t0,
                       valid_to=t0 + timedelta(minutes=15)),
            RunConsist(run_id=r2.id, consist_id=c02b.id,
                       valid_from=t0 + timedelta(minutes=15)),
        ])
        ev(db, r2, t0, "ENTER_SECTION", s1, "UP")
        ev(db, r2, t0 + timedelta(minutes=15), "CONSIST_CHANGE", s1, "UP",
           payload={"consist_id": c02b.id})
        ev(db, r2, t0 + timedelta(minutes=25), "EXIT_SECTION", s1, "UP")
        ev(db, r2, t0 + timedelta(minutes=25), "ENTER_SECTION", s2, "UP")
        ev(db, r2, t0 + timedelta(minutes=50), "EXIT_SECTION", s2, "UP")
        gen_onboard(db, ob2, st["OB-T02"], t0, t0 + timedelta(minutes=50),
                    reset_at=t0 + timedelta(minutes=10),
                    mult_change_at=t0 + timedelta(minutes=40), mult_after=2.0)
        gen_supply(db, sub1, st["SUB-S1"], t0, t0 + timedelta(minutes=30),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], t0 + timedelta(minutes=25),
                   t0 + timedelta(minutes=55), imp_rate=0.60, exp_rate=0.10)
        process(db, r2)

        # ============ RUN-1003 回馈受限 ============
        r3 = mk_run(db, "RUN-1003", t03, l2, D3, D3 + timedelta(minutes=25),
                    8, 500, "早高峰-2")
        db.add(RunConsist(run_id=r3.id, consist_id=c03.id, valid_from=D3))
        ev(db, r3, D3, "ENTER_SECTION", s3, "UP")
        ev(db, r3, D3 + timedelta(minutes=25), "EXIT_SECTION", s3, "UP")
        gen_onboard(db, ob3, st["OB-T03"], D3, D3 + timedelta(minutes=25),
                    rates=(0.5, 0.4, 0.05))  # 大回馈
        gen_supply(db, sub3, st["SUB-S3"], D3, D3 + timedelta(minutes=30),
                   imp_rate=0.30, exp_rate=0.0)  # 电网不接受回馈
        process(db, r3)

        # ============ RUN-1004 车载与供电表差异 ============
        r4 = mk_run(db, "RUN-1004", t01, l1, D4, D4 + timedelta(minutes=40),
                    22, 600, "平峰-1")
        db.add(RunConsist(run_id=r4.id, consist_id=c01.id, valid_from=D4))
        ev(db, r4, D4, "ENTER_SECTION", s1, "UP")
        ev(db, r4, D4 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r4, D4 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r4, D4 + timedelta(minutes=40), "EXIT_SECTION", s2, "UP")
        gen_onboard(db, ob1, st["OB-T01"], D4, D4 + timedelta(minutes=40))
        gen_supply(db, sub1, st["SUB-S1"], D4, D4 + timedelta(minutes=25),
                   imp_rate=0.78, exp_rate=0.05)  # 差异偏大
        gen_supply(db, sub2, st["SUB-S2"], D4 + timedelta(minutes=20),
                   D4 + timedelta(minutes=45), imp_rate=0.74, exp_rate=0.05)
        process(db, r4)

        # ============ RUN-1005 补传读数 ============
        r5 = mk_run(db, "RUN-1005", t02, l1, D5, D5 + timedelta(minutes=40),
                    22, 900, "平峰-2")
        db.add(RunConsist(run_id=r5.id, consist_id=c02b.id, valid_from=D5))
        ev(db, r5, D5, "ENTER_SECTION", s1, "UP")
        ev(db, r5, D5 + timedelta(minutes=20), "EXIT_SECTION", s1, "UP")
        ev(db, r5, D5 + timedelta(minutes=20), "ENTER_SECTION", s2, "UP")
        ev(db, r5, D5 + timedelta(minutes=40), "EXIT_SECTION", s2, "UP")
        # v1: 16:15 之后车载缺测(通信中断),先按缺测发布
        gen_onboard(db, ob2, st["OB-T02"], D5, D5 + timedelta(minutes=40),
                    skip_after=D5 + timedelta(minutes=15))
        gen_supply(db, sub1, st["SUB-S1"], D5, D5 + timedelta(minutes=25),
                   imp_rate=0.62, exp_rate=0.12)
        gen_supply(db, sub2, st["SUB-S2"], D5 + timedelta(minutes=20),
                   D5 + timedelta(minutes=45), imp_rate=0.60, exp_rate=0.10)
        process(db, r5)  # 发布 v1(低覆盖)
        # 迟到补传:16:20-16:40 的读数在 1 小时后到达 -> 触发 v2
        late_rows = []
        t = D5 + timedelta(minutes=20)
        while t <= D5 + timedelta(minutes=40):
            st["OB-T02"].seq += 1
            late_rows.append(MeterReading(
                device_id=ob2.id, sampled_at=t,
                received_at=t + timedelta(hours=1),  # 迟到
                seq=st["OB-T02"].seq,
                traction_cum=round(st["OB-T02"].v["traction"], 3),
                regen_cum=round(st["OB-T02"].v["regen"], 3),
                aux_cum=round(st["OB-T02"].v["aux"], 3),
                multiplier=1.0, comm_status="OK"))
            st["OB-T02"].v["traction"] += 0.5 * 5
            st["OB-T02"].v["regen"] += 0.1 * 5
            st["OB-T02"].v["aux"] += 0.05 * 5
            t += STEP
        db.add_all(late_rows); db.flush()
        r5v2 = reprocess_run(db, r5, note="迟到补传")
        process(db, r5v2, note="迟到补传触发新版本")

        # ============ RUN-1006 供电表缺失 ============
        r6 = mk_run(db, "RUN-1006", t03, l2, D6, D6 + timedelta(minutes=25),
                    8, 450, "早高峰-2")
        db.add(RunConsist(run_id=r6.id, consist_id=c03.id, valid_from=D6))
        ev(db, r6, D6, "ENTER_SECTION", s3, "UP")
        ev(db, r6, D6 + timedelta(minutes=25), "EXIT_SECTION", s3, "UP")
        gen_onboard(db, ob3, st["OB-T03"], D6, D6 + timedelta(minutes=25))
        # SUB-S3 在该窗口无任何读数(表计检修)-> 供电侧未知
        process(db, r6)

        # ================== 回馈去向验收场景 ==================
        def supply_window(sub, t0, t1, imp, exp):
            gen_supply(db, sub, st[sub.serial], t0, t1, imp_rate=imp, exp_rate=exp)

        def onboard_window(ob, t0, t1, regen_rate, trac_rate=0.5):
            gen_onboard(db, ob, st[ob.serial], t0, t1,
                        rates=(trac_rate, regen_rate, 0.05))

        # ---- RUN-2001 夜间低负荷:P1,无邻车,装置在线 -> 有界耗散 ----
        n0, n1 = N1, N1 + timedelta(minutes=20)
        r21 = mk_run(db, "RUN-2001", t01, l1, n0, n1, 10, 40, "夜间")
        db.add(RunConsist(run_id=r21.id, consist_id=c01.id, valid_from=n0))
        ev(db, r21, n0, "ENTER_SECTION", s1, "UP")
        ev(db, r21, n1, "EXIT_SECTION", s1, "UP")
        onboard_window(ob1, n0, n1, regen_rate=0.6, trac_rate=0.2)
        supply_window(sub1, n0, n1 + timedelta(minutes=5), imp=0.10, exp=0.0)
        gen_absorb(db, sto_meter, st["MET-ESS1"], n0, n1 + timedelta(minutes=5),
                   rate=0.0)  # 装置在线但未吸收
        process(db, r21)

        # ---- RUN-2002 早高峰多车:P1 同时段两车 -> 可能吸收/未定份额宽 ----
        m0, m1 = MP, MP + timedelta(minutes=20)
        r22a = mk_run(db, "RUN-2002A", t01, l1, m0, m1, 10, 1500, "早高峰-1")
        r22b = mk_run(db, "RUN-2002B", t02, l1, m0 + timedelta(minutes=5), m1,
                      10, 1400, "早高峰-1")
        db.add(RunConsist(run_id=r22a.id, consist_id=c01.id, valid_from=m0))
        db.add(RunConsist(run_id=r22b.id, consist_id=c02b.id,
                          valid_from=m0 + timedelta(minutes=5)))
        ev(db, r22a, m0, "ENTER_SECTION", s1, "UP")
        ev(db, r22a, m1, "EXIT_SECTION", s1, "UP")
        ev(db, r22b, m0 + timedelta(minutes=5), "ENTER_SECTION", s1, "UP")
        ev(db, r22b, m1, "EXIT_SECTION", s1, "UP")
        onboard_window(ob1, m0, m1, regen_rate=0.5, trac_rate=0.5)
        onboard_window(ob2, m0 + timedelta(minutes=5), m1, regen_rate=0.5, trac_rate=0.5)
        supply_window(sub1, m0, m1 + timedelta(minutes=5), imp=0.9, exp=0.0)
        gen_absorb(db, sto_meter, st["MET-ESS1"], m0, m1 + timedelta(minutes=5),
                   rate=0.0)
        process(db, r22a)
        process(db, r22b)

        # ---- RUN-2003 跨分区:S1(P1,边界闭合)->S2(P2,边界部分覆盖) ----
        # 两分区各自守恒:返送只在产生它的分区内确认,不得跨区抵销;
        # P2 边界不闭合,其回馈只能列可能吸收/资料不足,不能拿 P1 的返送去冲抵。
        x0, xm, x1 = XZ, XZ + timedelta(minutes=15), XZ + timedelta(minutes=30)
        r23 = mk_run(db, "RUN-2003", t01, l1, x0, x1, 22, 700, "平峰-3")
        db.add(RunConsist(run_id=r23.id, consist_id=c01.id, valid_from=x0))
        ev(db, r23, x0, "ENTER_SECTION", s1, "UP")
        ev(db, r23, xm, "EXIT_SECTION", s1, "UP")
        ev(db, r23, xm, "ENTER_SECTION", s2, "UP")
        ev(db, r23, x1, "EXIT_SECTION", s2, "UP")
        onboard_window(ob1, x0, x1, regen_rate=0.5, trac_rate=0.4)
        # P1 边界闭合、有返送、装置在线未吸收 -> 返送 + 耗散
        supply_window(sub1, x0, xm + timedelta(minutes=5), imp=0.4, exp=0.2)
        # P2 边界只在后段有读数(覆盖率 < 95%)-> 不闭合
        gen_supply(db, sub2, st["SUB-S2"], x1 - timedelta(minutes=5),
                   x1 + timedelta(minutes=5), imp_rate=0.4, exp_rate=0.0)
        gen_absorb(db, sto_meter, st["MET-ESS1"], x0, x1 + timedelta(minutes=5),
                   rate=0.0)
        process(db, r23)

        # ---- RUN-2004 装置故障:P2 回馈装置状态晚到 -> 新去向版本 ----
        # 故障前:装置已知可用但无独立计量,吸收只能列为"可能吸收"(容量上限);
        # 故障状态晚到后:确认不能吸收 -> 剩余只能"受限耗散"。两版并存,不追溯改写。
        f0, f1 = FA, FA + timedelta(minutes=20)
        r24 = mk_run(db, "RUN-2004", t02, l1, f0, f1, 12, 600, "平峰-4")
        db.add(RunConsist(run_id=r24.id, consist_id=c02b.id, valid_from=f0))
        ev(db, r24, f0, "ENTER_SECTION", s2, "UP")
        ev(db, r24, f1, "EXIT_SECTION", s2, "UP")
        onboard_window(ob2, f0, f1, regen_rate=0.5, trac_rate=0.4)
        supply_window(sub2, f0, f1 + timedelta(minutes=5), imp=0.3, exp=0.0)
        process(db, r24)
        # 首次推导:故障状态尚未到达(截止 f0+10min,早于故障到达 f0+1h)
        derive(db, r24, "CONSERVATION", cutoff=f0 + timedelta(minutes=10))
        derive(db, r24, "LEARNED", cutoff=f0 + timedelta(minutes=10))
        db.flush()
        db.add(AbsorptionStatus(device_id=ess2.id, state="FAULT",
                                observed_at=f0, received_at=f0 + timedelta(hours=1),
                                note="C站回馈装置变流器故障,制动回馈只能电阻耗散"))
        db.flush()
        # 状态晚到后重推 -> 新去向版本,不追溯改写旧版本
        derive_both(db, r24)

        # ---- RUN-2005 缺表:P3 供电表缺失,储能表在 -> A资料不足/B预测区间 ----
        # 先给 P3 足够历史(完整计量 + 装置吸收),让学习路线有训练样本
        hist_starts = [datetime(2026, 11, 8, 6, 0) + timedelta(hours=2 * i) for i in range(8)]
        t03_hist = []
        for i, h in enumerate(hist_starts):
            h1 = h + timedelta(minutes=20)
            rh = mk_run(db, f"RUN-2100-{i+1}", t03, l2, h, h1, 8, 400, "历史")
            db.add(RunConsist(run_id=rh.id, consist_id=c03.id, valid_from=h))
            ev(db, rh, h, "ENTER_SECTION", s3, "UP")
            ev(db, rh, h1, "EXIT_SECTION", s3, "UP")
            onboard_window(ob3, h, h1, regen_rate=0.5, trac_rate=0.4)
            supply_window(sub3, h, h1 + timedelta(minutes=5), imp=0.35, exp=0.0)
            gen_absorb(db, sto3_meter, st["MET-ESS3"], h, h1 + timedelta(minutes=5),
                       rate=0.6)  # P3 储能吸收
            db.add(AbsorptionStatus(device_id=ess3.id, state="AVAILABLE",
                                    observed_at=h, received_at=h,
                                    max_absorb_kwh_per_min=6.0, note="储能可用"))
            db.flush()
            process(db, rh)
            t03_hist.append(rh)

        g0, g1 = MM, MM + timedelta(minutes=20)
        r25 = mk_run(db, "RUN-2005", t03, l2, g0, g1, 8, 450, "平峰-5")
        db.add(RunConsist(run_id=r25.id, consist_id=c03.id, valid_from=g0))
        ev(db, r25, g0, "ENTER_SECTION", s3, "UP")
        ev(db, r25, g1, "EXIT_SECTION", s3, "UP")
        onboard_window(ob3, g0, g1, regen_rate=0.5, trac_rate=0.4)
        # 该时窗 SUB-S3 与 P3 储能表双双缺测:边界未知 + 装置吸收无直接计量。
        # 车载回馈可测,但去向只能靠路线B以历史条件关系给预测区间。
        gen_absorb(db, sto3_meter, st["MET-ESS3"], g0 - timedelta(hours=1),
                   g0 - timedelta(minutes=5), rate=0.0)
        db.add(AbsorptionStatus(device_id=ess3.id, state="AVAILABLE",
                                observed_at=g0, received_at=g0,
                                max_absorb_kwh_per_min=6.0, note="储能可用"))
        db.flush()
        process(db, r25)

        # ---- RUN-2006 确认装置吸收 / 确认邻车吸收 ----
        # 两个独立分区窗口,单独验证"已确认"两类:
        #  (a) P3 边界闭合 + 储能计量吸收 -> 已确认装置吸收
        #  (b) P1 边界闭合,同窗仅一辆邻车在牵引且邻车本身不回馈 -> 已确认邻车吸收
        c0 = datetime(2026, 11, 13, 9, 0)
        c1 = c0 + timedelta(minutes=20)
        r26a = mk_run(db, "RUN-2006A", t03, l2, c0, c1, 8, 500, "平峰-6")
        db.add(RunConsist(run_id=r26a.id, consist_id=c03.id, valid_from=c0))
        ev(db, r26a, c0, "ENTER_SECTION", s3, "UP")
        ev(db, r26a, c1, "EXIT_SECTION", s3, "UP")
        onboard_window(ob3, c0, c1, regen_rate=0.5, trac_rate=0.4)
        supply_window(sub3, c0, c1 + timedelta(minutes=5), imp=0.35, exp=0.0)
        gen_absorb(db, sto3_meter, st["MET-ESS3"], c0, c1 + timedelta(minutes=5),
                   rate=0.6)  # 储能计量吸收
        db.add(AbsorptionStatus(device_id=ess3.id, state="AVAILABLE",
                                observed_at=c0, received_at=c0,
                                max_absorb_kwh_per_min=6.0, note="储能可用"))
        db.flush()
        process(db, r26a)

        # (b) 邻车吸收:P1,目标车在 c0 制动回馈;邻车只需大牵引、不回馈
        nb0, nb1 = c0 + timedelta(hours=2), c0 + timedelta(hours=2, minutes=20)
        r26b = mk_run(db, "RUN-2006B", t01, l1, nb0, nb1, 10, 500, "平峰-6")
        nb_run = mk_run(db, "RUN-2006C", t02, l1, nb0 + timedelta(minutes=5), nb1,
                        10, 1200, "平峰-6")
        db.add(RunConsist(run_id=r26b.id, consist_id=c01.id, valid_from=nb0))
        db.add(RunConsist(run_id=nb_run.id, consist_id=c02b.id,
                          valid_from=nb0 + timedelta(minutes=5)))
        ev(db, r26b, nb0, "ENTER_SECTION", s1, "UP")
        ev(db, r26b, nb1, "EXIT_SECTION", s1, "UP")
        ev(db, nb_run, nb0 + timedelta(minutes=5), "ENTER_SECTION", s1, "UP")
        ev(db, nb_run, nb1, "EXIT_SECTION", s1, "UP")
        onboard_window(ob1, nb0, nb1, regen_rate=0.6, trac_rate=0.2)
        # 邻车只有牵引、无回馈(回馈率 0)
        gen_onboard(db, ob2, st["OB-T02"], nb0 + timedelta(minutes=5), nb1,
                    rates=(0.8, 0.0, 0.05))
        supply_window(sub1, nb0, nb1 + timedelta(minutes=5), imp=0.9, exp=0.0)
        gen_absorb(db, sto_meter, st["MET-ESS1"], nb0, nb1 + timedelta(minutes=5),
                   rate=0.0)
        process(db, r26b)
        process(db, nb_run)

        # 为已有验收运行补充回馈去向推导(路线A+B)。RUN-2004 已自行管理"状态晚到"
        # 版本序列,此处跳过;RUN-2003 正常补全。
        for r in [r1, r2, r3, r4, r5, r6, r21, r22a, r22b, r23, r25, r26a, r26b,
                  nb_run]:
            latest = (db.query(Run).filter(Run.run_key == r.run_key)
                      .order_by(Run.version.desc()).first())
            derive_both(db, latest)

        db.commit()
        print("seed ok: 运行含五类账实场景 + 回馈去向验收场景")
    finally:
        db.close()


if __name__ == "__main__":
    seed()
