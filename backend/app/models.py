"""领域模型:采集、运行切分、能量求值、账实核对、指标与复盘。

约定:
- 所有时间戳为 naive UTC(datetime),由写入方保证。
- 能量单位统一为 kWh;累计表读数为表码原值,倍率单独记录,求值时相乘。
- 带不确定范围的能量用 {"v": 估值, "lo": 下界, "hi": 上界} 的 JSON 表示;
  lo/hi 为 None 表示该侧未知(不闭合),禁止用估值冒充闭合计量。
"""
from datetime import datetime

from sqlalchemy import (
    JSON, Column, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint,
)
from sqlalchemy.orm import relationship

from .db import Base, engine


# ---------------- 基础档案 ----------------

class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True)
    username = Column(String(64), unique=True, nullable=False)
    password = Column(String(128), nullable=False)  # 演示环境明文,生产应哈希
    role = Column(String(32), nullable=False, default="viewer")  # admin|viewer
    token = Column(String(64), unique=True, nullable=False)


class Line(Base):
    __tablename__ = "lines"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    name = Column(String(64), nullable=False)


class Section(Base):
    __tablename__ = "sections"
    id = Column(Integer, primary_key=True)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    code = Column(String(32), nullable=False)
    name = Column(String(64), nullable=False)
    seq = Column(Integer, nullable=False)           # 区内顺序
    length_km = Column(Float, nullable=False)
    multiplier_zone = Column(String(32), nullable=False, default="Z1")  # 计量倍率区间
    # 供电分区:同一变电所供电的区段共享一个分区。回馈只能在分区内消耗/抵销,
    # 不同供电分区之间不得跨界抵销。
    supply_zone = Column(String(32), nullable=True)
    gradient_permille = Column(Float, nullable=False, default=0.0)  # 平均坡度,回馈条件建模特征
    substation_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)
    line = relationship("Line")


class Train(Base):
    __tablename__ = "trains"
    id = Column(Integer, primary_key=True)
    code = Column(String(32), unique=True, nullable=False)
    vehicle_model = Column(String(32), nullable=False)  # 车型


class Consist(Base):
    """编组版本:同一列车换编组后必须切断能量片段。"""
    __tablename__ = "consists"
    id = Column(Integer, primary_key=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    code = Column(String(32), nullable=False)       # 如 6B / 6A
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    train = relationship("Train")


class Device(Base):
    __tablename__ = "devices"
    id = Column(Integer, primary_key=True)
    serial = Column(String(64), unique=True, nullable=False)  # 设备序列号
    kind = Column(String(16), nullable=False)       # ONBOARD|SUBSTATION
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=True)
    station_name = Column(String(64), nullable=True)  # 变电所名
    train = relationship("Train")


# ---------------- 采集接入 ----------------

class MeterReading(Base):
    """原始累计值采样。车载表填 traction/regen/aux;变电所表填 import/export。"""
    __tablename__ = "meter_readings"
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("devices.id"), nullable=False, index=True)
    sampled_at = Column(DateTime, nullable=False, index=True)   # 采样时刻(表侧)
    received_at = Column(DateTime, nullable=False)              # 平台到达时刻
    seq = Column(Integer, nullable=False)                       # 设备采样序列号
    traction_cum = Column(Float, nullable=True)
    regen_cum = Column(Float, nullable=True)
    aux_cum = Column(Float, nullable=True)
    import_cum = Column(Float, nullable=True)   # 变电所受电累计
    export_cum = Column(Float, nullable=True)   # 变电所返送(回馈上网)累计
    absorb_cum = Column(Float, nullable=True)   # 储能/回馈吸收装置累计吸收量
    multiplier = Column(Float, nullable=False, default=1.0)     # 计量倍率
    comm_status = Column(String(16), nullable=False, default="OK")  # OK|DEGRADED|LOST
    device = relationship("Device")
    __table_args__ = (UniqueConstraint("device_id", "seq", name="uq_reading_device_seq"),)


# ---------------- 运行切分 ----------------

class Run(Base):
    """一趟可计量运行。迟到补传会产生 version+1 的新行,旧版本保留供复盘。"""
    __tablename__ = "runs"
    id = Column(Integer, primary_key=True)
    run_key = Column(String(64), nullable=False, index=True)  # 业务键,跨版本稳定
    version = Column(Integer, nullable=False, default=1)
    supersedes_id = Column(Integer, ForeignKey("runs.id"), nullable=True)
    train_id = Column(Integer, ForeignKey("trains.id"), nullable=False)
    line_id = Column(Integer, ForeignKey("lines.id"), nullable=False)
    driver_mode = Column(String(16), nullable=False, default="AM")  # AM|CM 司机模式
    started_at = Column(DateTime, nullable=False)
    ended_at = Column(DateTime, nullable=False)
    distance_km = Column(Float, nullable=False)
    passengers = Column(Integer, nullable=False, default=0)
    shift = Column(String(32), nullable=False, default="")          # 班次
    status = Column(String(16), nullable=False, default="OPEN")     # OPEN|EVALUATED|PUBLISHED
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    train = relationship("Train")
    line = relationship("Line")


