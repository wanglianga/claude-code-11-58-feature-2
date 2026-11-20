# 轨道交通牵引能耗运行事实与对标平台

判断一趟列车的**牵引取电、制动回馈、辅助负荷**与**站场供电表**是否描述了同一段可计量运行,
形成可用于线路对标、节能评估和事故复盘的能耗事实。

技术栈:Vue 3 + Element Plus(前端)/ FastAPI + SQLAlchemy(后端)/ PostgreSQL(存储)。

## 核心规则

| 环节 | 规则 |
| --- | --- |
| 采集接入 | 保存原始累计值、采样时刻、设备序列号、通信状态、计量倍率;迟到采样按到达时间识别 |
| 运行切分 | 按列车、编组、线路、区段、停车与折返记录切成可计量片段;**换编组、进入不同倍率区间、设备重启/读数倒退必须切断** |
| 能量求值 | 累计值重启或倒退**不直接相减**,只累加单调段,空窗按上限速率扩展不确定区间上界;供电表缺失或覆盖不足时输出**未知范围**,不以车载值冒充闭合计量 |
| 账实核对 | 计算列车侧与供电侧可比较范围、差异去向(线损/回馈未上网/计量延迟/不可知)与不可判定原因 |
| 双路径 | 路径A 运行对齐:逐片段对齐两侧时间边界再汇总;路径B 网格对齐:区段×15min 固定网格累计双侧能量、运行映射进网格,跨格按时间比例分摊为区间。两者对同一运行范围独立给出可比较能耗与差异边界 |
| 指标服务 | 按运行/区段/班次/车型发布单位里程、单位载客能耗与回馈比例,带来源覆盖率 |
| 迟到补传 | 只生成新运行版本;**已发布指标保留原水位**,新指标带差异说明,复盘可回查完整链路 |
| 回馈去向 | 车载负向读数只是"产生的回馈",不等于线路节电。按供电分区逐区守恒,把回馈归入 **已确认邻车吸收 / 已确认装置吸收 / 可能吸收 / 受限耗散 / 资料不足 / 已返送电网**,并给出每类的节能指标资格 |

## 回馈去向推导(新增)

制动回馈出现在车载控制器里,并不等于它真的替代了牵引供电:轻载时可能回灌邻车,夜间
也可能因吸收不足被制动电阻消耗。本功能量化"**产生的回馈**""**被系统吸收的回馈**"与
"**未被利用的回馈**",而不是把所有负向读数都宣称为线路节电。

### 两条路线(回答同一回馈量在指定运行范围内的去向与可利用范围)

| 路线 | 机制 | 特点 |
| --- | --- | --- |
| **A 守恒分配**(`CONSERVATION`) | 在同一**供电分区、同一时间窗**内用能量守恒分配回馈;结合分区边界计量(受电/返送)、邻车牵引需求、吸收装置计量/状态,区分五类去向 | 物理清晰、可审计、无需训练;多车密集时因果归因不明,未定份额较宽 |
| **B 学习预测**(`LEARNED`) | 用"计量完整时期"的守恒结论作训练标签,学习 *吸收比例 ~ 负荷/车距/坡度/装置状态* 的条件关系,对缺表运行给预测区间 | 日常估计更细;特殊调图/罕见故障下可能失配(以区间 + 外推警告表达) |

路线选择由**分区计量完整度、运行模式稳定性、节能核算容忍的不确定度**决定:
`GET /api/runs/{id}/regen/advice` 给出推荐路线与理由。

### 去向分类与节能指标资格

| 去向 | 含义 | 节能资格 |
| --- | --- | --- |
| 已确认邻车吸收 | 同分区内单一邻车牵引且边界闭合确认 | **计入**(已确认) |
| 已确认储能/回馈装置吸收 | 装置自有计量完整、状态可用 | **计入**(已确认) |
| 可能吸收 | 存在吸收可能但未确认(多车并发/边界不闭合/状态未知) | 条件计入,只能作潜在区间 |
| 受限耗散(制动电阻) | 边界闭合且无可吸收对象 | **不计入** |
| 资料不足 | 边界/装置资料不足以判定 | 不可判定,不得按 0 处理 |
| 已返送电网 | 边界计量确认返送 | 按上网结算口径,不计入本线牵引替代 |

守恒恒等式:`六类去向之和 = 产生回馈量`(逐运行、逐路线校验,见 `basis.consistency`)。

