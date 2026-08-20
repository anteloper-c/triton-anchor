# T8.2 依赖审计、SBOM 与开源许可合规——系统设计说明

> 文档定位：这是 T8.2 的唯一系统设计说明，用于解释需求边界、代码职责、输入输出、数据流、门禁语义和后续接入方式。组件事实以 `compliance/component-registry.json` 为准，许可证政策和风险接受分别以同目录对应 JSON 为准；本文件不复制第二份可执行规则。实现或接线发生变化时，必须在同一变更中更新本文件。

当前状态是“合规核心已本地实现并通过测试，正式自动扫描链和 release 晋级接线尚未完成”。文中必须继续区分：已经实现的核心能力、使用真实 Wheel 得到的技术验证，以及仍需 T3.8/T4.1 或人工审批才能关闭的事项。

## 这项工作解决什么问题

`triton-anchor` 的 Wheel 不只有项目自身的 Python 代码。它还分发或编入了 Triton、triton-linalg、f2reduce、LLVM/MLIR、pybind11 等代码，并依赖 Python 和若干系统运行库。T8.2 的目标是让每个候选发布产物都有一套可追溯的第三方成分证据：

1. 识别 Python、C++、子模块和主要构建环境中的直接及可识别间接依赖；
2. 为每个候选产物生成对应版本的 CycloneDX SBOM；
3. 核对许可证并生成 `THIRD_PARTY_NOTICES.md`；
4. 检查已知漏洞和新增依赖；
5. 在许可证不兼容或 High/Critical 漏洞未处置时阻止产物晋级。

T8.2 不重新实现 Wheel 构建、版本发布、签名或来源证明。它接收一份具体 Wheel及相应证据，给出技术合规结果；只有在受信发布流程明确指定该 Wheel 为正式候选时，结果才具有晋级语义。

## 系统边界与代码职责

系统按“取得产物、形成证据、合规判断、发布消费”分层。T8.2 核心只从文件系统读取调用者指定的 Wheel 和证据，不负责下载、构建或上传 Wheel：

```text
T3/T4、本地或 CI 取得 Wheel
              │  wheel_path
              ▼
wheel.py 固定文件身份 ──────────────┐
              │                    │ SHA256
源码/Wheel/构建/漏洞扫描报告         │
              ▼                    │
discovery.py 归一化并对账组件 ◄─────┘
              ▼
policy.py + core.py 判断证据、许可证、漏洞和晋级
              ▼
release.py 生成 SBOM、Notice、artifact link 和报告
              ▼
technical 仅报告结果；formal candidate 的非零退出码阻断晋级
```

| 位置 | 唯一职责 |
|---|---|
| `scripts/compliance/wheel.py` | 安全读取调用者传入的 Wheel，验证文件清单并生成不含本机路径的产物身份 |
| `scripts/compliance/declarations.py` | 比较 Python/CMake 等依赖声明变化，形成新增依赖准入输入 |
| `scripts/compliance/discovery.py` | 归一化源码、Wheel、Syft、OSV 和构建证据，并与组件登记表对账 |
| `scripts/compliance/model.py` | 校验登记表、策略、风险接受和目标等公共数据契约 |
| `scripts/compliance/policy.py` | 判断许可证、漏洞覆盖、风险接受、组件解析和依赖准入 |
| `scripts/compliance/release.py` | 从同一组件模型确定性生成 CycloneDX、Notice 和产物关联文件 |
| `scripts/compliance/core.py` | 编排一次产物评估，汇总相互独立的状态和最终阻断原因 |
| `scripts/compliance/osv_runner.py` | 对有精确可查询身份的实际组件运行 OSV-Scanner，并保留原始结果和覆盖证据 |
| `scripts/compliance/cli.py` | 提供 `admission`、`audit`、`artifact-evaluation`、`candidate` 和 Notice 入口 |
| `compliance/*.json` | 保存组件事实、许可证决策状态和经批准的风险接受，不在 Python 中复制名单 |
| `THIRD_PARTY_NOTICES.md` | 由登记表生成的规范归属声明；人工补证应先更新登记表再重新生成 |
| `.github/workflows/dependency-compliance.yml` | 当前仅验证合规核心；未来调用扫描链和正式门禁时仍复用上述入口 |

## 产物来源与评估上下文

合规核心不依赖 Wheel 的取得渠道。Wheel 可以来自本地 `dist/`、Local CI、手工 Delivery、CI artifact、服务器下载或已发布仓库；核心始终先以文件名、版本、平台标签和 SHA256 识别受评估产物。这里的“来源无关”只表示不绑定取得渠道，不表示可以省略 T8.2 原文要求的源码、C++、子模块和构建环境证据。

