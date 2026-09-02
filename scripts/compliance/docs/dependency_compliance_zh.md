# T8.2 依赖审计、SBOM 与开源许可合规——系统设计说明

> 文档定位：这是 T8.2 的唯一系统设计说明，用于解释需求边界、代码职责、输入输出、数据流、门禁语义和后续接入方式。组件事实以 `compliance/component-registry.json` 为准，许可证政策和风险接受分别以同目录对应 JSON 为准；本文件不复制第二份可执行规则。实现或接线发生变化时，必须在同一变更中更新本文件。

当前状态是“提交 `a2b0553` 已把 T8.2 Hosted Runner Wheel 构建切换为固定的 `uv 0.12.8`，以同次 pip report 记录 uv、setuptools、wheel、pybind11 及可识别闭包，同时明确未选择的 pypa-build/pyproject-hooks；普通候选模拟继续把产品 SBOM 放入上传证据，手工/定时 audit 也用来源渠道无关的 `artifact-evaluation` 生成并上传本次 Wheel 的产品 SBOM 和关联文件；个人实验仓库的普通运行 `33464818866` 已通过 117 项核心测试、源码快照、真实 uv Wheel 构建/扫描、OSV、候选产品 SBOM检查和证据上传；手工 audit `33465696798` 已再次完成 uv 构建、扫描、OSV、完整 audit、产品 SBOM和上传，顶层因真实合规缺口为 failure，其中 `execution_status=pass`、`audit_status=blocked`、`compliance_status=fail`，执行问题、未映射发现、漏洞发现和漏洞覆盖缺口均为 0；上传证据已独立核实包含产品 SBOM、关联文件和报告；文档提交 `488b9da` 对应的最终 HEAD 运行 `33468894837` 也已通过核心测试、源码快照和真实 uv Wheel 候选模拟，并成功上传 Wheel 与源码证据。该手工 audit 保留 57 个合规阻断，不能表述为 T8.2 或候选合规完成；每周 `schedule` 仍只是在非默认分支实现，尚未发生周期触发；RACE 实际晋级步骤接线尚未完成”。文中必须继续区分：已经实现的核心能力、本地或远端模拟得到的技术验证，以及仍需实际流程或人工审批才能关闭的事项。

## 这项工作解决什么问题

`triton-anchor` 的 Wheel 不只有项目自身的 Python 代码。它还分发或编入了 Triton、triton-linalg、f2reduce、LLVM/MLIR、pybind11 等代码，并依赖 Python 和若干系统运行库。T8.2 的目标是让每个候选发布产物都有一套可追溯的第三方成分证据：

1. 识别 Python、C++、子模块和主要构建环境中的直接及可识别间接依赖；
2. 为每个候选产物生成对应版本的 CycloneDX SBOM；
3. 核对许可证并生成 `THIRD_PARTY_NOTICES.md`；
4. 检查已知漏洞和新增依赖；
5. 在许可证不兼容或 High/Critical 漏洞未处置时阻止产物晋级。

当前候选发布集合按“Wheel + GitHub tag 自动生成的 Source code ZIP/TAR.GZ + 对应版本文档”理解。T8.2 不重新实现 Wheel 构建、tag/release、签名或来源证明；它评估 Wheel 和源码快照这两类软件产物。接口说明、升级说明等文档作为同一 release 的交付附件留存，不因每份文档单独生成 SBOM；源码快照内的 tracked 文档会随整棵源码一起扫描，但独立上传的文档附件清单、版本和 hash 仍应由实际发布清单机制提供，签名和来源证明继续属于 T8.4。若独立文档引入可分发第三方内容，T8.2 消费其审查结论并纳入登记表和 Notice；当前尚未实现独立文档附件入口，不能宣称已经覆盖。

## 系统边界与代码职责

系统按“取得产物、形成证据、合规判断、发布消费”分层。T8.2 核心只从文件系统读取调用者指定的 Wheel 或 GitHub 源码归档及证据，不负责下载、构建或上传产物：

