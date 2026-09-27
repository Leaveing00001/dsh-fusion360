# dsh-fusion360

**用 DeepSeek Harness 驱动 Autodesk Fusion 360。** 在对话里查询模型、改参数、
导出文件、执行任意 Fusion API Python。

[English](README.md) · [安装](docs/install.md) · [实测数据](docs/verification.md) · [Fusion API 避坑](docs/fusion-api-notes.md)

---

> ## 🐋 这是 DeepSeek Harness 的 VibeCoding 产物
>
> 本项目**从头到尾由 DeepSeek Harness (DSH) VibeCoding 完成**。不是人写好之后
> 丢给 agent 收拾——是 agent 在对话里建起来的，而它驱动的那台真实 Fusion 360，
> 走的正是**它自己当时还在写的这座桥**。
>
> 具体来说，agent 做了这些：
>
> - 读了 DSH 自己的 `app.asar` 和 Fusion 自带的 Python 类型存根来学习这两套
>   API，而不是凭记忆下笔；
> - 写了 Fusion 插件、MCP 服务器，以及三套测试；
> - 把插件装进 Fusion、重启 DSH，然后**通过 MCP 层回头调用自己的工具**；
> - 在真实 Fusion 上驱动了 **15 个建模命令**，每一个都拿闭式解体积对账，而不是
>   满足于「调用没报错」——见[实测表](docs/verification.md)；
> - 撞上 **16 处「记忆里的 API 和现实不符」**，每一处都是照着真实报错改的。全部
>   写在 [Fusion API 避坑](docs/fusion-api-notes.md) 里。
>
> 错误是写下来而不是藏起来的，因为那才是有用的部分：它们里面**大多数在 Fusion
> API 里是静默失败**——倒角建出了一个健康特征却什么也没改，草图悄悄把圆吸收成了
> 孔。如果你自己写 Fusion API，那一页能给你省一天。
>
> README 里的字也是 agent 写的。这里的每个数字都是 agent 在这台机器上量出来的，
> 不是从文档里抄的断言。

---

## 它是什么

Fusion 360 **没有无头模式**。任何建模操作都需要一个运行中的 Fusion 实例，而
Fusion API 是单线程的——只能在 Fusion 的主线程上调用。所以一个「shell 出去跑
Python」的普通 MCP 服务器是行不通的。这是一座两段式桥：

```
┌─────────────┐   MCP over stdio    ┌──────────────────┐   JSON over     ┌────────────────────┐
│     DSH     │ ──────────────────► │  MCP server      │ ── loopback ──► │  Fusion 360        │
│ (dsh-mcp-   │   newline JSON      │  (本仓库,        │   TCP :27182    │  插件              │
│  client)    │ ◄────────────────── │   仅标准库)      │ ◄─────────────  │  (主线程)          │
└─────────────┘                     └──────────────────┘                 └────────────────────┘
```

MCP 服务器是 DSH 拉起的普通子进程。插件跑在 Fusion **内部**，用 `CustomEvent`
把 socket 请求交回 Fusion 主线程，在那里执行。

## 快速开始

```powershell
# 1. 把插件装进 Fusion 自己的 AddIns 目录（同时打开 runOnStartup）
powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1

# 2. 向 DSH 注册 MCP 服务器 —— 所有路径自动填好
python tools\install_dsh_mcp.py

# 3. 重启 DSH —— 新增的 profile 行不会被热加载
python tools\restart_dsh.py

# 4. 启动 Fusion 360，然后验证
```

让 DSH 调用 `mcp__fusion360__fusion_status`。正常的返回：

```json
{
  "bridge": "FusionDSHBridge",
  "bridgeVersion": "1.0.3",
  "fusionVersion": "2705.1.15",
  "user": "you@example.com",
  "host": "127.0.0.1",
  "port": 27182
}
```

完整步骤（含手动加载插件的方式和排错）：**[docs/install.md](docs/install.md)**。

### 验证它真的能用

[`examples/build_a_part.py`](examples/build_a_part.py) 会建一块 100 × 60 × 8 mm
的板，四角 R6 圆角，打 4 个 ⌀4.5 通孔，并在每个阶段自校验体积——
48000 → 47752.78 → **47243.84 mm³**。把它作为 `mcp__fusion360__fusion_run_python`
的 `code` 参数发过去，要么对上，要么抛异常。