### 硬性约束(验收点)

- **邻车需求高不自动证明吸收**:需边界闭合且因果唯一(单邻车且邻车不回馈);多车并发或邻车同窗回馈时只能列为"可能吸收"。
- **不同供电分区不得跨界抵销**:每区独立守恒,汇总只是求和;返送只在产生区确认。
- **吸收装置状态晚到只形成新的去向版本**:不追溯改写历史结论,版本链 `version/supersedes_id` 完整保留。
- **回馈累计值重启不生成负耗散**:所有去向量夹紧非负;求值沿用"不直接相减、只累加单调段"规则。

## 一键启动(Docker Compose)

```bash
docker compose up --build
```

- 前端:http://localhost:8080
- 后端 API:http://localhost:8000(Swagger 文档 `/docs`,FastAPI 自带)
- 首次启动自动建库并注入五类验收场景演示数据;`SEED_ON_START=0` 可关闭重建

## 本地开发

前置:PostgreSQL 运行于 127.0.0.1:5432(库/用户/密码均为 `ccdev`)。

```bash
# 后端(端口 8000)
cd backend
python3 -m venv .venv
.venv/bin/pip install -i https://mirrors.aliyun.com/pypi/simple/ -r requirements.txt
.venv/bin/python -m app.seed          # 建表 + 注入验收场景数据
.venv/bin/uvicorn app.main:app --port 8000

# 前端(端口 5173,代理 /api -> 8000)
cd frontend
npm install
npm run dev                           # 打开 http://localhost:5173
```

## 测试账号

| 账号 | 密码 | 角色 |
| --- | --- | --- |
| admin | admin123 | 管理(可采集写入) |
| viewer | viewer123 | 只读 |

## 验证方式

### 自动化测试(已实际执行,31 项全部通过)

```bash
cd backend
.venv/bin/python -m pytest tests/ -q    # 使用独立测试库 ccdev_test
```

覆盖:计数器重启/倒退不相减、倍率切断、未知范围不冒充 0、五类场景的端到端结论、
迟到补传触发新版本且旧指标保留、双路径同范围结果一致、复盘链路完整;
以及**回馈去向**:守恒恒等式与无负耗散、每类去向的节能资格、夜间低负荷确认耗散、
早高峰多车只列可能吸收、单邻车因果唯一才确认邻车吸收、确认装置吸收、跨分区不抵销、
装置状态晚到生成新版本、缺表下路线A资料不足/路线B给预测区间并带外推警告、路线建议、
状态补报接口触发新版本、重复推导只增版本。

### 回馈去向验收场景(种子数据自带)

| 运行 | 场景 | 预期去向结论(路线A) |
| --- | --- | --- |
| RUN-2001 | 夜间低负荷 | 无邻车、边界闭合、装置在线未吸收 → **受限耗散 12 kWh**,已确认节电 0 |
| RUN-2002A | 早高峰多车 | 两车并发 → 邻车需求 7.5 入**可能吸收**,0 确认邻车吸收 |
| RUN-2003 | 跨分区 | P1 返送 3 在 P1 确认;P2 边界不闭合只列可能/资料不足;**返送不跨区抵销**,状态 PARTIAL |
| RUN-2004 | 装置故障(状态晚到) | v1(故障未到)可能吸收 10;v2(故障确认)**受限耗散 10**;两版并存 |
| RUN-2005 | 缺表 | 路线A 资料不足 10;路线B 学习历史给可能吸收预测区间,不外推为已确认 |
| RUN-2006A | 确认装置吸收 | 储能自有计量完整 → **已确认装置吸收 10**,可计入节电 |
| RUN-2006B | 确认邻车吸收 | 单邻车牵引且邻车不回馈 + 边界闭合 → **已确认邻车吸收 12** |

页面路径:运行与能量 →(点运行进详情)→「回馈去向推导」按钮;页面可切换 路线A/路线B、
查看六类去向与节能资格、分区守恒明细、学习模型依据、警告与去向版本历史。

### 五类账实验收场景(种子数据自带,可在页面直接核对)

