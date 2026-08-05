# 星港无人机调度系统设计说明

## 1. 项目背景

星港无人机调度系统（Starport Drone Orchestrator，简称 SDO）用于管理园区内的无人机配送任务。
系统接收配送订单，为订单选择合适的无人机和起降平台，并持续跟踪飞行状态。

系统的核心目标是：在保证安全的前提下，提高无人机利用率，并减少订单等待时间。

## 2. 核心组件

### 2.1 MissionGateway

`MissionGateway` 是任务入口，负责接收配送请求并验证参数。每个请求至少需要包含：

- 取货点坐标
- 送达点坐标
- 货物重量
- 期望送达时间

货物重量不得超过 12 千克。超过限制的请求会被拒绝，并返回错误码 `PAYLOAD_TOO_HEAVY`。

### 2.2 DroneAllocator

`DroneAllocator` 负责选择无人机。候选无人机必须同时满足以下条件：

- 当前状态为 `IDLE`
- 剩余电量不低于 35%
- 最大载重不小于货物重量
- 与取货点的距离不超过 8 千米

当多个候选无人机都满足要求时，系统优先选择预计到达取货点时间最短的无人机。
若预计时间相同，则选择剩余电量更高的无人机。

### 2.3 PadScheduler

`PadScheduler` 管理起降平台。每个平台同一时间只能服务一架无人机。
平台预约默认保留 90 秒；无人机未在规定时间内到达，预约自动释放。

紧急医疗任务可以抢占普通配送任务的平台预约，但不能抢占正在起飞或降落的平台。

### 2.4 FlightSentinel

`FlightSentinel` 是飞行安全监控组件，每 5 秒接收一次无人机遥测数据，包括位置、电量、速度和风速。

当连续 3 次未收到遥测数据时，任务进入 `SIGNAL_LOST` 状态。系统随后启动失联恢复流程：

1. 通过备用通信通道发送一次状态查询。
2. 等待 15 秒获取响应。
3. 若仍无响应，命令无人机返回最近的安全降落点。
4. 通知值班人员，并记录一条高优先级告警。

## 3. 任务状态机

配送任务按以下状态流转：

```text
CREATED -> VALIDATED -> ALLOCATED -> LOADING -> IN_FLIGHT -> DELIVERED
```

任何飞行中的任务都可能进入 `SIGNAL_LOST` 或 `EMERGENCY_LANDING`。任务成功送达后进入
`DELIVERED`，该状态不可逆。

如果在分配阶段 30 秒内没有找到可用无人机，任务进入 `WAITING_FOR_DRONE`。系统每隔 20 秒
重新尝试分配，最多尝试 6 次。全部失败后，任务标记为 `ALLOCATION_FAILED`。

## 4. 电量与充电策略

无人机完成任务后，系统根据剩余电量决定下一步动作：

- 电量高于 60%：返回空闲队列，可以继续接单。
- 电量在 30% 到 60% 之间：进入普通充电队列。
- 电量低于 30%：进入优先充电队列。
- 电量低于 15%：禁止执行新任务，并触发 `CRITICAL_BATTERY` 告警。

充电调度器名为 `AuroraChargeManager`。它每分钟重新计算一次充电优先级。

## 5. 配置示例

```yaml
allocator:
  minimum_battery_percent: 35
  maximum_pickup_distance_km: 8
  allocation_timeout_seconds: 30
  retry_interval_seconds: 20
  maximum_retries: 6

pad_scheduler:
  reservation_timeout_seconds: 90

flight_sentinel:
  telemetry_interval_seconds: 5
  missing_telemetry_threshold: 3
  recovery_wait_seconds: 15
```

生产环境通过环境变量 `SDO_CONFIG_PATH` 指定配置文件。若该变量不存在，系统默认读取
`config/starport.yml`。

## 6. 故障恢复原则

系统采用“安全优先、任务其次”的恢复原则。任何可能影响飞行安全的异常，都不能通过自动重试
无限延长任务。自动恢复最多执行两轮，仍未恢复时必须转人工处理。

所有状态变化都会写入 `mission_events` 事件表。事件记录包含任务编号、旧状态、新状态、发生时间
和原因。审计日志至少保留 180 天。

## 7. 常见问题

### 为什么一架电量为 34% 的无人机不能接单？

因为 `DroneAllocator` 要求候选无人机的最低电量为 35%。34% 低于安全阈值，因此不会进入候选集。

### 无人机失联后多久会启动返回安全点？

系统连续 3 次收不到每 5 秒一次的遥测后判定失联，然后通过备用通道查询并等待 15 秒。仍无响应
时，系统才会命令无人机返回最近的安全降落点。

### 哪个组件负责管理充电优先级？

`AuroraChargeManager` 负责计算充电优先级，并且每分钟重新计算一次。