CLI 的 `--target` 表示由登记表中第一方产品组件声明的逻辑产物目标（当前为 `core-wheel`），不是 `linux_x86_64` 之类的平台标签；平台和 Python ABI 直接从 Wheel 文件名与元数据读取。未知目标以及只由 test/CI 依赖声明的目标会在生成库存前失败，不能通过空组件集合绕过检查。

同一套核心只有两个上下文，不维护两套规则：

| 上下文 | CLI | 含义 | `promotion_status` |
|---|---|---|---|
| `technical-artifact` | `artifact-evaluation` | 对任意具体 Wheel 做技术评估或重放，不宣称它是正式候选 | `not-applicable` |
| `formal-candidate` | `candidate` | 仅由受信 T3.8/T4.1 tag/release 流程对其明确指定的 Wheel 调用 | `pass` 或 `blocked` |

两个上下文对同一 Wheel和同一证据必须生成相同的 SBOM、Notice 和合规发现。技术评估缺少证据时仍生成可检查的部分输出，但 `execution_status`/`evidence_status` 会明确失败或不完整；不能把“成功写出 SBOM”解释为“允许发布”。正式候选除了执行和合规均通过，还要求构建证据声明 `same-build` 并绑定相同 Wheel SHA256。`post-hoc` 或 `unknown` 证据可以用于技术分析，但不能放行正式候选。

报告中的四类技术状态各自只回答一个问题：`execution_status` 表示工具和对账是否成功；`evidence_status` 表示六类必需证据是否齐全；`compliance_status` 表示许可证、Notice、漏洞和政策是否通过；`sbom_inventory_status` 表示组件库存与依赖图是否完整。`promotion_status` 仅表示 T8.2 是否阻断正式候选晋级，不等于整体发布批准，也不会反向改变前述技术状态。

当前尚未确认 T3.8/T4.1 的完成状态和正式候选生产入口。因此本地 Wheel、Local CI Wheel及手工 Delivery Wheel目前都只作为受评估产物；未来发布流程只需将其构建的确切 Wheel和同次构建证据传给相同 `candidate` CLI，不需要重写合规核心。

`candidate` 命令只能表达“受信发布调用者正在请求晋级判断”，不能自行证明调用者身份。正式候选的指定和发布入口属于 T3/T4；workflow 权限及不可信代码隔离属于 T8.3；签名、attestation 和 staging→production 同 digest 证明属于 T8.4。T8.2 只消费受信调用上下文，并校验当前 Wheel、证据和 SBOM 之间的一致性，避免伪造一份自报可信的候选清单。

## Wheel 路径解耦契约

调用者通过 `--wheel <path>` 告诉入口“文件现在在哪里”。`wheel.py` 读取完成后不会把绝对路径放入组件模型、SBOM 或门禁报告；下游只消费文件名、Wheel 元数据、标签、大小、文件清单和 SHA256。因此取得 Wheel 的方式和目录布局属于接入层，合规规则不应出现 `dist/`、服务器工作目录或某个 GitHub artifact 目录的硬编码。

| 输入变化 | 核心应有行为 |
|---|---|
| 同一字节、同一文件名，从 `dist/` 移到下载目录 | 产物身份和合规输出相同 |
| 同一路径中的文件被替换 | SHA256 改变，视为另一份产物；旧证据不得复用 |
| 同一字节被重命名 | 重新解析文件名中的版本/ABI/平台；不保证身份相同，因为文件名是 Wheel 格式的一部分 |
| 输出目录变化 | 只改变报告保存位置，不改变判断 |

“路径解耦”不等于“证据解耦”。源码扫描、Wheel 扫描、Syft、OSV 和构建证据仍必须对应同一份内容；构建证据中的产物 SHA256 必须与入口实际读取值一致，正式候选还必须声明 `same-build`。

当前实现已经显式校验 build evidence 的 Wheel SHA256；ScanCode 和 Syft 的原始格式尚未显式携带被扫描 Wheel 的 SHA256。正式自动化接入前，调用流程必须先冻结 Wheel，在扫描前后复核 SHA256，并用带该 SHA256 和原始报告摘要的证据清单把两类报告绑定到同一文件。在这项绑定落地前，相关重放只能算技术验证，不能把“报告路径相邻”当成同一产物的证明。

Wheel basename 不是父目录路径的一部分，而是 Wheel 格式的语义输入。当前实现从 basename 解析 Python/ABI/平台标签、从内部 `METADATA` 读取名称和版本，但尚未完整交叉校验 basename 与内部 `METADATA`/`WHEEL Tag`。因此现阶段必须保留构建器产生的原始 basename；完成正式候选接线前还应补齐该一致性校验，不能通过改名改变组件判断。