| 运行 | 场景 | 页面验证点 |
| --- | --- | --- |
| RUN-1001 | 跨区段折返 | 运行详情:折返/停车切断、方向 UP→DOWN 翻转;核对 FULL,差异=线损+不可知 |
| RUN-1002 | 设备重启+换编组+倍率切换 | 片段切断原因含 DEVICE_RESET/CONSIST_CHANGE/MULTIPLIER_CHANGE;重启段质量 ESTIMATED、上界>下界 |
| RUN-1003 | 回馈受限 | 核对归因"回馈未上网">0,假设中 regen_accepted=false |
| RUN-1004 | 车载与供电表差异 | 差异 10.4 kWh 分解为线损/回馈未上网/不可知,归因之和=差异 |
| RUN-1005 | 补传读数 | v1 指标为"未知"(覆盖 37.5%),v2 闭合;指标页 diff_note 含"迟到补传";复盘页见历史水位 |
| RUN-1006 | 供电表缺失 | 核对 NONE,供电净受电"未知",不可判定原因 SUPPLY_COVERAGE_INSUFFICIENT |

页面路径:总览 → 运行与能量(点运行进详情,可切换 路径A/路径B)→ 指标服务 → 复盘回查。

### 手工 API 验证示例

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"admin123"}' | python3 -c "import sys,json;print(json.load(sys.stdin)['token'])")

# 双路径核对结论对比(同一运行范围)
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=RUN_ALIGNED"  -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/runs/1/reconciliation?method=GRID_ALIGNED" -H "Authorization: Bearer $TOKEN"

# 补传一条迟到读数 -> 自动生成新运行版本并重发指标
curl -s -X POST http://localhost:8000/api/readings -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '[{"device_serial":"OB-T01","sampled_at":"2026-10-21T14:05:00",
        "received_at":"2026-10-21T18:00:00","seq":99001,
        "traction_cum":0,"regen_cum":0,"aux_cum":0,"multiplier":1.0}]'

# 回馈去向:两条路线 + 路线建议(以 RUN-2001 为例,id 以 /api/runs 返回为准)
curl -s "http://localhost:8000/api/runs/8/regen"           -H "Authorization: Bearer $TOKEN"
curl -s "http://localhost:8000/api/runs/8/regen/advice"    -H "Authorization: Bearer $TOKEN"

# 吸收装置状态晚到 -> 受影响分区的已发布运行生成新的去向版本(不追溯改写)
curl -s -X POST http://localhost:8000/api/absorption-status -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"device_serial":"ESS-P1","state":"FAULT",
       "observed_at":"2026-11-12T01:00:00","received_at":"2026-11-12T05:00:00"}'

# 重新推导(再次调用只增版本):route=BOTH|CONSERVATION|LEARNED
curl -s -X POST "http://localhost:8000/api/runs/8/regen/derive?route=BOTH" -H "Authorization: Bearer $TOKEN"
```

## 目录结构

```
backend/app/
  models.py              # 领域模型:读数/运行/片段/能量/网格/核对/指标/配置版本/吸收装置/去向推导
  services/energy.py     # 计数器求差:重启倒退、倍率切断、不确定区间
  services/segmentation.py  # 运行切分(停车/折返/换编组/倍率/重启)
  services/evaluate.py   # 片段求值(路径A) + 网格重建(路径B)
  services/reconcile.py  # 账实核对:可比范围、差异归因、不可判定
  services/metrics.py    # 指标发布与迟到版本化
  services/regen.py      # 回馈去向推导:分区守恒分配(路线A)+ 条件关系学习(路线B)+ 路线建议
  api/routes.py          # REST API(登录鉴权)
  seed.py                # 账实验收场景 + 回馈去向验收场景
backend/tests/           # pytest:单元 + 场景端到端(含 test_regen.py)
frontend/src/views/      # 总览/采集/运行/指标/复盘/回馈去向 页面
docker-compose.yml       # 一键启动(db + backend + frontend)
```

## 已执行的验证结论

- `pytest`:31 项全部通过(测试库 ccdev_test,与开发库隔离),其中新增 13 项回馈去向验收。
- 种子数据执行成功:账实场景 + 回馈去向场景均可建库注入。
- 本地开发模式全链路冒烟:后端 uvicorn 下,登录、运行列表、回馈去向(regen/advice)、
  吸收装置状态补报接口均返回预期数据;前端 `npm run build` 构建通过。
- 本交付容器内无 docker 命令,Docker 镜像未实际构建;Compose 配置按标准
  多阶段构建编写,本地开发验证均使用原生运行时完成。