class RunEvent(Base):
    """切分输入:停车/起动/折返/进出区段/换编组等原始记录。"""
    __tablename__ = "run_events"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    ts = Column(DateTime, nullable=False)
    type = Column(String(24), nullable=False)
    # ENTER_SECTION|EXIT_SECTION|STOP|START_MOVE|TURNBACK|CONSIST_CHANGE
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=True)
    direction = Column(String(8), nullable=True)  # UP|DOWN
    payload = Column(JSON, nullable=True)         # 如 {"consist_id": 2}
    run = relationship("Run")


class RunConsist(Base):
    """运行内的编组有效期(换编组切断)。"""
    __tablename__ = "run_consists"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    consist = relationship("Consist")


class RunSegment(Base):
    """可计量运行片段:同车、同编组、同倍率区间,由停车/折返/换编组/换倍率/设备重启切断。"""
    __tablename__ = "run_segments"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False)
    consist_id = Column(Integer, ForeignKey("consists.id"), nullable=False)
    direction = Column(String(8), nullable=False)
    start_ts = Column(DateTime, nullable=False)
    end_ts = Column(DateTime, nullable=False)
    seq = Column(Integer, nullable=False)
    cut_reason = Column(String(24), nullable=False)  # START|STOP|TURNBACK|SECTION_EXIT|CONSIST_CHANGE|MULTIPLIER_CHANGE|DEVICE_RESET|END
    run = relationship("Run")
    section = relationship("Section")


# ---------------- 能量求值 ----------------

class EnergyFragment(Base):
    """片段级能量事实。quantities 示例:
    车载: {"traction": {"v":..,"lo":..,"hi":..}, "regen": {...}, "aux": {...}}
    供电: {"import": {...}, "export": {...}}
    """
    __tablename__ = "energy_fragments"
    id = Column(Integer, primary_key=True)
    run_segment_id = Column(Integer, ForeignKey("run_segments.id"), nullable=False, index=True)
    source = Column(String(8), nullable=False)      # ONBOARD|SUPPLY
    quantities = Column(JSON, nullable=False)
    quality = Column(String(16), nullable=False)    # GOOD|ESTIMATED|UNKNOWN
    coverage = Column(Float, nullable=False)        # 有效计量时间覆盖率 0..1
    detail = Column(JSON, nullable=True)            # resets/gaps/notes 等解释
    segment = relationship("RunSegment")


class GridCell(Base):
    """网格对齐路径:固定 区段×时间桶 双侧能量累计。"""
    __tablename__ = "grid_cells"
    id = Column(Integer, primary_key=True)
    section_id = Column(Integer, ForeignKey("sections.id"), nullable=False, index=True)
    bucket_start = Column(DateTime, nullable=False, index=True)
    bucket_minutes = Column(Integer, nullable=False)
    onboard = Column(JSON, nullable=False)  # {"traction": {...}, "regen": {...}, "aux": {...}}
    supply = Column(JSON, nullable=False)   # {"import": {...}, "export": {...}}
    run_ids = Column(JSON, nullable=False)  # 落入该桶的运行 id 列表
    section = relationship("Section")


# ---------------- 账实核对 ----------------

class Reconciliation(Base):
    __tablename__ = "reconciliations"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    run_version = Column(Integer, nullable=False)
    method = Column(String(16), nullable=False)     # RUN_ALIGNED|GRID_ALIGNED
    range_start = Column(DateTime, nullable=False)
    range_end = Column(DateTime, nullable=False)
    comparable = Column(String(8), nullable=False)  # FULL|PARTIAL|NONE
    coverage_onboard = Column(Float, nullable=False)
    coverage_supply = Column(Float, nullable=False)
    onboard_net = Column(JSON, nullable=False)      # {"v","lo","hi"} 车载净能耗(牵引+辅助-回馈)
    supply_net = Column(JSON, nullable=False)       # {"v","lo","hi"} 供电净受电(受电-返送)
    diff = Column(JSON, nullable=False)             # supply_net - onboard_net 的区间
    attribution = Column(JSON, nullable=False)      # {line_loss, regen_not_returned, metering_delay, unknown}
    undetermined = Column(JSON, nullable=False)     # 不可判定原因列表
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    run = relationship("Run")


# ---------------- 指标与复盘 ----------------