## 唯一组件证据模型

[`compliance/component-registry.json`](../../../compliance/component-registry.json) 是组件身份、已知用途和仓库证据的唯一人工登记表。SBOM、Notice 和门禁都应由“登记表 + 实际受评估产物发现 + 实际构建清单”生成，不再分别维护组件名单。

分类发生在 `usages` 上，而不是组件本身上。同一组件可以有多个互不混淆的用途。例如 Triton 的 Python 文件和头文件是 `distributed`，其 C++ 对象又是 `embedded`。

| 分类 | 含义 | 产品 SBOM | Notice |
|---|---|---:|---:|
| `distributed` | 组件文件作为源码、头文件、数据或独立库存在于受评估产物中 | 是 | 第三方组件需要 |
| `embedded` | 组件代码被编入受评估二进制，没有单独交付 | 是 | 第三方组件需要 |
| `runtime-external` | 不随产物交付，但运行时通过 Python 导入或动态链接需要 | 是 | 通常不需要 |
| `build-only` | 只用于生成受评估产物 | 进入 CycloneDX formulation | 否 |
| `test-only` | 只用于测试或兼容性验证 | 全量审计清单，不进入产品图 | 否 |
| `CI-only` | 只用于工作流调度、上传或状态回写 | T8.3/CI 供应链治理范围，不进入 T8.2 门禁 | 否 |

路径统一转成 POSIX 形式，再按 Python `fnmatch` 语义匹配。多个源码或分发归属规则命中时，最具体的路径优先，例如 `triton/third_party/f2reduce/**` 优先于 `triton/**`。

- `path_patterns` 只表示仓库源码归属，用于对账源码和许可证发现；
- `declaration_patterns` 只表示依赖声明或 import 出现的位置，不表示该文件归依赖组件所有，也不证明候选包含该组件；
- `artifact_patterns` 只用于受评估产物中可唯一确认归属的分发文件；
- `container_artifact_patterns` 只定位可能承载内嵌代码的共享二进制。单凭 `libtriton.so` 存在不能证明 LLVM、zlib、f2reduce 等分别已编入，必须再有组件级构建或链接证据。

任何扫描发现如果既没有唯一映射到组件，也没有带理由归入非产品范围，技术评估必须报告失败；如果该产物是正式候选，还必须阻断晋级。

## 当前仓库能够确认和不能确认的事实

当前基线是 RACE-org `main` 的 `9e00777b3df51cab3dd4addcc37cc6c959f6a712`。

| 组件或事实 | 当前结论 |
|---|---|
| triton-anchor | `setup.py` 为 0.2.0，`triton_anchor.__version__` 为 0.1.3；版本冲突未解决 |
| Triton | vendored commit `757b6a61e7df814ba806f498f8bb3160f84b120c` 与该提交官方 MIT LICENSE 可确认；仓库没有随 vendored tree 保存该文件，且源码存在其他许可证标记，正式 concluded license 与兼容性仍待审 |
| triton-linalg | 已将 vendored surface 与 Cambricon `master@00f51c2e48a943922f86f03d58e29f514def646d` 完整对账：114 个映射文件内容一致、4 个有本地内容修改，另有 117 个文件模式变化；官方 `LICENSE` 声明 Apache-2.0，`ACKNOWLEDGMENTS` 另列 Triton 与 triton-shared 的 MIT 归属。由于本地是派生树而非原样 revision，最终许可证结论、修改声明和随包归属仍待人工审查，详见 [`triton_linalg_license_audit.md`](triton_linalg_license_audit.md) |
| f2reduce | commit `949b91d022c001bbce19157f806013d37f05fbf5` 与 MIT 许可文件均可确认 |
| LLVM/MLIR | 官方项目声明 Apache-2.0 WITH LLVM-exception；CMake 确认会查找并链接，实际版本和静态/动态链接结果必须由候选构建清单与 ELF 证据确认 |
| pybind11 | 既是构建依赖，也有头文件模板编入原生模块；版本没有固定，不能标成 excluded |
| TTGPU 变体源码 | 头文件会被打入 Wheel，`TTGPU` 开启时源码还会编入原生模块；当前没有足够证据判断来源、所有权和许可证 |
| FlagGems | gitlink 固定在 `633d9111528d37e60d9804d2f4ac8d9e00c3af5c`，该提交官方 LICENSE 为 Apache-2.0；当前仅用于兼容性测试，不属于核心 Wheel |
| triton-shared | 已核对官方 MIT LICENSE，但当前只在文档/上游归属中出现，没有构建或打包证据，因此不能人为加入当前 Wheel 的 SBOM |
| zstd | 已检查的候选 ELF 在 `DT_NEEDED` 中出现 `libzstd.so.1`，因此该候选必须将 zstd 记录为 `runtime-external`；SONAME 只证明 ABI 约束，不证明包版本、源码来源或许可证结论，这些字段继续阻断直至补证和审查 |