这是区分「装好了」和「装坏了」最快的办法，也值得当作一份「API 尖角怎么处理」的
示例来读：按包围盒选轮廓、草图坐标系转换、倒角的 `isTangentChain` 陷阱，都在注释里。

## 工具

13 个工具，在 DSH 里暴露为 `mcp__fusion360__<名字>`。

| 工具 | 用途 |
|---|---|
| `fusion_status` | 连通性、Fusion 版本、登录用户 |
| `fusion_document_info` | 当前设计：单位、实体/参数数量、包围盒 |
| `fusion_list_bodies` | 每个实体的体积 (mm³)、面积 (mm²)、包围盒、材质 |
| `fusion_list_parameters` | 全部模型参数和用户参数 |
| `fusion_set_parameter` | 设置表达式并重算——参数化这条路 |
| `fusion_create_document` | 新建空设计 |
| `fusion_list_documents` | 当前打开的文档 |
| `fusion_open_document` | 按名字打开云端数据文件 |
| `fusion_save` | 保存当前文档 |
| `fusion_export` | 导出 STEP/IGES/STL/OBJ/F3D/SMT/SAT |
| `fusion_text_command` | 原始 Fusion 文本命令 |
| `fusion_reload_bridge` | 从磁盘重载插件并重启监听 |
| `fusion_run_python` | **在 Fusion 主线程上跑任意 Python** |

`fusion_run_python` 是让这套工具完整的那道逃生门：Fusion API 能做的，你都能做。
`adsk`、`app`、`ui`、`design`、`root` 已经预绑定，`stdout` 会被捕获，赋值给
`result` 就能返回值。

```python
sketch = root.sketches.add(root.xYConstructionPlane)
sketch.sketchCurves.sketchCircles.addByCenterRadius(
    adsk.core.Point3D.create(0, 0, 0), 2.0)   # 单位是厘米
extrude = root.features.extrudeFeatures
inp = extrude.createInput(
    sketch.profiles.item(0), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
inp.setDistanceExtent(False, adsk.core.ValueInput.createByReal(1.0))
extrude.add(inp)
result = {'bodies': root.bRepBodies.count}
```

> **单位：** 不管文档显示单位是什么，Fusion API 一律用**厘米和弧度**。只读工具会
> 帮你换算成毫米；传给 `fusion_run_python` 的代码没有这个待遇。

## 实测能做什么

下表每一项都是在真实 Fusion **2705.1.15** 上通过 `fusion_run_python` 驱动、并拿
闭式解体积对过账的。这是测量，不是冒烟测试。

| 命令 | 实测 | 解析值 |
|---|---|---|
| 草图：矩形、圆、圆弧、拟合样条 | 8 条曲线，3 个闭合轮廓 | — |
| 拉伸，新建实体 60 × 40 × 10 | 24000.00 mm³ | 24000 |
| 拉伸，合并——凸台 ⌀16 × 16 | +3216.99 mm³ | +3216.99 |
| 拉伸，切除贯穿——⌀10 | −785.40 mm³ | −785.40 |
| 圆角，4 条竖边 R3 | −77.26 mm³ | −77.26 |
| 倒角，4 条顶边 1 mm | −98.67 mm³，面数 6 → 10 | −100 |
| 放样，⌀40 → ⌀16，高 30 | 19603.54 mm³ | 19603.54 |
| 旋转，矩形绕 Z 轴 360° | 42411.50 mm³ | 42411.50 |
| 扫掠，⌀10 圆沿 50 mm 直线 | 3926.99 mm³ | 3926.99 |
| 抽壳，顶面开口，壁厚 2 mm | 7872.00 mm³ | 7872 |
| 孔 ⌀4.5 贯穿 8 mm | −127.23 mm³ | −127.23 |
| 矩形阵列 2 × 2 | 47491.06 mm³，4 个孔 | 47491.06 |
| 整件：板 + R6 + 4 × ⌀4.5 | 48000.00 → 47752.78 → 47243.84 | 相同 |
| 在实体面上建草图 + 盲孔切除 | 23547.61 mm³ | 23547.61 |

倒角比 `面积 × 长度` 的粗略算法少的那一点，是四条倒角交汇处的角部处理——在纯
长方体上同样复现（98.667 对 100 mm³），所以是几何，不是错误。

完整表格（含 MCP 层往返、STL/STEP 导出校验）：**[docs/verification.md](docs/verification.md)**。