class Metric(Base):
    """已发布指标。迟到数据只产生新版本指标,旧指标保留原水位与差异说明。"""
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    scope = Column(String(16), nullable=False)      # RUN|SECTION|SHIFT|VEHICLE_MODEL
    scope_key = Column(String(64), nullable=False)  # 如 run_key / section code / 班次 / 车型
    name = Column(String(32), nullable=False)       # KWH_PER_KM|KWH_PER_PASSENGER|REGEN_RATIO|NET_KWH
    value = Column(Float, nullable=True)            # None 表示不可判定
    coverage = Column(Float, nullable=False)
    method = Column(String(16), nullable=False)
    run_version = Column(Integer, nullable=False)
    published_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    supersedes_id = Column(Integer, ForeignKey("metrics.id"), nullable=True)
    diff_note = Column(Text, nullable=True)         # 与上一已发布水位的差异说明
    source_run_ids = Column(JSON, nullable=False)   # 构成该指标的运行版本 id


class ConfigVersion(Base):
    """计量配置版本:倍率、设备映射、回馈上网是否被电网接受等,供复盘追溯。"""
    __tablename__ = "config_versions"
    id = Column(Integer, primary_key=True)
    kind = Column(String(24), nullable=False)       # MULTIPLIER|DEVICE_MAP|REGEN_ACCEPT|LOSS_RATE
    payload = Column(JSON, nullable=False)
    valid_from = Column(DateTime, nullable=False)
    valid_to = Column(DateTime, nullable=True)
    note = Column(String(255), nullable=True)


# ---------------- 回馈去向推导 ----------------

class AbsorptionDevice(Base):
    """分区内可吸收回馈的装置(储能/专用回馈装置)。

    回馈被吸收 ≠ 被电网接受:装置能把回馈消耗在分区内,才形成"装置吸收"。
    kind: STORAGE|REGEN_UNIT。
    """
    __tablename__ = "absorption_devices"
    id = Column(Integer, primary_key=True)
    serial = Column(String(64), unique=True, nullable=False)
    kind = Column(String(16), nullable=False)        # STORAGE|REGEN_UNIT
    supply_zone = Column(String(32), nullable=False, index=True)
    station_name = Column(String(64), nullable=True)
    rated_kwh = Column(Float, nullable=True)         # 单次可吸收上限,用于外观合理性检查
    meter_device_id = Column(Integer, ForeignKey("devices.id"), nullable=True)  # 吸收量计量表


class AbsorptionStatus(Base):
    """吸收装置状态。状态晚到只形成新的去向版本,不追溯改写历史结论。

    state: AVAILABLE|LIMITED|FAULT|OFFLINE。
    observed_at: 状态实际发生时刻;received_at: 平台收到时刻(晚到判据)。
    """
    __tablename__ = "absorption_statuses"
    id = Column(Integer, primary_key=True)
    device_id = Column(Integer, ForeignKey("absorption_devices.id"), nullable=False, index=True)
    state = Column(String(16), nullable=False)
    observed_at = Column(DateTime, nullable=False, index=True)
    received_at = Column(DateTime, nullable=False)
    max_absorb_kwh_per_min = Column(Float, nullable=True)  # 该状态下吸收功率上限
    note = Column(String(255), nullable=True)
    device = relationship("AbsorptionDevice")


class RegenDerivation(Base):
    """一次回馈去向推导(按运行版本 + 路线 + 版本号唯一)。

    每个供电分区一条 allocation;分区之间不跨界抵销。
    destinations 统一使用六类去向,其中后五类为"去向分类",no_source 为镜像项:
      neighbor_train / storage_device / possible_absorption / limited_dissipation / unknown_source
    另有 regen_available_kwh 表示产生回馈总量;类别之和 = regen_available_kwh(守恒区间)。
    """
    __tablename__ = "regen_derivations"
    id = Column(Integer, primary_key=True)
    run_id = Column(Integer, ForeignKey("runs.id"), nullable=False, index=True)
    run_version = Column(Integer, nullable=False)
    route = Column(String(16), nullable=False)       # CONSERVATION|LEARNED
    version = Column(Integer, nullable=False, default=1)  # 状态晚到 -> version+1
    range_start = Column(DateTime, nullable=False)
    range_end = Column(DateTime, nullable=False)
    status = Column(String(16), nullable=False, default="READY")  # READY|PARTIAL|INSUFFICIENT
    regen_available = Column(JSON, nullable=False)   # 产生回馈总量(区间)
    destinations = Column(JSON, nullable=False)      # 五类去向 + no_source 镜像(区间)
    eligibility = Column(JSON, nullable=False)       # 各类去向的节能指标资格说明
    allocations = Column(JSON, nullable=False)       # 分区级守恒分配明细
    basis = Column(JSON, nullable=False)             # 输入、假设、模型系数、版本引用等
    warnings = Column(JSON, nullable=False)          # 物理/资料约束提示
    supersedes_id = Column(Integer, ForeignKey("regen_derivations.id"), nullable=True)
    created_at = Column(DateTime, nullable=False, default=datetime.utcnow)
    run = relationship("Run")


def create_all():
    from . import models as _  # noqa
    Base.metadata.create_all(engine)


def drop_all():
    Base.metadata.drop_all(engine)