原任务点名组件的固定证据、当前产品用途和待批准兼容性结论集中记录在 [`license_compatibility_review.md`](license_compatibility_review.md)，不由扫描器自动批准。

`install_requires=[]` 不能证明没有 Python 依赖。已分发的 Triton Python 代码存在 `setuptools`、NumPy、PyTorch 和 redis-py 等运行时导入，其中部分只在特定功能启用时需要。这些依赖仍要记录为 required 或 optional 的 `runtime-external`，不能因 Wheel 元数据为空而丢失；可选依赖在 SBOM 中标成 `optional`，不能伪装成候选环境中已经安装的确定版本。

当前可由源码确认的最小依赖图是：产物根组件直接依赖 Triton、triton-linalg、已分发的 TTGPU 头文件、LLVM/MLIR、pybind11、zlib 和 CPython；Triton 再依赖 f2reduce，并在相应功能启用时使用 setuptools、NumPy、PyTorch 和 redis-py。受评估 ELF 证据还会按实际 `DT_NEEDED` 激活 zstd 等外部运行库。具体产物中的 TTGPU 编译状态、LLVM/zlib 的静态或动态形态以及 zstd 的 ABI 约束，均必须由该次产物证据解析，不能从另一产物继承。

## 具体产物的技术评估与正式候选门禁

对每个 Wheel 独立执行以下最小链路：

1. **固定产物身份**：记录文件名、Wheel 元数据版本、Python ABI、平台标签和 SHA256。这只证明“正在评估哪份文件”，不会自动把它升级为正式候选。
2. **读取最小构建成分证据**：包含实际使用的主要构建组件及版本、会改变第三方成分的开关（例如 `TTGPU`），以及 LLVM/zlib/zstd 等组件的静态嵌入或外部运行时分类。证据同时声明 `same-build`、`post-hoc` 或 `unknown` 绑定并校验 Wheel SHA256；事后在另一台机器上读取同一个 Wheel 只能补充 ELF/文件证据，不能冒充原始构建工具版本。源码来源证明、构建镜像摘要、完整构建参数和可重现构建属于 T8.4；T8.2 可以引用这些现成证据，但不负责重新实现。
3. **收集发现**：安全读取 Wheel RECORD 和文件列表；分析 Python 依赖声明或 import；读取原生文件的链接依赖摘要；分别扫描源码和解包 Wheel 的许可证；读取子模块和主要构建组件清单。源码扫描结果按 `path_patterns` 归属，Wheel 扫描结果按 `artifact_patterns` 归属，二者不能混用。
4. **统一对账**：按路径、PURL/名称别名和链接证据合并到组件登记表。冲突保持 `unresolved`，不得静默选择任一结果。扫描器只能增加证据，不能覆盖组件范围或作出法律结论。
5. **生成交付物**：生成产品 CycloneDX SBOM、构建 formulation、`THIRD_PARTY_NOTICES.md`、漏洞报告和门禁报告。
6. **解释结果**：技术评估只报告 execution、evidence、compliance 和 SBOM inventory 状态，`promotion_status=not-applicable`；正式候选模式才以 promotion 结果决定能否晋级。工具执行成功不等于允许发布。

原生二进制不应在合规扫描宿主上直接加载执行。T8.2 可以复用 T3/T4 的隔离测试结果，但自身只解析候选文件和证据。

## 输入与输出契约

一次完整的产物评估只需要接入层提供下列运行时输入；路径可以改变，内容和相互绑定关系不能改变：

| 输入 | 作用 |
|---|---|
| 具体 Wheel | 确定版本、ABI、平台、文件列表和 SHA256 |
| source ScanCode JSON | 核对仓库源码许可证与路径归属 |
| unpacked-Wheel ScanCode JSON | 核对实际分发文件许可证与归属 |
| Wheel Syft CycloneDX JSON | 补充可识别包身份和成分发现 |
| OSV 派生结果及原始报告摘要 | 提供逐组件、逐版本漏洞发现和覆盖证据 |
| build evidence JSON | 确定主要构建组件、条件开关、链接形态及其与 Wheel 的绑定 |
| registry、policy、risk acceptances、规范 Notice | 提供受信组件事实和人工决策 |

核心输出为当前 Wheel 的 CycloneDX SBOM、`artifact-sbom-link.json`、合规报告和生成的 Notice。原始扫描报告由调用流程一并留存，核心不改写它们。对多个 Wheel 必须逐个调用并分别生成关联文件，不能用目录名或“同一个 release”推断它们具有相同成分。