```text
现有 CI、本地或实际发布流程取得产物
              │
       ┌──────┴────────┐
       ▼               ▼
wheel.py          source_snapshot.py
Wheel SHA256       tag/commit + 规范树 digest
       └──────┬────────┘
              │ 源码/产物/构建/漏洞证据
              ▼
discovery.py 按当前 target 归一化并对账组件
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
| `scripts/compliance/source_snapshot.py` | 读取 GitHub ZIP/TAR.GZ，比较规范化路径、类型、执行位和内容，验证 tag/commit 与本地 Git 树绑定，并形成一个逻辑源码快照身份 |
| `scripts/compliance/declarations.py` | 扫描一棵源码树的 Python/CMake/子模块/vendored 声明，或比较两棵树形成新增依赖准入输入 |
| `scripts/compliance/discovery.py` | 归一化源码、Wheel、Syft、OSV 和构建证据，并与组件登记表对账 |
| `scripts/compliance/model.py` | 校验登记表、策略、风险接受和目标等公共数据契约 |
| `scripts/compliance/policy.py` | 判断许可证、漏洞覆盖、风险接受、组件解析和依赖准入 |
| `scripts/compliance/release.py` | 从同一组件模型确定性生成 CycloneDX、Notice 和产物关联文件 |
| `scripts/compliance/core.py` | 编排一次产物评估，汇总相互独立的状态和最终阻断原因 |
| `scripts/compliance/osv_runner.py` | 对构建/源码实际组件或登记表中新增、政策相关更新且有精确身份的组件运行 OSV-Scanner，并保留原始结果和覆盖证据 |
| `scripts/compliance/build_evidence.py` | 在同次 Wheel 构建后记录主要构建组件（含实际 C++ 编译器）、条件组件的 present/absent、Wheel SHA256 和 ELF 动态依赖 |
| `scripts/compliance/bootstrap_tools.sh` | 下载并校验固定版本扫描器；完整产物链安装 ScanCode/Syft/OSV，PR 准入只安装 OSV |
| `scripts/compliance/cli.py` | 提供 `admission`、`audit`、`artifact-evaluation`、`candidate` 和 Notice 入口 |
| `compliance/*.json` | 保存组件事实、许可证决策状态和经批准的风险接受，不在 Python 中复制名单 |
| `THIRD_PARTY_NOTICES.md` | 由登记表生成的规范归属声明；人工补证应先更新登记表再重新生成 |
| `.github/workflows/dependency-compliance.yml` | 运行核心回归测试、定时/手工审计和 PR 依赖准入，并在 anteloper 专用分支执行 Wheel 候选与 commit 源码快照模拟 |

## 产物来源与评估上下文

合规核心不依赖产物所在目录或下载渠道。Wheel 可以来自本地 `dist/`、Local CI、手工 Delivery、CI artifact、服务器下载或包仓库；源码 ZIP/TAR.GZ 可以位于任意调用者可见路径。这里的“来源无关”只表示不硬编码取得渠道，不表示可以省略 T8.2 原文要求的源码、C++、子模块和构建环境证据，也不表示正式候选可以缺少受信发布上下文。

CLI 的 `--target` 表示由登记表中第一方产品组件声明的逻辑产物目标，当前为 `core-wheel` 和 `source-snapshot`，不是 `linux_x86_64` 之类的平台标签。Wheel 的平台和 Python ABI 从文件名与元数据读取；GitHub 源码快照没有 Python ABI 或平台字段。未知目标以及只由 test/CI 依赖声明的目标会在生成库存前失败，不能通过空组件集合绕过检查。每次对账只读取当前 target 或 `*` 的 usage，避免 Wheel 发现误激活源码 target，或反向污染。

同一套核心只有两个上下文，不维护两套规则：

| 上下文 | CLI | 含义 | `promotion_status` |
|---|---|---|---|
| `technical-artifact` | `artifact-evaluation` | 对任意具体 Wheel 或源码快照做技术评估或重放，不宣称它是正式候选 | `not-applicable` |
| `formal-candidate` | `candidate` | 由实际具有产物晋级语义的受信调用流程对明确指定的产物调用 | `pass` 或 `blocked` |

两个上下文对同一产物和同一证据必须生成相同的 SBOM、Notice 和合规发现。技术评估缺少证据时仍生成可检查的部分输出，但 `execution_status`/`evidence_status` 会明确失败或不完整；不能把“成功写出 SBOM”解释为“允许发布”。正式 Wheel 还要求构建证据为 `same-build` 并绑定相同 Wheel SHA256；正式源码快照则要求 `reference_kind=tag`，且本地 Git checkout 已证明 tag 解析到声明 commit、两份 GitHub 归档的规范树与该 commit 一致。commit 分支模拟只能得到 `verified-commit`，不能冒充 `verified-tag-commit`。

报告中的四类技术状态各自只回答一个问题：`execution_status` 表示工具和对账是否成功；`evidence_status` 表示当前产物类型的必需证据是否齐全；`compliance_status` 表示许可证、Notice、漏洞和政策是否通过；`sbom_inventory_status` 表示组件库存与依赖图是否完整。`promotion_status` 仅表示 T8.2 是否阻断正式候选晋级，不等于整体发布批准，也不会反向改变前述技术状态。

T3.8 和 T4.1 可能不再继续实施，也不作为 T8.2 后续开发的前置条件。当前直接复用现有 CI 的 Wheel 构建点、显式产物路径和同次证据完成审计及门禁技术验证；本地 Wheel、Local CI Wheel、手工 Delivery Wheel 和 commit 源码归档仍只作为受评估或模拟产物。若以后出现实际发布流程，它只需把确切 Wheel及同次构建证据、以及同一 tag 自动生成的 ZIP/TAR.GZ 分别传给相同 `candidate` CLI，不需要重写合规核心。

`candidate` 命令只能表达“受信调用者正在请求晋级判断”，不能自行证明调用者身份。正式候选的指定和实际发布入口由现有或以后形成的发布流程负责，不阻塞 T8.2 先完成自身能力；workflow 权限及不可信代码隔离属于 T8.3；签名、attestation 和 staging→production 同 digest 证明属于 T8.4。T8.2 校验当前产物、证据和 SBOM 之间的一致性，但 GitHub 自动源码归档会按请求重新压缩，外层 ZIP/TAR.GZ SHA 不是长期来源证明；正式身份以 repository、tag、commit 和规范树 digest 为主，签名或 attestation 仍属于 T8.4。

## Wheel 路径解耦契约

调用者通过 `--wheel <path>` 告诉入口“文件现在在哪里”。`wheel.py` 读取完成后不会把绝对路径放入组件模型、SBOM 或门禁报告；下游只消费文件名、Wheel 元数据、标签、大小、文件清单和 SHA256。因此取得 Wheel 的方式和目录布局属于接入层，合规规则不应出现 `dist/`、服务器工作目录或某个 GitHub artifact 目录的硬编码。

| 输入变化 | 核心应有行为 |
|---|---|
| 同一字节、同一文件名，从 `dist/` 移到下载目录 | 产物身份和合规输出相同 |
| 同一路径中的文件被替换 | SHA256 改变，视为另一份产物；旧证据不得复用 |
| 同一字节被重命名 | 重新解析文件名中的版本/ABI/平台；不保证身份相同，因为文件名是 Wheel 格式的一部分 |
| 输出目录变化 | 只改变报告保存位置，不改变判断 |

“路径解耦”不等于“证据解耦”。源码扫描、Wheel 扫描、Syft、OSV 和构建证据仍必须对应同一份内容；构建证据中的产物 SHA256 必须与入口实际读取值一致，正式候选还必须声明 `same-build`。

当前实现已经显式校验 build evidence 的 Wheel SHA256。Hosted Runner 调用流程还会在 ScanCode/Syft 扫描前后复核该 SHA256；`compliance-report.json` 将实际读取的 Wheel 与两份原始报告摘要关联，现有 `evidence-manifest.json` 再复核并保存同一关联，不用“报告路径相邻”代替同一产物证明。该绑定只关闭扫描输入一致性缺口，不证明许可证已批准、风险已接受或产物可晋级。

Wheel basename 不是父目录路径的一部分，而是 Wheel 格式的语义输入。当前实现从 basename 解析名称、版本和 Python/ABI/平台标签，并要求它们分别与内部 `METADATA` 的 Name/Version 和 `WHEEL` 的完整 Tag 集合一致；压缩标签按其笛卡尔积展开后比较。调用者仍必须保留构建器产生的原始 basename，不能通过改名改变组件或平台判断。

## GitHub 自动源码快照契约

这里的“源码包”明确指 GitHub release 随 tag 自动提供的 `Source code (zip)` 和 `Source code (tar.gz)`，不是 `python -m build --sdist` 生成的 Python sdist，也不读取 `PKG-INFO`。两份归档是**一个逻辑源码快照的两种 representation**，因此只生成一份源码快照 SBOM：

```text
repository + tag + resolved commit + normalized Git-tree SHA256
                              │
                 ┌────────────┴────────────┐
                 ▼                         ▼
        ZIP：本次外层 SHA256       TAR.GZ：本次外层 SHA256
                 └────────────┬────────────┘
                              ▼
                  一份 source-snapshot SBOM
```

`source_snapshot.py` 去掉各归档唯一顶层目录后，逐项比较文件路径、普通文件/符号链接类型、执行位、大小和内容 SHA256；第二种表示必须是真实的 gzip tar，普通 tar 或其他压缩格式不能伪装成 `tar.gz`。若提供 `--source-repository-root`，它还通过本地 Git 解析 tag/commit，对该 commit 重新执行 `git archive` 做内容树对照，并从 Git tree 读取 `160000` gitlink；完整规范树 digest 同时覆盖归档文件和 gitlink commit。任一归档不同、tag 指向不同 commit、归档与 Git tree 不一致，或观察到的 gitlink commit 与组件登记值冲突都会失败。没有受信 checkout 时只能得到 `unverified` 的归档内容 digest，证据状态不完整，不能作为正式候选。

[GitHub 官方说明](https://docs.github.com/en/repositories/working-with-files/using-files/downloading-source-code-archives#stability-of-source-code-archives)归档会重新生成，压缩设置未来可能变化；所以外层 ZIP/TAR.GZ SHA256 记录的是“本次实际评估的下载表示”，不能替代稳定逻辑身份。`artifact-sbom-link.json` 同时保存两种 representation 的文件名、格式、大小和外层 SHA256，并把它们关联到以规范树 digest 为根的一份 CycloneDX。源码 SBOM 不出现 `python_tag`、`abi_tag` 或 `platform_tag`。

GitHub 自动归档不递归包含 FlagGems 子模块源码。当前源码 inventory 保留 `.gitmodules` 中的 name/path/URL，受信 Git tree 提供实际 FlagGems gitlink commit；URL 和 commit 分别与项目登记值对账。FlagGems 仍是 `test-only`，不能把其文件声明为这个源码快照已分发内容。当前链路会查询该精确 commit 的已知漏洞，但因归档没有子模块文件，FlagGems 自身直接/间接依赖的 manifests、Syft 和 ScanCode 实扫仍须由未来定时全量审计在受控 checkout 中完成，不能把一次 commit 查询称为子模块审计闭环。当前 commit 模拟下载真实 GitHub `zipball/tarball` 并验证到本地 commit；正式 release 必须改为 tag 归档并建立 `verified-tag-commit`，不能把 commit URL 下载物重命名为 tag 源码包。

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
| triton-linalg | 已将 vendored surface 与 Cambricon `master@00f51c2e48a943922f86f03d58e29f514def646d` 完整对账：114 个映射文件内容一致、4 个有本地内容修改，另有 117 个文件模式变化；当前 include/lib tree 分别固定为 `cb73c58b...` 与 `ab55e8b4...`。官方 `LICENSE` 声明 Apache-2.0，`ACKNOWLEDGMENTS` 另列 Triton 与 triton-shared 的 MIT 归属。tree id 只标识本地派生内容，最终许可证结论、修改声明和随包归属仍待人工审查，详见 [`triton_linalg_license_audit.md`](triton_linalg_license_audit.md) |
| f2reduce | commit `949b91d022c001bbce19157f806013d37f05fbf5` 与 MIT 许可文件均可确认 |
| LLVM/MLIR | `triton/cmake/llvm-hash.txt` 固定官方 commit `10dc3a8e916d73291269e5e2b82dd22681489aa1`，该提交声明 Apache-2.0 WITH LLVM-exception；同次构建证据以 commit 作为组件身份、另存 `llvm-config` 工具版本，静态/动态链接结果仍由 ELF 证据确认 |
| pybind11 | 既是构建依赖，也有头文件模板编入原生模块；`2998f15` audit 构建观察到 3.1.0，官方 PyPI 元数据和该 tag LICENSE 均声明 BSD-3-Clause。项目构建声明没有固定全局版本，所以未来候选仍必须使用同次构建证据，concluded license 仍待审 |
| Python Wheel 构建环境 | 当前工作流固定记录实际安装的 `build`、`pybind11`、`setuptools` 和 `wheel` 四个直接构建工具，并通过 pip 稳定版 installation report 解析这组固定版本的闭包；当前 Linux 构建可识别的 `build → packaging, pyproject-hooks` 和 `wheel → packaging` 关系进入构建 formulation 和 OSV 输入，未启用 extras 不形成依赖边。提交 `4a2507f` 已完成 Hosted Runner 验证；实际版本仍必须由每次同次构建证据提供，不能从开发机环境继承 |
| TTGPU 变体源码 | 17 个文件均能映射到固定 Triton `757b6a6` 文件且包含本地修改，当前两个子树已有 Git tree 身份；这只确认本地派生关系，修改权属、独立版本和许可证结论仍未解决，详见 [`ttgpu_provenance_audit.md`](ttgpu_provenance_audit.md) |
| FlagGems | gitlink 固定在 `633d9111528d37e60d9804d2f4ac8d9e00c3af5c`，该提交官方 LICENSE 为 Apache-2.0；其 `setup.py` 已确认 Triton/PyTorch/PyYAML、测试 extras 和 example extra 等直接约束，但没有锁文件可证明间接版本；当前仅用于兼容性测试，不属于核心 Wheel，详见 [`flaggems_dependency_audit.md`](flaggems_dependency_audit.md) |
| zlib | CMake 与当前 Wheel ELF 确认构建/外部运行时关系；官方 v1.3.1 LICENSE 可固定 Zlib 声明，但它不能代替候选实际 Ubuntu 源包、版本和许可证结论 |
| triton-shared | 已核对官方 MIT LICENSE，但当前只在文档/上游归属中出现，没有构建或打包证据，因此不能人为加入当前 Wheel 的 SBOM |
| zstd | 较早候选 ELF 曾在 `DT_NEEDED` 中出现 `libzstd.so.1`，2026-08-24 的 audit Wheel 则明确记录为 absent；每份候选必须按自身 ELF 分类。SONAME 只证明 ABI 约束，不证明包版本、源码来源或许可证结论 |

原任务点名组件的固定证据、当前产品用途和待批准兼容性结论集中记录在 [`license_compatibility_review.md`](license_compatibility_review.md)，不由扫描器自动批准。

`install_requires=[]` 不能证明没有 Python 依赖。已分发的 Triton Python 代码存在 `setuptools`、NumPy、PyTorch 和 redis-py 等运行时导入，其中部分只在特定功能启用时需要。这些依赖仍要记录为 required 或 optional 的 `runtime-external`，不能因 Wheel 元数据为空而丢失；可选依赖在 SBOM 中标成 `optional`，不能伪装成候选环境中已经安装的确定版本。

FlagGems 的固定 `setup.py` 进一步声明了 test-only 依赖面。当前登记表已加入此前缺失的 PyYAML、SciPy 和 Transformers，并把已登记的 Triton、PyTorch、setuptools、pytest、NumPy 与 CPython 关联到 FlagGems；这不会把它们加入核心 Wheel 产品 SBOM。由于子模块没有 lock，具体测试环境及间接依赖仍必须由实际解析证据补齐，不能把源码约束转换成任意选定版本。

当前可由源码确认的最小依赖图是：产物根组件直接依赖 Triton、triton-linalg、已分发的 TTGPU 头文件、LLVM/MLIR、pybind11、zlib 和 CPython；Triton 再依赖 f2reduce，并在相应功能启用时使用 setuptools、NumPy、PyTorch 和 redis-py。受评估 ELF 证据还会按实际 `DT_NEEDED` 激活 zstd 等外部运行库。具体产物中的 TTGPU 编译状态、LLVM/zlib 的静态或动态形态以及 zstd 的 ABI 约束，均必须由该次产物证据解析，不能从另一产物继承。

## 具体产物的技术评估与正式候选门禁

对每个 Wheel 独立执行以下最小链路：

1. **固定产物身份**：记录文件名、Wheel 元数据版本、Python ABI、平台标签和 SHA256。这只证明“正在评估哪份文件”，不会自动把它升级为正式候选。
2. **读取最小构建成分证据**：包含实际使用的主要构建组件及版本、会改变第三方成分的开关（例如 `TTGPU`），以及 LLVM/zlib/zstd 等组件的静态嵌入或外部运行时分类。调用层按实际选择的前端固定四个直接根：`pypa-build` 对应 `build`、`pybind11`、`setuptools`、`wheel`，`uv` 对应 `uv`、`pybind11`、`setuptools`、`wheel`；保存同次 pip installation report 中解析出的闭包和依赖边，不扫描 runner 的其他 Python 包。未被选中的替代前端，以及报告闭包中未解析出的已登记条件组件，明确记录为 absent，不能产生假设性库存或漏洞覆盖要求。证据同时声明 `same-build`、`post-hoc` 或 `unknown` 绑定并校验 Wheel SHA256；事后在另一台机器上读取同一个 Wheel 只能补充 ELF/文件证据，不能冒充原始构建工具版本。源码来源证明、构建镜像摘要、完整构建参数和可重现构建属于 T8.4；T8.2 可以引用这些现成证据，但不负责重新实现。
3. **收集发现**：安全读取 Wheel RECORD 和文件列表；分析 Python 依赖声明或 import；读取原生文件的链接依赖摘要；分别扫描源码和解包 Wheel 的许可证；读取子模块和主要构建组件清单。源码扫描结果按 `path_patterns` 归属，Wheel 扫描结果按 `artifact_patterns` 归属，二者不能混用。
4. **统一对账**：按路径、PURL/名称别名和链接证据合并到组件登记表。冲突保持 `unresolved`，不得静默选择任一结果。扫描器只能增加证据，不能覆盖组件范围或作出法律结论。
5. **生成交付物**：生成产品 CycloneDX SBOM、构建 formulation、`THIRD_PARTY_NOTICES.md`、漏洞报告和门禁报告。
6. **解释结果**：技术评估只报告 execution、evidence、compliance 和 SBOM inventory 状态，`promotion_status=not-applicable`；正式候选模式才以 promotion 结果决定能否晋级。工具执行成功不等于允许发布。

对一个 GitHub 源码快照执行对应但不伪造构建语义的链路：

1. 同时读取 ZIP 和 gzip TAR，建立 repository、reference、commit、包含 gitlink 的规范树 digest 和两份外层 SHA256；正式模式必须验证 tag→commit→Git tree。
2. 只扫描一份已经证明内容等价的解包树；分别运行 source ScanCode、source Syft、依赖声明 inventory 和源码专用 OSV 精确 commit 查询，不重复扫描两种压缩格式。Syft 对 `.github/workflows` 识别出的 `actions/*` 作为带原因的 `CI-only` 发现留存，但不进入 T8.2 产品 SBOM；它们的供应链治理属于 T8.3。
3. `source-snapshot`、`scancode-source`、`syft`、`dependency-inventory`、`osv` 五类证据都必须成功。源码快照没有发生编译，因此不接受 Wheel build evidence，也不声明 `same-build`。
4. 已有精确 Git commit 且实际出现在快照中的产品、构建和测试组件进入 OSV custom input，纯 `CI-only` 组件除外；因此 FlagGems gitlink 会被查询但仍不进入产品 SBOM。OSV 成功执行后为所有实际查询组件记录覆盖，即使零漏洞结果返回空 `results`。版本未解析或没有可查询身份的组件仍保留明确的漏洞覆盖缺口，不能用“扫描无发现”替代覆盖证明。
5. 从同一 target 对账结果生成一份 source-snapshot CycloneDX、两归档关联文件、唯一规范 Notice、合规报告和原始证据清单；归档根目录中随包的 `THIRD_PARTY_NOTICES.md` 还必须与该规范 Notice 的 UTF-8/LF 字节完全一致。

原生二进制不应在合规扫描宿主上直接加载执行。T8.2 可以复用现有 CI 或其他流程已有的隔离测试结果，但自身只解析候选文件和证据。

## 输入与输出契约

一次完整的产物评估只需要接入层提供下列运行时输入；路径可以改变，内容和相互绑定关系不能改变：

| 输入 | 作用 |
|---|---|
| 具体 Wheel | 确定版本、ABI、平台、文件列表和 SHA256 |
| GitHub source ZIP + tar.gz、repository、tag/commit、本地 Git checkout | 确定一个源码快照的归档文件与 gitlink 规范树身份、两种下载表示以及 tag/commit 绑定 |
| source ScanCode JSON | 核对仓库源码许可证与路径归属 |
| unpacked-Wheel ScanCode JSON | 核对实际分发文件许可证与归属 |
| Wheel Syft CycloneDX JSON | 补充可识别包身份和成分发现 |
| OSV 派生结果及原始报告摘要 | 提供逐组件、逐版本漏洞发现和覆盖证据 |
| build evidence JSON | 确定主要构建组件、条件开关、链接形态及其与 Wheel 的绑定 |
| source dependency inventory JSON | 核对源码快照中的 Python import、pyproject、CMake、vendored 目录和子模块声明 |
| registry、policy、risk acceptances、规范 Notice | 提供受信组件事实和人工决策 |

核心输出为当前软件产物的 CycloneDX SBOM、`artifact-sbom-link.json`、合规报告和生成的 Notice。原始扫描报告由调用流程一并留存，核心不改写它们。每个 Wheel 必须逐个调用并生成自己的 SBOM；一个 GitHub 源码快照的 ZIP/TAR.GZ 内容等价并共享一份源码 SBOM，但两份外层 SHA 都必须出现在关联文件中，归档内部 Notice 也必须与本次模型生成值一致。不能用当前工作树的外部 Notice 替代旧 tag 中实际分发的文件，也不能用目录名或“同一个 release”推断不同软件产物具有相同成分。独立接口说明、升级说明等 release 附件当前不进入此软件产物 CLI，其清单和 hash 仍待实际发布清单机制接线。

## SBOM 与 Notice 的生成边界

产品 SBOM 的根组件必须关联当前逻辑产物身份：Wheel 使用版本、Python ABI、平台和文件 SHA256；源码快照使用版本、repository、tag/commit 和包含 gitlink 的规范树 digest。只有被本次 target 证据激活的 `distributed`、`embedded`、`runtime-external` 用途进入产品组件和依赖图；主要 `build-only` 输入进入 CycloneDX formulation；`test-only` 和 `CI-only` 保留在审计清单，不污染产品依赖图。条件用途没有解析、库存证据缺失、存在未映射发现、库存来源出现版本/来源/声明冲突，或产品组件无法从根依赖图到达时，composition 只能是 `incomplete`。

CycloneDX composition 只表达组件库存和依赖图是否完整，不表达许可证是否获准、漏洞是否已处置或调用者是否具有正式候选语义。因此同一份完整 SBOM 不会因为一个 High 漏洞或 `technical-artifact` 上下文而被错误降成 `incomplete`；这些问题分别体现在 `compliance_status` 和 `promotion_status` 中。正式候选的 SBOM库存不完整时仍然必须阻断，但阻断不反向定义 SBOM完整性。

仓库只维护一份由登记表生成的规范 `THIRD_PARTY_NOTICES.md`，覆盖所有正式发布 target 中可能分发或嵌入的第三方组件并按 component ID 去重；一组件一节，至少包含名称、版本或 commit、来源 URL、经审查的完整 SPDX expression、版权信息和许可文本位置。具体产物评估从实际证据计算本 target 的必需集合，验证它是规范 Notice 的子集，并检查规范文件与登记表的全 target 并集逐字一致；不存在 Wheel Notice、源码 Notice 或候选专用组件清单三份并行事实。

当前已生成仓库根 `THIRD_PARTY_NOTICES.md` 基线，其中未解决字段明确显示为 `UNRESOLVED`；生成命令会以非零退出码报告这些缺口。因此它现在可用于漂移检查和审查，但还不是可随正式 release 交付的最终声明。

当前 GitHub tag 源码快照已有独立 target 和 SBOM 模型。对于其他架构/ABI Wheel、未来后端插件或以后新增的产物类型，仍必须依据实际文件和链接结果计算 usage；不能把 Linux x86_64 Wheel 或源码快照的 SBOM 原样复制给另一产物。

## 许可证与漏洞门禁

[`compliance/license-policy.json`](../../../compliance/license-policy.json) 当前处于 `status=pending`，并要求 leader 与许可证审查人批准，因此所有 allow/deny 都只是待批准提案，不能用于正式放行。

许可证按**完整 SPDX expression 精确匹配**。未知、`NOASSERTION`、扫描器冲突、没有明确批准的 `OR`/`AND` 表达式均进入 review 并阻断；工具不能把表达式拆成若干 token 后自行推断兼容性。

FlagGems 等 `test-only` 组件仍要出现在全量许可证审计中，满足任务描述中的“核对”；但在没有进入候选产物、运行依赖或构建链的证据时，不默认阻断核心 Wheel。纯上传、状态回写等 `CI-only` Action 由 T8.3/CI 供应链治理处理，不扩张为 T8.2 产品候选门禁。

High/Critical 漏洞默认阻断。只有 [`compliance/risk-acceptances.json`](../../../compliance/risk-acceptances.json) 中状态为 `accepted`、与组件版本和漏洞编号匹配、限定到当前 release 版本或具体产物 SHA256、且未过期的人工批准记录可以放行。这样同一 release 的多个 ABI Wheel 不需要机械地重复审批；确实只影响某一产物时仍可用 SHA256 收窄范围。当前该文件没有任何接受记录。

“扫描完成且零漏洞”还不够。每个实际产品组件和主要构建组件必须有对应版本的漏洞覆盖记录：机器扫描记录包含扫描器、版本、日期和结果证据；工具不支持其生态时则需要带审查人、日期和证据的人工复核。缺少覆盖不能被解释为“没有漏洞”，ABI-only SONAME 也不能伪装成有精确包版本的自动查询。

`scripts/compliance/osv_runner.py` 只从同次构建证据中选择具有精确版本和可查询 PURL 的实际组件，生成临时查询清单并调用 OSV-Scanner。扫描器退出码 0（无发现）和 1（有发现）都表示执行成功；其他退出码、无效 JSON、空生态或无法唯一映射的包不获得覆盖。runner 原样保存扫描器 JSON 及 SHA256，另生成带工具版本、日期和逐组件 coverage 的派生输入供门禁消费。它不会把 ABI SONAME 或未知版本伪装成已扫描组件。

当前本地 Wheel 的技术重放已对构建组件 `setuptools 68.1.2` 完成真实查询，发现两个 High 级漏洞并形成“若被指定为正式候选则必须阻断”的结论。该结果证明技术评估能够消费真实发现，但它不是一次正式 promotion 决策，也尚未替代正式 CI 中固定工具版本、网络策略和长期报告留存。

来源解耦调整后，同一真实 Wheel和同一组证据分别以两次 `technical-artifact` 和一次 `formal-candidate` 重放：三份 SBOM、artifact link 与生成 Notice 均逐字节一致并通过 CycloneDX 1.7 校验。技术评估报告为 `promotion_status=not-applicable`；形式上的候选调用为 `blocked`，除已有合规阻塞外，还因为现有事后采集证据没有声明 `same-build`。这证明上下文只改变晋级语义，不改变技术事实。

2026-08-20 的 [fork 首次 Hosted Runner 模拟](https://github.com/anteloper-c/triton-anchor/actions/runs/32356106386) 自动构建了真实 Wheel，源码 ScanCode、Wheel ScanCode、Syft、OSV 和证据上传均执行成功。候选评估暴露出 Syft 的 202 个 `type=file` 条目被误当作包，以及 37 个合规文档/测试中的许可证示例被当作产品发现，共形成 239 个未映射项。实现随后只过滤非包类型和自扫描材料，保留“真正未映射依赖必须阻断”。

[修正后的第二次 Hosted Runner 模拟](https://github.com/anteloper-c/triton-anchor/actions/runs/32359594017) 自动构建 `triton_anchor-0.2.0-cp312-cp312-linux_x86_64.whl`（SHA256 `ef3a1a3aeb78d826cca079925f628f8d2969590f7f508dd805716abc3da5c8bd`）。Wheel、build evidence、SBOM link 和报告中的 SHA256 一致；`execution_status=pass`、`evidence_status=complete`、未映射项和执行问题均为 0。真实 `candidate` 仍返回 1，`compliance_status=fail`、`sbom_inventory_status=incomplete`、`promotion_status=blocked`，无发布动作的模拟晋级 job 被跳过。证据还发现本次 ELF 无 zstd、构建未选 uv 时仍会产生“用途未分类”误阻断；当前同次构建证据因此显式记录条件用途的 `present` 或 `absent`，缺少分类仍保持阻断，明确 absent 的组件不会进入 SBOM、Notice 或漏洞范围。

2026-08-24 的 [双产物 Hosted Runner 验证](https://github.com/anteloper-c/triton-anchor/actions/runs/32705703743) 针对提交 `ee258be` 同时通过 109 项核心测试、源码快照技术模拟和 Wheel 候选模拟。源码 job 从 GitHub API 重新下载该 commit 的 ZIP/TAR.GZ，验证双表示与 Git tree，运行固定版本 ScanCode、Syft、依赖声明 inventory 和 OSV，再生成并上传源码 SBOM、Notice、关联文件、报告和原始证据；约 3.2 MB 的源码证据包与约 141.8 MB 的 Wheel/候选证据包均已留存 14 天。源码调用保持 `promotion_status=not-applicable`；Wheel 调用正确观察到真实合规阻断，所以无发布动作的模拟晋级仍被跳过。整个 run 成功表示技术链按预期执行和阻断，不表示候选已通过合规或正式 release 已获批准。

提交 `a897db0` 的 [手工 audit](https://github.com/anteloper-c/triton-anchor/actions/runs/32721221161) 完成源码/Wheel 扫描、同次构建证据和 OSV：`execution_status=pass`、无执行问题或未映射发现，`compliance_status=fail`、`audit_status=blocked`；证据中有 25 项组件事实不完整、27 项许可证待审、9 项漏洞覆盖缺口和 1 项政策未批准。约 141.8 MB 的 audit 证据包成功上传。随后 [非 audit 回归](https://github.com/anteloper-c/triton-anchor/actions/runs/32722781370) 的核心测试、Wheel 候选模拟和源码快照模拟均成功，模拟晋级因候选被正确阻断而跳过。

个人仓库 [PR #1](https://github.com/anteloper-c/triton-anchor/pull/1) 只向 `setup.py` 增加未登记的 `packaging==24.1`，其 [admission 运行](https://github.com/anteloper-c/triton-anchor/actions/runs/32723187417) 在两个 target 都记录 1 项 unmapped declaration 并非零阻断，证据上传成功；登记表没有语义新增/更新组件，因此 OSV 明确记录 `not-applicable`，没有伪造覆盖。PR 已关闭且未合并，测试分支已删除。该运行验证了声明差异和 admission 接线；已登记组件变更的 exact OSV 查询仍只有针对性测试证据。

上述首次 audit 只查询到 4 个有精确 PyPI 身份的构建组件。后续实现复用 OSV 官方 custom lockfile 形式，在同一全量查询中同时携带精确 PyPI version 与 Git commit，并将 LLVM 组件身份与仓库 pin 对齐；提交 `1f1e372` 的 [手工 audit](https://github.com/anteloper-c/triton-anchor/actions/runs/32732894684) 已在 Hosted Runner 将精确查询扩大到 8 个组件，把漏洞覆盖缺口从 9 个降至 5 个。

提交 `5e56f65` 的 [手工 audit](https://github.com/anteloper-c/triton-anchor/actions/runs/32738446964) 又从同次构建证据查询 Ubuntu CMake、GCC、Ninja 源包版本和 CPython release commit：源码扫描、Wheel 构建/扫描、OSV 查询和证据上传均成功，共记录 12 个扫描覆盖；该运行同时暴露出 OSV commit 结果的空 `version` 未回填，导致 CPython 两条发现被报告为 inventory mismatch，并确认当前 Wheel 不含 `libzstd.so.1`，不能用 runner 上偶然安装的 `libzstd1` 冒充候选运行时覆盖。后续实现只回填已验证的 CPython 候选版本，并保留 zstd ABI-only 的人工覆盖要求；每个新提交仍须由新的 Hosted Runner audit 验证，不能用本地重放替代远端结论。

提交 `2998f15` 的 [最终手工 audit](https://github.com/anteloper-c/triton-anchor/actions/runs/32741572764) 已验证上述修正：源码扫描、Wheel 构建和同次证据、Wheel 扫描、OSV 以及证据上传均成功，`execution_status=pass` 且执行问题为 0；CMake、GCC、Ninja 和 CPython 都取得精确覆盖。最终 `audit_status=blocked`、`compliance_status=fail`，阻断由 25 项组件事实、27 项许可证审查、1 项 pending policy 和 1 项 zstd ABI-only coverage gap 构成。该结果是远端技术验证和正确阻断，不是合规通过。

提交 `f3474a0` 的 [普通推送运行](https://github.com/anteloper-c/triton-anchor/actions/runs/33368620800) 已通过核心测试、Wheel 候选模拟和源码快照模拟；模拟晋级仍因候选被正确阻断而跳过。随后 [手工 audit](https://github.com/anteloper-c/triton-anchor/actions/runs/33369760635) 完成源码/Wheel 扫描、构建证据、OSV、dependency inventory 对账和证据上传，报告为 `execution_status=pass`、执行问题和未映射发现均为 0，最终 `audit_status=blocked`、`compliance_status=fail`，共保留 60 个真实合规阻断项。由此，audit 消费 dependency inventory 及当时登记事实的接线已经远端验证；阻断仍然不等于 T8.2 完成。`f3474a0` 本身不包含后续 Python 构建环境闭包，不能用这两个运行验证该新增能力。

提交 `ac072fe` 的 [普通推送运行](https://github.com/anteloper-c/triton-anchor/actions/runs/33373236891) 已通过核心测试和源码快照模拟；Wheel job 的源码扫描、真实 Wheel 构建、同次 Python 构建闭包证据、Wheel ScanCode/Syft 和证据上传也成功，但 `Query current vulnerabilities` 在调用 OSV-Scanner 前因构建依赖图对账失败而终止。原因是解析器把 `setuptools` 未启用的 extras 误当成实际依赖，同时登记表缺少当前 `wheel → packaging` 关系。该 run 是失败诊断证据，不是漏洞扫描成功、候选正确阻断或远端端到端通过；对应最小修正仍需新运行验证。

提交 `4a2507f` 的 [修正后普通推送运行](https://github.com/anteloper-c/triton-anchor/actions/runs/33374983083) 已通过 115 项核心测试、源码快照模拟和 Wheel 候选模拟。Wheel job 的真实构建、同次 Python 构建闭包、源码/Wheel 扫描、OSV 查询、候选评估、证据清单和上传均成功；候选步骤还确认 `execution_status=pass`、`evidence_status=complete`、`same-build` 绑定及 Wheel SHA256 一致，并因政策仍为 `pending` 保持 `promotion_status=blocked`。这验证了修正后的技术链和正确阻断，不表示候选合规通过或 T8.2 已完成。

最终文档 HEAD `88fd22c` 的 [普通推送运行](https://github.com/anteloper-c/triton-anchor/actions/runs/33376289755) 再次通过三个必需 job；随后 [手工 audit](https://github.com/anteloper-c/triton-anchor/actions/runs/33377618999) 的真实 Wheel 构建、同次构建闭包、源码/Wheel 扫描、OSV、dependency inventory、证据清单和上传均成功。报告为 `execution_status=pass`、执行问题和未映射发现均为 0，`audit_status=blocked`、`compliance_status=fail`；64 个阻断项由 30 个库存事实、32 个许可证审查、1 个 pending policy 和 1 个 zstd reviewed-coverage 缺口构成，漏洞发现为 0。`build 1.6.0`、`wheel 0.48.0`、`packaging 26.3`、`pyproject-hooks 1.2.0` 和 `setuptools 84.0.0` 均取得精确 OSV `scanned` 覆盖。该结果完成了新构建闭包的手工 audit 技术验证，但仍不是合规通过。

以下情况至少有一项出现时，候选不得晋级：

- 合规策略仍未批准；
- 缺少候选构建清单或产物 SHA256；
- 发现未映射，或分发/嵌入组件的来源、版本、许可证结论未解决；
- SBOM 与 Notice 应覆盖的组件集合不一致；
- 源码归档缺少根 `THIRD_PARTY_NOTICES.md`，或其内容与本次规范 Notice 不一致；
- 存在未批准或不兼容的许可证表达式；
- 存在未处置的 High/Critical 漏洞，或组件没有可核验的漏洞覆盖；
- 组件扫描失败但报告仍试图给出通过结论。

## 原始 T8.2 要求对照

下表只描述当前本地实现，不代表任务已经验收：

| 原始要求 | 当前实现与验证证据 | 尚未关闭 |
|---|---|---|
| 扫描 Python、C++、子模块和构建环境的直接及间接依赖 | Wheel fork 远端运行已递归拉取子模块并运行源码/Wheel ScanCode、Wheel Syft、OSV、同次构建组件、GNU C++ 与 ELF 采集；源码 fork 远端运行已扫描真实 GitHub 快照的文件、Python/CMake/vendored/子模块声明并完成对账；`f3474a0` audit 已远端验证 dependency inventory 对账和 FlagGems 直接依赖登记；`4a2507f` 已验证此前 pypa-build 固定直接根，`a2b0553` 的两次运行已验证 uv 四根、已解析间接依赖及 absent 替代前端分类 | GitHub 自动归档不递归包含 FlagGems 子模块源码，当前声明扫描也尚未自动递归解析其动态 `setup.py`；FlagGems 没有 lock，间接版本及部分组件事实仍未解析 |
| 为发布产物生成 CycloneDX 或 SPDX SBOM | 远端 Wheel 已按文件名、版本、平台和 SHA256 生成独立 CycloneDX 1.7；源码核心已为真实 GitHub ZIP/TAR.GZ 建立一份以规范树 digest 为根、同时记录两种 representation 的独立 SBOM link；手工 audit `33465696798` 已为实际构建 Wheel 生成产品 SBOM 和关联文件并随证据上传，证据包已独立核实 | 当前产品 SBOM 的库存状态仍因真实未解决组件事实为 incomplete；正式 tag 候选和实际 release 入口尚未形成 |
| CI 定期漏洞扫描并跟踪依赖更新 | 手工 `audit` 已在 Hosted Runner 真实构建/扫描 Wheel并正确阻断；每周定时定义已接入；精确 PyPI version、Git commit、Ubuntu 源包版本、CPython release commit 和 dependency inventory 对账均已有远端执行证据；`33465696798` 进一步验证 uv 0.12.8 为 scanned，fully absent 条件组件不再产生假设性覆盖缺口，漏洞发现和覆盖缺口均为 0 | `schedule` 尚未在默认分支周期触发；自动跨期比较、通知和正式保留期尚未落地 |
| 核对指定组件许可证及兼容性 | 已固定官方许可证证据并形成 [许可证核对记录](license_compatibility_review.md)；triton-linalg 另有 tree 对账；手工 audit `33377618999` 中实际出现的 5 个 Python 构建组件已登记版本对应证据；当前本地还登记了 uv 0.12.8 的官方包声明和固定 tag 双许可证文本摘要 | 这些记录仅确认 declared license；uv 及其他组件的 concluded license、组合兼容性和分发义务仍待 leader/许可证审查人批准 |
| 生成并维护 THIRD_PARTY_NOTICES.md | 根 Notice 由唯一登记表确定性生成，并有漂移测试；源码候选还核对归档内实际 Notice 的字节摘要 | 6 个分发/嵌入组件仍缺最终版本、许可文本或归属，当前文件不是 release-ready |
| 新增依赖许可证和高风险漏洞准入 | 个人 fork PR 已真实比较 base/merge-result 声明、对两个 target 调用 `admission` 并因未登记依赖正确阻断；登记表中新增/更新精确组件的 OSV 查询已有针对性测试 | 本次远端样例没有语义修改登记表，因此该 OSV 分支尚未远端执行；首次引入 T8.2 且 base 无登记表时只能记录不可适用；子模块内部及其他间接依赖仍需可信 scanner delta 输入 |
| 每个候选关联对应版本 SBOM | Wheel、构建证据、报告与一对一 SBOM link 使用同一 SHA256；源码 ZIP/TAR.GZ 的外层 SHA 分别记录并共同关联一份对应 repository/ref/commit/tree digest 的源码 SBOM | 实际正式候选生产入口及晋级调用点尚未形成；接口/升级文档是 release collateral，不单独生成 SBOM |
| 覆盖直接、可识别间接和主要构建组件 | 产品依赖图与 CycloneDX formulation 分离；Wheel 同次构建证据记录 Python 构建根及已解析间接依赖、CMake、Ninja、LLVM、pybind11、vendored 组件及 ELF 运行库，并把构建依赖边写入 formulation；该路径已由 `4a2507f` 远端执行；源码快照从声明和实际路径激活 distributed/build-only/runtime-optional 组件 | 仍需依据真实结果补齐 unresolved 组件身份和许可证结论 |
| 严重漏洞有处置或批准记录 | 修复/隔离/升级要求处置证据；风险接受有范围、审批人和有效期校验；此前 `setuptools 68.1.2` 样本中的两个 High 漏洞已证明会阻断；`5e56f65` 的原始 OSV 证据发现 CPython 3.12.14 的一项 medium 和一项 low，修正后都能绑定到候选版本；当前本地实现不再为 fully absent 的 zstd 产生假设性覆盖缺口 | 当前无已批准风险接受；新规则尚未远端验证；实际出现但缺少精确扫描或 reviewed coverage 的组件仍会阻断 |
| 所有分发第三方进入归属声明 | 候选必需集合与规范 Notice 做独立覆盖对账；源码归档缺失或漂移的随包 Notice 会阻断 | 当前 Notice 的未解决项仍会阻断 |
| 不兼容许可证或未处置严重漏洞阻断晋级 | 远端模拟在扫描与证据完整时仍保留 `candidate` 非零和 `promotion_status=blocked`，且没有发布动作 | 还未挂到实际正式 promotion 步骤，不能宣称已形成发布闭环 |

## CI 接入边界

核心逻辑、组件登记表、策略和规范 Notice 应在 `main` 维护唯一副本，其他流程只调用同一 CLI，不在 `scripts/ci` 或 `scripts/local_ci` 复制规则。当前 `CI_dev` 的正常 PR 重型构建主要由 `scripts/local_ci` 完成，它生成并安装一个测试 Wheel；`scripts/ci` 的完整 Delivery 构建主要是手工辅助链。两者都可以提供技术评估样本，但活跃程度或 Wheel 位于 `dist/` 都不能自动把它变成正式候选。

T8.2 不等待 T3.8/T4.1 形成新的 tag/release 流程，而是直接复用现有 CI 和本地构建能力取得确切 Wheel、生成同次证据并验证门禁；正式源码快照仍应是同一 tag 自动生成的 ZIP/TAR.GZ。当前没有实际正式候选入口，因此不把 Local CI Wheel、手工 Delivery Wheel 或分支 commit 归档冒充正式候选。`anteloper-c/triton-anchor:t82-dependency-compliance` 继续作为实验目标仓库：一个 job 自动构建真实 Wheel并按事件运行 `audit` 或模拟 `candidate` 阻断，另一个 job 下载当前 commit 的真实 GitHub 双格式归档并调用 `artifact-evaluation`。这些路径都不创建 tag、GitHub Release 或包仓库发布。

若以后形成实际发布流程，它必须在生成正式 Wheel 后传入同次构建 Wheel、`same-build` 证据和全部扫描报告；还要对已冻结 tag 下载实际 ZIP/TAR.GZ，解析 tag→commit并建立 `verified-tag-commit`。GitHub 自动源码归档只能在 tag 已存在后验证，因此 T8.2 阻断点应是 Release 发布或附件晋级，不是 tag 创建；失败时不得移动或删除该 tag。随后逐产物调用同一 `candidate` 并依据退出码阻断 release 晋级；届时不需要改写合规核心。

### 当前 `dependency-compliance.yml` 的执行流

开发阶段向 `t82-dependency-compliance` 推送合规文件或会改变 Wheel 的源码/构建文件会触发该 workflow；手工触发时可用 `run_audit=true` 复用同一 Wheel 构建/扫描链运行全量 `audit`，否则保持现有 Wheel 候选和 commit 源码快照模拟。手工 audit 已完成远端验证。每周一 03:17 UTC 的 `schedule` 只从仓库默认分支运行，因此当前分支尚不能证明周期执行。面向 `main`、`CI_dev` 或个人实验基线的依赖相关 PR 会运行独立 `dependency-admission`；普通 push 不借此获得候选身份。相同 ref 上的新运行会取消旧运行。fork 分支名和模拟 job 的仓库判断只用于当前实验，不能把个人开发入口固化到长期流程中。

`audit` 的退出码不会被工作流改写。提交 `88fd22c` 的手工运行 [33377618999](https://github.com/anteloper-c/triton-anchor/actions/runs/33377618999) 中，源码扫描、Wheel 同次构建/扫描、Python 构建闭包、精确 OSV 查询、dependency inventory 对账和证据上传均成功；报告为 `execution_status=pass`、执行问题和未映射发现均为 0，`audit` 因 `pending` 政策、未解决组件/许可证事实和 1 个 zstd 覆盖缺口返回非零。该结果验证的是扫描链、`compliance-audit.json`、依赖库存、阻断原因和证据上传，而不是绿色 CI；正确阻断也不代表 T8.2 已完成。

PR job 使用 base commit 与 GitHub merge-result 的只读 Git 归档生成声明差异，并比较两端组件登记表。声明扫描、OSV 归一化、`admission` 逻辑、policy 和风险接受都取自 base；merge-result 只作为待审源码与 proposed registry 输入，不能通过同时修改自身门禁来放行。声明 delta 非空或登记表文件变化时才安装 base 固定的 OSV-Scanner；runner 只对登记表中语义新增/更新、属于当前 target 且身份精确的组件查询 PyPI version 或 Git commit，然后分别对 `core-wheel`、`source-snapshot` 调用 `admission`。不属于某 target 的变更明确记录 `scanner_execution.status=not-applicable`，不伪造覆盖。若 base 还没有组件登记表和合规核心（首次引入 T8.2），job 只在 step summary 标记 baseline registry unavailable 并跳过准入，不能把它算作通过；个人实验分支可作为已有登记表的验证基线。当前 base policy 为 `pending`，真正进入 admission 的 PR 预期非零阻断并上传报告。该接线覆盖可静态识别的 Python、pyproject、setup、CMake、vendored、`.gitmodules` 声明和登记表变化，不宣称已覆盖子模块内部或 scanner 才能识别的全部间接变化。

```text
push / pull_request / workflow_dispatch / schedule
                         ↓
core-test：checkout（不拉子模块）→ compileall → 全部 compliance 单元测试
       ┌─────────────────┼──────────────────────┐
       ▼                 ▼                      ▼
依赖相关 pull_request   schedule / 手工 audit；  仅 anteloper 实验分支普通触发
base/merge 声明与       普通 Wheel candidate     ├─ Wheel candidate 模拟
registry delta          模拟仍限实验分支          └─ commit 源码快照评估
       ↓                 ↓                      ↓
变更组件 exact OSV      Wheel 同次构建/扫描       双归档/Git tree/扫描
       ↓                 ↓                      ↓
两 target admission     audit 或 candidate       artifact-evaluation
       └────────────上传各自证据──────────────────┘
                              ↓
              candidate 只记录门禁结果，不执行发布动作
```

`core-test` 用小型夹具验证 Wheel、源码双归档、target 隔离、组件对账、OSV exact-commit/变更组件输入、SBOM/Notice 和 CLI 门禁；它本身不扫描真实项目。`dependency-admission` 只在 PR 上比较受信 base 与 merge-result，并按需要安装 OSV；`candidate-simulation` 这个历史 job id 复用同一 Wheel 真实数据流：定时或手工审计时调用 `audit`，并用同一组产物和证据调用 `artifact-evaluation` 生成 Wheel 产品 SBOM；个人实验分支的普通触发才调用模拟 `candidate`，其输出本身已经包含产品 SBOM。两种模式都把 SBOM、关联文件、报告和原始输入放在同一证据目录并整体上传，也都使用与 `triton/cmake/llvm-hash.txt` 匹配的公开 LLVM 归档。`source-snapshot-simulation` 不构建 Wheel或 LLVM：它下载当前 `GITHUB_SHA` 的 GitHub API zipball/tarball，与 checkout commit 树对照，只解包一份等价表示后运行独立的 ScanCode、Syft、dependency inventory 和 OSV。固定扫描器、任一证据或对账失败都会使相应执行失败，不能用另一产物报告或空报告替代。源码、既有手工 audit、声明差异/admission 和非 audit 回归都已在 anteloper fork 运行；它们仍是 commit 或 fork 验证，不是 RACE tag/release 验收。Ubuntu 源包、CPython release commit、dependency inventory 和此前 Python 构建环境闭包均已由 `88fd22c` 的 Hosted Runner audit 验证；新增 uv 与 audit 产品 SBOM 路径已分别由普通运行 `33464818866`、手工 audit `33465696798` 和最终 HEAD 运行 `33468894837` 验证。

提交 `bc67046` 进一步为 Wheel ELF 已实际识别出的 glibc 运行库查询同次 Hosted Runner 的 `libc6` 二进制包、Ubuntu 源包版本和 OSV 身份。普通运行 `33617609706` 和手工 audit `33619224129` 已验证 Ubuntu 24.04 的 `libc6:amd64` 来自 `glibc` 源包，版本 `2.39-0ubuntu8.8`，OSV 覆盖为 `scanned`；完整 audit 中 glibc 的 inventory 缺口因此只剩“approved concluded license”，仍须人工批准。该次 OSV 原始证据还观察到 CPython 3.12.14 的中等和未定严重度记录；它们没有触发当前高风险漏洞门禁，但仍是后续审计需要跟踪的事实。`gcc-runtime` 同时覆盖 `libgcc_s.so.1` 与 `libstdc++.so.6`，不能选其中一个二进制包冒充完整来源；可选 Python 运行依赖和 test-only 依赖也只能由以后明确选定的支持环境提供版本，不能登记任意最新版本。

模拟 job 从自己的构建步骤取得唯一 `dist/triton_anchor-*.whl`，而不是假定一个固定文件名。工作流固定安装 `uv 0.12.8`，通过 `uv pip --system` 安装 PEP 518 构建依赖，再执行 `uv build --wheel --no-build-isolation`；这与 README、`docs/build.md` 和 CI_dev 的实际前端构建一致。构建后立即调用 `build_evidence.py`，所以 `same-build` 声明与当前 Wheel SHA256 对应，实际 package tool 和 ELF 条件依赖也在同一时点分类。工作流在构建前以 uv、pybind11、setuptools、wheel 的实际安装版本生成 pip installation report；核心校验报告解释器、所选前端对应的直接根和本机已安装版本，只把报告闭包纳入构建证据、SBOM formulation 与 OSV 输入，不读取 runner 的其他 Python 包。登记表同时保留 pypa-build 作为其他调用者可选择的条件前端；运行 `33464818866` 和手工 audit `33465696798` 均验证 uv 0.12.8 为 present、pypa-build/pyproject-hooks 为 absent、packaging 为 present，uv 的 OSV 覆盖为 scanned。Ubuntu Hosted Runner 对 GCC、CMake、Ninja 和 zlib 构建/运行输入记录 `dpkg-query` 返回的源包名/源包版本，并以 `Ubuntu:<release>` 生态查询 OSV；zlib 使用提供 `libz.so.1` 的 `zlib1g`。若当前 Wheel `DT_NEEDED` 实际出现 `libzstd.so.1`，同样以 `libzstd1` 查询；组件 `presence=absent` 时跳过该包查询。CPython 记录解释器精确版本，并把对应官方 release tag 解析为 commit 后做 Git 查询。CPython tag 映射只用于漏洞查询身份，不证明 `setup-python` 二进制的构建 provenance。工具显示版本、二进制包版本和 OSV 查询身份分别保留，不能用任意相同名称代替实际构建包。`present` 才激活组件；同次构建证据明确 `absent` 的用途不进入本次候选范围，完整 audit 也排除全部受审用途均已如此分类且没有 present 观察的组件。没有明确分类、只排除部分用途或实际 present 的组件仍按原规则阻断。核心仍保留任意路径接口；本地、服务器或未来 release job 可以使用同一命令，把 `--wheel` 换成该执行环境实际可见的路径：

```text
python -m scripts.compliance.cli candidate \
  --wheel <exact-wheel-path> \
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

正式 tag 源码候选使用同一个入口，但输入和绑定规则不同：

```text
python -m scripts.compliance.cli candidate \
  --source-zip <tag-source.zip> \
  --source-tar <tag-source.tar.gz> \
  --source-repository https://github.com/RACE-org/triton-anchor \
  --source-reference-kind tag \
  --source-reference <tag> \
  --source-commit <resolved-commit> \
  --source-version <release-version> \
  --source-repository-root <trusted-checkout> \
  --registry compliance/component-registry.json \
  --policy compliance/license-policy.json \
  --risk-acceptances compliance/risk-acceptances.json \
  --scancode-source <source-scancode.json> \
  --syft <source-syft.cdx.json> \
  --osv <source-osv-results.json> \
  --dependency-inventory <source-dependency-inventory.json> \
  --notices THIRD_PARTY_NOTICES.md \
  --target source-snapshot \
  --output-dir <source-output>
```

当前 CLI 只记录调用者传入的 repository 和 `source-version`；tag 到 release 版本的映射、Wheel/源码/文档的统一版本来源及 repository 规范表示，必须由实际具有候选指定权的受信调用流程校验后再调用，不能仅凭上述命令的退出码宣称版本发布闭环。

Wheel fork `candidate` 模拟和手工候选调用都不构成正式发布批准。当前策略仍为 `pending`，所以 Wheel 模拟预期 `promotion_status=blocked`；workflow 可以因“正确观察到阻断”显示成功，但结果文件必须保留 blocked。源码 commit job只运行 `artifact-evaluation`，必须保持 `promotion_status=not-applicable`、`formal_tag_binding=false`；两条实验路径都不执行发布动作。

该 fork 模拟从同一开发分支读取扫描对象和合规代码，因此只能验证数据流，不能验证候选代码与受信门禁代码的隔离；正式接入时该隔离由 T8.3 的 workflow 权限边界负责。

原先放在普通 `ci.yml` 中的 `compliance-core` 已移到 `dependency-compliance.yml`，只在合规核心、策略、Notice 或对应测试变化时运行。它验证 Wheel-SBOM 一一关联、许可证和严重漏洞阻断、风险接受、Notice 对账及 CLI 退出码等 T8.2 判断逻辑。单元测试不是原任务单列的交付物或新任务节点，也不代替真实扫描；它是防止门禁代码修改后错误放行的最小实现保障。

四个入口共用同一套代码和策略，不再维护第二套“简化门禁”：

| 入口 | 用途 | 成功条件 |
|---|---|---|
| `admission` | PR 中新增或政策相关的依赖变更 | 声明差异已映射到登记表，版本/来源/许可证可审查，且有版本对应的漏洞覆盖和无未处置高风险漏洞 |
| `audit` | 定时或手工全量依赖审计 | 扫描执行完整，本次实际出现或没有被完整 absent 证据排除的登记组件身份与许可证已解决，逐组件漏洞覆盖完整；已批准风险接受按 release 版本生效 |
| `artifact-evaluation` | 对任意具体 Wheel 或源码快照做来源渠道无关的技术评估 | 执行成功、证据完整且合规通过时返回 0，但 `promotion_status` 始终为 `not-applicable` |
| `candidate` | 由实际具有产物晋级语义的受信调用流程对正式候选做发布前门禁 | 除全部技术/合规检查外，Wheel 必须是 `same-build` 绑定；源码快照必须是 `verified-tag-commit`，进程才返回 0 |

当前阶段建立了唯一多产物核心。Wheel、源码快照、手工 `audit`、PR `admission` 和非 audit 回归都已有 Hosted Runner 证据；PyPI version、Git commit、Ubuntu 源包和 CPython release commit 的全量查询都已真实执行。提交 `a2b0553` 的普通运行和手工 audit 已验证 uv Wheel、同次构建闭包、产品 SBOM 上传、uv OSV 以及 fully absent 条件组件范围；运行 `33468894837` 又在文档提交 `488b9da` 上重复通过三项必需验证并上传两份证据包。手工 audit 的 57 个阻断由 27 个库存事实、29 个许可证审查和 1 个未批准政策组成，没有漏洞或漏洞覆盖阻断。uv 0.12.8 的官方双许可证声明和固定 tag 文本摘要已登记，但许可证兼容性结论仍为空。这仍不能宣称 T8.2 完成：默认分支周期审计、自动跨期跟踪、子模块内部和间接依赖 delta、许可证政策及组件结论尚未关闭，审计、准入和模拟输出也不是正式 release asset。T8.2 后续继续基于现有 CI 独立推进；若以后出现实际晋级步骤，再同时接入正式 Wheel 和 tag 源码快照的 blocking `candidate`。自动化必须消费同一登记表、策略和核心，不能复制第二份规则。

## 重构不变量与恢复步骤

无论未来把调用入口接到 Local CI、GitHub Actions 还是独立 release 服务，重构时必须保持以下不变量：

1. 取得、构建或下载产物的代码在核心之外；核心只接收明确的 Wheel 路径，或一对明确的 GitHub ZIP/TAR.GZ 路径及源码引用信息。
2. 每份 Wheel 以实际 SHA256 和经校验元数据标识；源码快照以 repository、tag/commit、归档文件与 gitlink 的规范树 digest 标识，两份外层 SHA作为 representation 证据；绝对路径不进入稳定输出。
3. 同一组件模型派生 SBOM、Notice 和门禁范围，不维护三份组件名单。
4. 扫描器提供发现和证据，不能自行批准许可证、风险接受或候选身份。
5. 缺报告、扫描失败、未映射发现和身份冲突必须显式失败，不能用空报告代替。
6. `execution`、`evidence`、`compliance`、`sbom_inventory` 和 `promotion` 状态保持独立，避免一个状态掩盖另一个问题。
7. `artifact-evaluation` 与 `candidate` 共用技术逻辑；只有后者增加正式候选绑定和非零阻断语义：Wheel 为 `same-build`，源码为 `verified-tag-commit`。
8. 不加载或 import 候选 Wheel/源码归档中的代码；原生依赖通过文件、ELF、声明和构建证据读取。
9. 每个逻辑候选产物独立生成 SBOM 和关联文件；源码 ZIP/TAR.GZ 只有在规范树相同后才视为同一逻辑产物，不能复用另一架构、ABI、插件或不同源码树的结果。
10. 自动化接入只能调用受信版本的核心和策略，候选代码不能修改自身门禁后放行。

## 尚需上报的决策

以下事项不应由实现代码猜测；实验可以先给出数据，但在正式启用前需要 leader 或相应责任人确认：

| 决策 | 最迟确认点 |
|---|---|
| 哪个实际 CI/release job 有权把 Wheel 和 tag 源码快照指定为正式候选，以及哪个晋级步骤受 T8.2 阻断 | 接入正式 `candidate` 前 |
| 首批正式 Wheel 包含哪些平台/Python ABI/插件；Wheel 与 tag 源码快照的唯一 release 版本来源、tag 到版本的映射和 repository 规范表示；以及必须随 release 交付哪些接口/升级文档 | 第一次正式候选评估前 |
| 许可证 expression 的 allow/deny/review 结论及 Notice 分发义务 | 将 policy 从 `pending` 改为批准前 |
| High/Critical 风险接受的批准人、适用范围和记录存放方式 | 第一次需要例外放行前 |
| 候选 Wheel、源码 ZIP/TAR.GZ、SBOM、文档和原始扫描证据的保存位置及保留期 | 正式 release 流程落地前，可在自动扫描开发期间确认 |
