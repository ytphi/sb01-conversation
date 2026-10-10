# 设计:生成 → sim2sim 验证 → 入库

> 目标:让 sb01(Unitree G1)**边说边动**。本设计描述如何把 TEXEDO 生成的动作,
> 经 SONIC sim2sim **动力学验证**后,晋级进一个"已验证动作库",供 `sb01_conversation.py`
> 在对话时挑选播放。**尚未实现,先出设计。**

## 0. 核心思路

- **sim2sim 是离线的"入库闸门",不进对话回路。** 对话时只消费已验证的库(低延迟、无未测动作摔机风险)。
- **对话时"挑选",不"现场生成"。** 现场生成太慢(N=1 约 1s,N=32 约 3.5s)且危险。

## 1. 硬约束(决定设计形态)

播放控制(`T` 播放 / `N`/`P` 切换 / `R` 复位)是**键盘专属**的;ZMQ 的 `command` topic
只有 `{start, stop, planner}`(见 `gear_sonic_deploy/.../input_command.hpp::CommandMessage`)。
**没有"喂一个动作、自动播、自动报结果"的无头接口。** 因此:

- **v1 只能半自动(human-in-the-loop)**:人开两终端、按键;脚本只做"开日志 + 算 pass/fail + 通过才入库"。
- 全自动批量验证需走 ZMQ 流式喂帧(`--input-type zmq`)或小改 C++ 把 play 挂到 command topic(v2)。
- **第一版只做上半身 + 站立**(挥手/指/点头/比划),排除带腿动作 → 风险与失败率都大幅下降。

## 2. 数据流与目录

```
text_to_motion_live.py ──► staging/<ts>_<slug>/   (候选:motion.npy + sonic_csv/ + prompt.json)
                                    │
                          validate_motion.py  ◄── 人在两终端跑 sim2sim,脚本采集实测日志
                                    │  算 pass/fail
                        ┌───────────┴───────────┐
                     PASS                       FAIL
                        │                        │
        verified_motions/<name>/           staging 保留 + 记 rejected.jsonl
          ├── motion.npy
          ├── sonic_csv/          (joint_pos/joint_vel/body_pos/body_quat + metadata.txt)
          ├── prompt.json         (prompt + 生成参数 + 选中候选)
          ├── measured/           ← 实测轨迹 CSV(base_quat/q/dq/motor_torque/motion_playing…)
          └── sim2sim_result.json ← pass/fail + 指标 + checkpoint + 日期
```

`sim2sim_result.json` + `measured/` 就是"这动作真能被 SONIC 跟踪执行"的证据
(区别于 TEXEDO 的 `R_dyn_success_prob` 只是快速猜测)。

## 3. pass/fail 判据(全部来自部署端实测 CSV)

| 指标 | 来源信号 | 判据(占位阈值,建库时标定) | 含义 |
|---|---|---|---|
| **没摔** | `base_quat` → 重力向量偏离竖直角度 | 全程 < ~30–45° | 部署端无绝对位置,只能用姿态倾角 |
| **跟踪误差** | 实测 `q`(29关节) vs 参考 `joint_pos.csv` | 上肢关节 平均/最大误差 < 阈值 | 手臂有没有真跟到位 |
| **播完** | `motion_playing` 标志 1→…→0 | 走完整段未中断 | 没中途崩 |
| (辅助) | `motor_torque` / `motor_temperature` | 无饱和 / 过热 | 动作是否吃力/危险 |

**pass = 没摔 AND 跟踪误差达标 AND 播完**(三者取 AND)。

## 4. `validate_motion.py`(新脚本)v1 职责

**脚本做的**(不碰键盘):
1. 接收一个 staging 候选目录,把其 `sonic_csv/` 拷/软链进 `gear_sonic_deploy/reference/my_motions/<slug>/`。
2. 确认部署端开启 CSV 日志(`enable_csv`),记录本次日志目录(`state_logger::getLogsDir` 输出)。
3. 提示人:起终端1模拟器 → 终端2 `deploy.sh sim` → `]` `9` → 播放 `<slug>`。
4. 人按完键、动作播完后,脚本读该次 `measured/` 日志 → 按 §3 算指标 → 打印 pass/fail。
5. pass → 晋级入库(拷 npy/csv/prompt + measured + 写 `sim2sim_result.json`);
   fail → 留 staging + 记 `rejected.jsonl`(留作反例分析)。

**人做的**:开两终端、按 `]`/`9`/`T`、看 MuJoCo 窗口(随时 `O` 急停)。

## 5. 与对话回路的衔接(终点)

`verified_motions/` = 一个"意图 → 已验证动作"目录。`sb01_conversation.py` 里让 Claude 回话时
**从清单里按名字挑一个**(不自由生成),真机播放:

```
ASR 转写 → Claude 生成回复文本 + 选一个 motion 名(来自 verified_motions/ 清单)
        → TTS 说话  ‖  同时把该已验证动作发给 G1(参考播放 / ZMQ 流)
```

## 6. 分期

- **v1**:半自动验证 + 入库(依赖 C++ 编译完成;只为建库,不进对话回路)。
- **v2**:ZMQ 流式喂帧(`--input-type zmq`)替代键盘播放,做成可批量、接近无头
  (仍需 ENTER 起流,或小改 C++ 把 play 挂到 command topic)。
- **v3**:接进 `sb01_conversation.py`,先 MuJoCo 演"说+挑库播放",再上真机(吊测→站立→上肢)。

## 7. 编译后第一时间要验的开放项

1. **怎么开 CSV 日志** + `getLogsDir` 写到哪(deploy.sh 里没显式设 → 是二进制的某 flag/默认)。
2. **摔倒判据**:部署端只测到 `base_quat`(无绝对高度)→ 确认倾角够不够,或改从
   `run_sim_loop.py`(MuJoCo 有真值高度)那侧记一份。
3. **模拟器无头**:`run_sim_loop.py` 有无 headless(给 v2 批量),或 `MUJOCO_GL=egl` 能否顶。
4. **ZMQ 流式 msg schema**(给 v2):`pose` topic / 端口 5556 / 字段与打包格式
   (见 `docs/source/tutorials/zmq.md`)。

---

相关:sim2sim 运行手册见 memory `sonic_sim2sim_validation`;集成全貌见 memory
`project_texedo_sonic_integration`。