## SBOM 与 Notice 的生成边界

产品 SBOM 的根组件必须关联当前 Wheel 的版本、平台和 SHA256。只有被本次产物证据激活的 `distributed`、`embedded`、`runtime-external` 用途进入产品组件和依赖图；主要 `build-only` 输入进入 CycloneDX formulation；`test-only` 和 `CI-only` 保留在审计清单，不污染产品依赖图。条件用途没有解析、库存证据缺失、存在未映射发现或产品组件无法从根依赖图到达时，composition 只能是 `incomplete`。

CycloneDX composition 只表达组件库存和依赖图是否完整，不表达许可证是否获准、漏洞是否已处置或调用者是否具有正式候选语义。因此同一份完整 SBOM 不会因为一个 High 漏洞或 `technical-artifact` 上下文而被错误降成 `incomplete`；这些问题分别体现在 `compliance_status` 和 `promotion_status` 中。正式候选的 SBOM库存不完整时仍然必须阻断，但阻断不反向定义 SBOM完整性。

仓库只维护一份由登记表生成的规范 `THIRD_PARTY_NOTICES.md`，覆盖该交付目标中可能分发或嵌入的第三方组件；一组件一节，至少包含名称、版本或 commit、来源 URL、经审查的完整 SPDX expression、版权信息和许可文本位置。产物评估从实际证据计算必需集合，验证它是规范 Notice 的子集，并检查规范文件与登记表逐字一致；不存在另一份候选专用组件清单。

当前已生成仓库根 `THIRD_PARTY_NOTICES.md` 基线，其中未解决字段明确显示为 `UNRESOLVED`；生成命令会以非零退出码报告这些缺口。因此它现在可用于漂移检查和审查，但还不是可随正式 release 交付的最终声明。

对于源码包、其他架构 Wheel 或后端插件，必须重新依据该产物的实际文件和链接结果计算 usage。不能把 Linux x86_64 Wheel 的 SBOM 原样复制给另一个产物。

## 许可证与漏洞门禁

[`compliance/license-policy.json`](../../../compliance/license-policy.json) 当前处于 `status=pending`，并要求 leader 与许可证审查人批准，因此所有 allow/deny 都只是待批准提案，不能用于正式放行。

许可证按**完整 SPDX expression 精确匹配**。未知、`NOASSERTION`、扫描器冲突、没有明确批准的 `OR`/`AND` 表达式均进入 review 并阻断；工具不能把表达式拆成若干 token 后自行推断兼容性。

FlagGems 等 `test-only` 组件仍要出现在全量许可证审计中，满足任务描述中的“核对”；但在没有进入候选产物、运行依赖或构建链的证据时，不默认阻断核心 Wheel。纯上传、状态回写等 `CI-only` Action 由 T8.3/CI 供应链治理处理，不扩张为 T8.2 产品候选门禁。

High/Critical 漏洞默认阻断。只有 [`compliance/risk-acceptances.json`](../../../compliance/risk-acceptances.json) 中状态为 `accepted`、与组件版本和漏洞编号匹配、限定到当前 release 版本或具体产物 SHA256、且未过期的人工批准记录可以放行。这样同一 release 的多个 ABI Wheel 不需要机械地重复审批；确实只影响某一产物时仍可用 SHA256 收窄范围。当前该文件没有任何接受记录。

“扫描完成且零漏洞”还不够。每个实际产品组件和主要构建组件必须有对应版本的漏洞覆盖记录：机器扫描记录包含扫描器、版本、日期和结果证据；工具不支持其生态时则需要带审查人、日期和证据的人工复核。缺少覆盖不能被解释为“没有漏洞”，ABI-only SONAME 也不能伪装成有精确包版本的自动查询。

`scripts/compliance/osv_runner.py` 只从同次构建证据中选择具有精确版本和可查询 PURL 的实际组件，生成临时查询清单并调用 OSV-Scanner。扫描器退出码 0（无发现）和 1（有发现）都表示执行成功；其他退出码、无效 JSON、空生态或无法唯一映射的包不获得覆盖。runner 原样保存扫描器 JSON 及 SHA256，另生成带工具版本、日期和逐组件 coverage 的派生输入供门禁消费。它不会把 ABI SONAME 或未知版本伪装成已扫描组件。

当前本地 Wheel 的技术重放已对构建组件 `setuptools 68.1.2` 完成真实查询，发现两个 High 级漏洞并形成“若被指定为正式候选则必须阻断”的结论。该结果证明技术评估能够消费真实发现，但它不是一次正式 promotion 决策，也尚未替代正式 CI 中固定工具版本、网络策略和长期报告留存。