## 写 Fusion API 之前请先看这个

Fusion 会在版本之间改名和删成员，而且失败是**静默**的。这里代价最大的四个：

- **`Sketches.add(face)` 会悄悄把面的边界投影成草图线。** 在板的顶面建的草图，
  `profiles.item(0)` 拿到的是**那块板**，不是你刚画的圆。选轮廓要按 `boundingBox`，
  永远不要按下标。
- **一个闭合曲线落在另一个里面，就会变成它的孔。** 同一个草图里在矩形内画个圆，
  矩形的轮廓就变成了圆环。挖孔要单独开草图。
- **圆角之后再倒角，必须 `isTangentChain=True`。** 传 `False` 时特征建出来了、
  `healthState` 健康、`errorOrWarningMessage` 为空，而**什么都不会变**。
- **`Face.geometry.normal` 分不出顶面和底面。** 一块板的两个平面都可能报 `+Z`
  ——它描述的是底层曲面，不是面的朝向。

16 条全部在 **[docs/fusion-api-notes.md](docs/fusion-api-notes.md)**，每条都附了它
产生的报错原文和修法。

## 目录

| 路径 | 作用 |
|---|---|
| `fusion_addin/` | 跑在 Fusion 360 **内部**的插件 |
| `mcp_server/fusion_mcp_server.py` | DSH 拉起的 MCP 服务器；仅标准库 |
| `dsh/cordis.patch.yml` | 把它注册进 DSH 的 profile patch 条目 |
| `docs/` | 安装指南、实测数据、Fusion API 避坑 |
| `examples/` | 一个带注释、可直接跑的零件建模示例 |
| `tools/` | 测试、探针、安装器、DSH 重启助手 |

## 开发

```powershell
# 对着假桥跑完整 MCP 交互——不需要 Fusion，不起子进程
python tools\smoke_test.py

# 插件 worker → 主线程交接，对着打桩的 adsk 包
python tools\addin_stub_test.py

# 对着真实 Fusion：建真几何、读回来、导出
python tools\verify_e2e.py

# 只做协议握手
python mcp_server\fusion_mcp_server.py --selftest

# 直接跟桥对话（完全绕过 DSH）
python tools\probe_bridge.py --cmd ping

# 改完插件后重新安装
powershell -ExecutionPolicy Bypass -File tools\install_addin.ps1
```

`probe_bridge.py` 有 `--args-file`，是因为 PowerShell 和 cmd 在把内联 JSON 当参数
传给原生可执行文件时都会破坏引号。

### 不碰 Fusion 界面重载插件

改完代码通常需要人在 Scripts and Add-Ins 对话框里点 **Stop** 再点 **Run**。
`fusion_reload_bridge`（或 `probe_bridge.py --cmd reload`）改成从磁盘重新执行插件
并重新绑定监听，所以迭代完全不需要 GUI。监听会断大约一秒；轮询到 `ping` 报出新的
`bridgeVersion` 即可。

自重载一旦失败，监听就没了，只有 GUI 能救回来——记住这条恢复路径。

## 配置

**端口** —— 改 `fusion_addin/bridge_config.json` 和 `dsh/cordis.patch.yml` 里对应
的 `FUSION_DSH_BRIDGE_PORT`。默认 `27182`。

**安全** —— 端点只绑 `127.0.0.1`，网络里够不着。但它仍然是一个**无认证、能在
Fusion 里执行 Python 的本地 RPC**；把「能访问本机 loopback」当作信任边界。不要把
端口暴露出去。

**日志** —— 插件会追加写 `fusion_addin/bridge.log`，同时通过 `app.log()` 写进
Fusion 自己的日志。插件起不来时先看这里。

## 设计说明

- **零第三方依赖。** MCP 服务器直接实现了协议，所以不需要 `pip install`，解释器
  可以是任何 Python 3.8+。
- **工具名是稳定的。** DSH 把它们渲染成 `mcp__fusion360__<工具>`，改名会破坏会话
  历史——把这些名字当作冻结的。
- **`failOnStartupError: false`** 让 Fusion 没开时 DSH 照样启动。工具依然出现，
  只是报告桥不可达。

## 许可证

MIT —— 见 [LICENSE](LICENSE)。

Fusion 360 与 Autodesk 是 Autodesk, Inc. 的商标。本项目与 Autodesk 无隶属关系，
也未获其背书。
