# 港口潮汐通航行程规划服务（Port Tidal Voyage Planner）

基于 **Python + FastAPI + SQLite** 的港口潮汐通航行程规划服务。

录入港口节点、单向航段（正整数分钟航时、静水深度、可选桥底净空）与逐分钟潮位后，
给定起终点、船舶参数（吃水 / 水面以上高度 / 安全余量）和不超过 1440 分钟的规划窗口，
返回**最早抵达**行程及每段起止分钟；抵达相同则按**航段编号序列字典序**择优。

## 规则模型

- 规划窗口为分钟 `[0, window]`，`1 <= window <= 1440`；船在节点**整分钟**出发、可整分钟等待。
- 航段耗时 `d` 分钟、于分钟 `t` 出发，则占用分钟 `t .. t+d-1`，于分钟 `t+d` 抵达；抵达须 `<= window`。
- 航段占用的**每一分钟** `m` 必须同时满足：
  - 水深约束：`静水深度 + 潮位(m) >= 吃水 + 安全余量`
  - 净空约束（仅当航段设有桥底净空）：`桥底净空 - 潮位(m) >= 水面以上高度 + 安全余量`
- 潮位按**修订（revision）**管理：每次提交潮位产生一个新修订号，旧修订数据不可变。
- 确认行程必须携带所据修订号，服务端在**同一事务**内复核（修订未过期 + 逐分钟重算可行性）；
  修订过期返回 `409` 且不保存；已确认行程永久保留当时结果快照。

## 快速启动

### Docker（一键启动并初始化数据库）

```bash
docker compose up --build
```

服务地址：**http://localhost:8000**（交互式文档 http://localhost:8000/docs ）。
SQLite 数据库文件位于命名卷 `tide_data` 的 `/data/tide_planner.db`，容器启动时自动建表（幂等）。

### 本地运行

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 配置

| 环境变量  | 默认值                    | 说明               |
| --------- | ------------------------- | ------------------ |
| `DB_PATH` | `./data/tide_planner.db`（Docker 中为 `/data/tide_planner.db`） | SQLite 数据库文件路径 |
| `PORT`    | `8000`                    | 由 uvicorn/compose 端口映射决定 |

### 测试

```bash
pip install -r requirements-dev.txt
pytest tests/ -q
```

## API 一览

| 方法 | 路径                       | 说明                                   |
| ---- | -------------------------- | -------------------------------------- |
| POST | `/nodes`                   | 录入港口节点 `{name}`                  |
| GET  | `/nodes`                   | 节点列表                               |
| POST | `/segments`                | 录入单向航段（航时/静水深度/可选净空） |
| GET  | `/segments`                | 航段列表                               |
| POST | `/tide-revisions`          | 提交逐分钟潮位，产生新修订             |
| GET  | `/tide-revisions/latest`   | 当前最新修订号                         |
| GET  | `/tide-revisions/{id}`     | 查看某修订的全部潮位                   |
| POST | `/plans`                   | 规划最早抵达行程（基于最新修订）       |
| POST | `/confirmations`           | 携带修订号确认行程（同事务复核）       |
| GET  | `/confirmations`           | 已确认行程列表                         |
| GET  | `/confirmations/{id}`      | 已确认行程快照（保留当时结果）         |
| GET  | `/health`                  | 健康检查                               |

错误约定：`404` 未知节点/修订/确认单；`409` 无潮位数据或修订过期冲突；
`422` 参数非法、潮位缺分钟、无可行行程、行程复核失败。

## 调用示例（curl）