来源解耦调整后，同一真实 Wheel和同一组证据分别以两次 `technical-artifact` 和一次 `formal-candidate` 重放：三份 SBOM、artifact link 与生成 Notice 均逐字节一致并通过 CycloneDX 1.7 校验。技术评估报告为 `promotion_status=not-applicable`；形式上的候选调用为 `blocked`，除已有合规阻塞外，还因为现有事后采集证据没有声明 `same-build`。这证明上下文只改变晋级语义，不改变技术事实。

以下情况至少有一项出现时，候选不得晋级：

- 合规策略仍未批准；
- 缺少候选构建清单或产物 SHA256；
- 发现未映射，或分发/嵌入组件的来源、版本、许可证结论未解决；
- SBOM 与 Notice 应覆盖的组件集合不一致；
- 存在未批准或不兼容的许可证表达式；
- 存在未处置的 High/Critical 漏洞，或组件没有可核验的漏洞覆盖；
- 组件扫描失败但报告仍试图给出通过结论。

## 原始 T8.2 要求对照

下表只描述当前本地实现，不代表任务已经验收：

| 原始要求 | 当前本地证据 | 尚未关闭 |
|---|---|---|
| 扫描 Python、C++、子模块和构建环境的直接及间接依赖 | 声明扫描、组件登记、源码/Wheel/ELF/构建证据归一化与依赖图已有测试；真实 Wheel 已发现 zstd 等系统依赖；可信 OSV runner 已完成本地实包验证 | CI 中固定版本的 ScanCode/Syft 和完整扫描链尚未接入；部分组件版本和来源未解析 |
| 为发布产物生成 CycloneDX 或 SPDX SBOM | 任意具体 Wheel 可按文件名、版本、平台和 SHA256 生成独立 CycloneDX 1.7及 artifact-SBOM link；SBOM库存完整性已与合规/晋级状态解耦 | 当前只对一个 Linux x86_64 Wheel 做技术验证；正式候选集合尚未由 T3.8/T4.1 指定 |
| CI 定期漏洞扫描并跟踪依赖更新 | `audit`、declaration delta/admission 和逐组件 OSV 查询核心已有负向测试与本地真实查询 | 定时入口、固定工具安装及认可候选证据保留尚未落地 |
| 核对指定组件许可证及兼容性 | 已固定官方许可证证据并形成 [许可证核对记录](license_compatibility_review.md)；triton-linalg 另有 tree 对账 | concluded license、组合兼容性和分发义务仍待 leader/许可证审查人批准 |
| 生成并维护 THIRD_PARTY_NOTICES.md | 根 Notice 由唯一登记表确定性生成，并有漂移测试 | 6 个分发/嵌入组件仍缺最终版本、许可文本或归属，当前文件不是 release-ready |
| 新增依赖许可证和高风险漏洞准入 | `admission` 对未登记声明、未批准许可、无覆盖或严重漏洞失败关闭 | 尚未接入 CI Security Gate；间接依赖变化还需可信 scanner delta 输入 |
| 每个候选关联对应版本 SBOM | 实包技术评估已生成 hash 绑定的一对一关联文件；同一核心支持正式 `candidate` 上下文 | T3.8/T4.1 的正式候选生产入口及晋级调用点尚未确认 |
| 覆盖直接、可识别间接和主要构建组件 | 产品依赖图与 CycloneDX formulation 分离，Triton→f2reduce 等间接关系可表达 | 实际构建工具版本、LLVM/MLIR 与若干运行库仍缺同次构建证据 |
| 严重漏洞有处置或批准记录 | 修复/隔离/升级要求处置证据；风险接受有范围、审批人和有效期校验；真实 OSV 查询已证明两个 setuptools High 漏洞会阻断 | `PYSEC-2025-49`、`PYSEC-2026-1918` 尚需升级、修复、隔离或批准风险接受；当前无已批准风险接受；ABI-only 依赖仍需人工 reviewed coverage |
| 所有分发第三方进入归属声明 | 候选必需集合与规范 Notice 做独立覆盖对账 | 当前 Notice 的未解决项仍会阻断 |
| 不兼容许可证或未处置严重漏洞阻断晋级 | 技术评估输出 `promotion_status=not-applicable`；正式 candidate gate 才以 promotion 状态返回非零，策略 pending 也阻断 | 还未挂到 T3.8/T4.1 的正式 promotion job，不能宣称已形成发布闭环 |

## CI 接入边界

核心逻辑、组件登记表、策略和规范 Notice 应在 `main` 维护唯一副本，其他流程只调用同一 CLI，不在 `scripts/ci` 或 `scripts/local_ci` 复制规则。当前 `CI_dev` 的正常 PR 重型构建主要由 `scripts/local_ci` 完成，它生成并安装一个测试 Wheel；`scripts/ci` 的完整 Delivery 构建主要是手工辅助链。两者都可以提供技术评估样本，但活跃程度或 Wheel 位于 `dist/` 都不能自动把它变成正式候选。

