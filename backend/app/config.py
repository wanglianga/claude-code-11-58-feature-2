import os

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg2://ccdev:ccdev@127.0.0.1:5432/ccdev",
)

# 能量求值与核对的全局常量(可随配置版本演进,见 ConfigVersion)
MAX_TRACTION_RATE_KWH_PER_MIN = 40.0   # 重启/失连空窗的牵引上限速率,用于不确定区间上界
MAX_AUX_RATE_KWH_PER_MIN = 3.0
LINE_LOSS_RATE = 0.06                  # 线网传输损耗经验比例(相对供电侧受电)
COVERAGE_THRESHOLD = 0.95              # 可闭合核对的最低时间覆盖率
GRID_BUCKET_MINUTES = 15               # 网格对齐路径的固定时间桶
LATE_ARRIVAL_SECONDS = 300             # 采样-到达超过该值视为迟到采样

# ---- 回馈去向推导 ----
# 分区边界计量达到该覆盖率才允许"已确认吸收/耗散";否则只能给"可能吸收/资料不足"。
REGEN_BOUNDARY_COVERAGE = 0.95
# 吸收装置计量达到该覆盖率,设备吸收才可确认。
DEVICE_METER_COVERAGE = 0.9
# 学习路线:训练样本不足该数量时不出预测区间。
MIN_TRAIN_SAMPLES = 6
# 学习路线预测区间倍数(近似 95%)。
PREDICTION_Z = 1.96