```bash
B=http://localhost:8000

# 1. 录入节点
curl -s -X POST $B/nodes -H 'Content-Type: application/json' -d '{"name":"Alpha"}'
# {"id":1,"name":"Alpha"}
curl -s -X POST $B/nodes -H 'Content-Type: application/json' -d '{"name":"Bravo"}'
curl -s -X POST $B/nodes -H 'Content-Type: application/json' -d '{"name":"Delta"}'

# 2. 录入单向航段：正整数分钟航时、静水深度、可选桥底净空
curl -s -X POST $B/segments -H 'Content-Type: application/json' -d \
  '{"from_node":1,"to_node":2,"travel_minutes":30,"still_depth":2.4}'
curl -s -X POST $B/segments -H 'Content-Type: application/json' -d \
  '{"from_node":2,"to_node":3,"travel_minutes":40,"still_depth":3.0,"bridge_clearance":18.5}'

# 3. 提交逐分钟潮位（前 10 分钟低潮 0.0，其后 1.0）-> 产生修订 1
python3 -c 'import json; print(json.dumps({"levels":[{"minute":m,"level":0.0 if m<10 else 1.0} for m in range(1440)]}))' \
  | curl -s -X POST $B/tide-revisions -H 'Content-Type: application/json' -d @-
# {"revision_id":1,"point_count":1440,"created_at":"..."}

# 4. 规划：吃水 2.0m、水面以上高度 10.0m、安全余量 0.5m，窗口 240 分钟
curl -s -X POST $B/plans -H 'Content-Type: application/json' -d \
  '{"from_node":1,"to_node":3,"window_minutes":240,"draft":2.0,"height":10.0,"margin":0.5}'
# {"revision_id":1,"from_node":1,"to_node":3,"window_minutes":240,"arrival_minute":80,
#  "legs":[{"segment_id":1,"from_node":1,"to_node":2,"depart_minute":10,"arrive_minute":40},
#          {"segment_id":2,"from_node":2,"to_node":3,"depart_minute":40,"arrive_minute":80}]}
# 船在 Alpha 等到分钟 10（前 10 分钟潮位 0.0 不满足 2.4+潮位 >= 2.0+0.5），分钟 80 抵达 Delta。

# 5. 确认行程：携带所据修订号，服务端同一事务复核后落库
curl -s -X POST $B/confirmations -H 'Content-Type: application/json' -d \
  '{"revision_id":1,"from_node":1,"to_node":3,"window_minutes":240,
    "draft":2.0,"height":10.0,"margin":0.5,
    "legs":[{"segment_id":1,"depart_minute":10,"arrive_minute":40},
            {"segment_id":2,"depart_minute":40,"arrive_minute":80}]}'
# {"confirmation_id":1,"revision_id":1,"arrival_minute":80,"status":"confirmed"}

# 6. 潮位更正 -> 产生修订 2；再用旧修订 1 确认 -> 409 冲突且不保存
python3 -c 'import json; print(json.dumps({"levels":[{"minute":m,"level":0.8} for m in range(1440)]}))' \
  | curl -s -X POST $B/tide-revisions -H 'Content-Type: application/json' -d @-
curl -s -X POST $B/confirmations -H 'Content-Type: application/json' -d \
  '{"revision_id":1,"from_node":1,"to_node":3,"window_minutes":240,
    "draft":2.0,"height":10.0,"margin":0.5,
    "legs":[{"segment_id":1,"depart_minute":10,"arrive_minute":40},
            {"segment_id":2,"depart_minute":40,"arrive_minute":80}]}'
# HTTP 409 {"detail":"stale tide revision: itinerary based on revision 1, current revision is 2; not saved"}

# 7. 已确认行程保留当时结果
curl -s $B/confirmations/1
```

错误示例：

```bash
# 未知节点 -> 404
curl -s -X POST $B/plans -H 'Content-Type: application/json' -d \
  '{"from_node":1,"to_node":999,"window_minutes":240,"draft":2.0,"height":10.0,"margin":0.5}'
# {"detail":"unknown node id: 999"}

# 潮位缺分钟（窗口 240 但潮位只覆盖到分钟 99）-> 422
# {"detail":"tide level missing for 140 minute(s): 100, 101, 102, ..."}

# 无可行行程（水深恒不足）-> 422
# {"detail":"no feasible itinerary within the window"}
```

## 项目结构

```
app/
  main.py      # FastAPI 路由与确认事务
  planner.py   # 时间依赖最短路（最早抵达 + 航段编号序列择优）与行程复核
  db.py        # SQLite 连接、建表、BEGIN IMMEDIATE 事务
  schemas.py   # Pydantic 请求/响应模型
tests/
  test_api.py  # 12 个端到端测试（规划/择优/约束/报错/确认冲突）
Dockerfile
docker-compose.yml
requirements.txt / requirements-dev.txt
```