正式候选 Wheel 预期由未来 T3.8/T4.1 的 tag/release 流程产生。其实现状态和最终入口尚未确认，因此本阶段不把 blocking `candidate` 门禁硬接到 Local CI 或手工 Delivery，也不把每个 PR Wheel冒充发布候选。将来发布流程必须在生成正式 Wheel 后传入同次构建 Wheel、`same-build` 证据和全部扫描报告，再依据同一 CLI 的退出码阻断晋级。

当前的候选门禁预览放在能够实际读取 Wheel 和证据的本地或服务器流程中，不放入 GitHub 托管 runner。原因是 `workflow_dispatch` 只能传递路径字符串，不能把本机文件送入 runner；当前仓库也没有一个包含 Wheel 和全部扫描证据的可下载 artifact。为避免建立默认必失败的伪自动化入口，`.github/workflows/dependency-compliance.yml` 现阶段只运行合规核心测试。

### 当前 `dependency-compliance.yml` 的执行流

开发阶段向 `t82-dependency-compliance` 推送合规相关文件会触发该 workflow；未来变更进入 `main`/`CI_dev` 或以它们为 PR 基线时也会触发。路径过滤只关注 workflow 自身、`scripts/compliance/**`、`compliance/**` 和根 Notice。相同 ref 上的新运行会取消旧运行，避免重复消耗。分支名只用于当前 fork 验证，正式合入前应从 `push.branches` 删除，避免把个人开发分支固化到长期流程中。

```text
push / pull_request / workflow_dispatch
                ↓
checkout 当前提交（不拉子模块）
                ↓
安装 Python 3.11
                ↓
compileall scripts/compliance
                ↓
发现并运行 scripts/compliance/tests 下全部 test_*.py
                ↓
任一编译或断言失败 → job 失败；全部通过 → core-test 通过
```

这条流用测试文件把 T8.2 核心拆开验证：Wheel 输入和配置契约；组件发现与对账；依赖声明变化和 admission；OSV 覆盖及严重漏洞；SBOM、Notice、产物关联和 CLI 门禁。它的通过只表示这些实现契约没有回归，不表示已经扫描了当前仓库或某个真实 Wheel。

当前 workflow 没有下载或构建 Wheel，没有安装/运行 ScanCode、Syft、OSV-Scanner，没有调用 `admission`、`audit`、`artifact-evaluation` 或 `candidate`，也没有定时触发和报告上传。因此它是 T8.2 自动化的核心验证层，不是完整的 T8.2 执行流。后续应在证据生产和正式 release 入口明确后增加相应 job，而不是把占位报告塞进现有 `core-test`。

候选预览暂定 Wheel 路径为 `dist/triton_anchor-0.2.0-cp312-cp312-linux_x86_64.whl`，调用时可以将 `--wheel` 替换成执行机器可见的任意路径。证据目录暂定为 `t82-evidence/`，包含 `scancode-source.json`、`scancode-wheel.json`、`syft-wheel.cdx.json`、`osv-results.json` 和 `build-evidence.json`。本地或服务器使用同一入口：

```text
python -m scripts.compliance.cli candidate \
  --wheel <runner-visible-wheel-path> \
  --registry compliance/component-registry.json \
  --policy compliance/license-policy.json \
  --risk-acceptances compliance/risk-acceptances.json \
  --scancode-source t82-evidence/scancode-source.json \
  --scancode-wheel t82-evidence/scancode-wheel.json \
  --syft t82-evidence/syft-wheel.cdx.json \
  --osv t82-evidence/osv-results.json \
  --build-evidence t82-evidence/build-evidence.json \
  --notices THIRD_PARTY_NOTICES.md \
  --target core-wheel \
  --output-dir t82-output
```

这次手工 `candidate` 调用只验证正式门禁的技术行为，不构成正式发布批准。缺少任一必要证据时必须失败关闭，不生成占位报告冒充扫描成功。未来只替换 Wheel 与证据的生产步骤，不修改核心的任意路径接口。

原先放在普通 `ci.yml` 中的 `compliance-core` 已移到 `dependency-compliance.yml`，只在合规核心、策略、Notice 或对应测试变化时运行。它验证 Wheel-SBOM 一一关联、许可证和严重漏洞阻断、风险接受、Notice 对账及 CLI 退出码等 T8.2 判断逻辑。单元测试不是原任务单列的交付物或新任务节点，也不代替真实扫描；它是防止门禁代码修改后错误放行的最小实现保障。

四个入口共用同一套代码和策略，不再维护第二套“简化门禁”：

| 入口 | 用途 | 成功条件 |
|---|---|---|
| `admission` | PR 中新增或政策相关的依赖变更 | 声明差异已映射到登记表，版本/来源/许可证可审查，且有版本对应的漏洞覆盖和无未处置高风险漏洞 |
| `audit` | 定时或手工全量依赖审计 | 扫描执行完整，登记组件身份与许可证已解决，逐组件漏洞覆盖完整；已批准风险接受按 release 版本生效 |
| `artifact-evaluation` | 对任意具体 Wheel 做来源渠道无关的技术评估 | 执行和合规均通过时返回 0，但 `promotion_status` 始终为 `not-applicable` |
| `candidate` | 由受信 T3.8/T4.1 流程对正式候选做发布前门禁 | 全部技术检查通过、构建证据为 `same-build` 且绑定同一 SHA256，进程才返回 0 |

当前阶段只建立 `main` 侧可复用的组件、策略、审批数据和唯一核心，并使用真实 Wheel 做技术重放；尚未因此宣称 T8.2 完成。`dependency-compliance.yml` 当前只自动运行核心测试；它还没有自动取得 Wheel，也没有运行 ScanCode/Syft、PR 新依赖准入或定期复扫。T3.8/T4.1 的正式候选入口确定后，必须把 blocking candidate 调用接到正式 release Wheel 的同次构建流程。当前 `scripts/ci` Delivery 原型仅保留为本地实验参考，不作为正式接线结论。自动化必须消费这些文件，不能把同一规则复制成脚本中的第二份常量表。

## 重构不变量与恢复步骤

无论未来把调用入口接到 Local CI、GitHub Actions 还是独立 release 服务，重构时必须保持以下不变量：

1. 取得、构建或下载 Wheel 的代码在核心之外；核心只接收一个明确文件路径。
2. 每份 Wheel 以实际读取的 SHA256 和经校验的 Wheel 元数据标识，绝对路径不进入稳定输出。
3. 同一组件模型派生 SBOM、Notice 和门禁范围，不维护三份组件名单。
4. 扫描器提供发现和证据，不能自行批准许可证、风险接受或候选身份。
5. 缺报告、扫描失败、未映射发现和身份冲突必须显式失败，不能用空报告代替。
6. `execution`、`evidence`、`compliance`、`sbom_inventory` 和 `promotion` 状态保持独立，避免一个状态掩盖另一个问题。
7. `artifact-evaluation` 与 `candidate` 共用技术逻辑；只有后者增加正式候选、`same-build` 和非零阻断语义。
8. 不加载或 import 候选 Wheel 中的代码；原生依赖通过文件、ELF 和构建证据读取。
9. 每个候选产物独立生成 SBOM 和 SHA 关联文件；不能复用另一架构或另一文件的结果。
10. 自动化接入只能调用受信版本的核心和策略，候选代码不能修改自身门禁后放行。

如果以后需要从本说明重新搭建 T8.2，按以下顺序恢复即可：

1. 恢复 `compliance/*.json`、规范 Notice 和 `scripts/compliance/` 唯一核心，并先运行核心测试；
2. 由产物生产流程给出一个确切 Wheel 路径，不在核心中寻找“最新文件”；
3. 对该 Wheel 和对应源码生成五类扫描/构建输入，保留工具版本和原始报告；
4. 先运行 `artifact-evaluation` 验证对账、SBOM 和政策结果；
5. 只有 T3.8/T4.1 明确指定正式候选后，才在同一受信链路改用 `candidate` 并以退出码阻断晋级；
6. 将 SBOM、关联文件、报告和原始证据随对应 Wheel 留存，并用一个故意违规样例确认门禁确实会失败。

## 尚需上报的决策

以下事项不应由实现代码猜测；实验可以先给出数据，但在正式启用前需要 leader 或相应责任人确认：

| 决策 | 最迟确认点 |
|---|---|
| 哪个 T3.8/T4.1 job 有权把某个 Wheel 指定为正式候选，以及哪个晋级步骤受 T8.2 阻断 | 接入正式 `candidate` 前 |
| 首批正式候选包含哪些平台/ABI/源码包或插件，以及唯一版本来源 | 第一次正式候选评估前 |
| 许可证 expression 的 allow/deny/review 结论及 Notice 分发义务 | 将 policy 从 `pending` 改为批准前 |
| High/Critical 风险接受的批准人、适用范围和记录存放方式 | 第一次需要例外放行前 |
| 候选 Wheel、SBOM 和原始扫描证据的保存位置及保留期 | 正式 release 流程落地前，可在自动扫描开发期间确认 |
