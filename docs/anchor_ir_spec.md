# AnchorIR 规范

本文档规定 AnchorIR 的双轨基础白名单、Forbidden 集合、两阶段验证规则以及方言准入与扩展机制。

## 1. AnchorIR 是什么

AnchorIR 是 triton-anchor 中 IR 生产路径与 Backend 之间的统一交付契约，定义了 Linalg Track 和 TritonGPU Track 两种合法输出形态，并规定每个 Track 的基础方言集合、Forbidden 集合和验证边界。

AnchorIR 两端的职责明确如下：

- **生产者**：triton-anchor 中的 Adapter 或公共转换路径，负责把优化后的 TTIR 转换为符合所选 Track 的 AnchorIR；
- **消费者**：Backend （后端），负责接收通过验证的 AnchorIR，并将验证后的 IR lowering 到目标相关 IR、指令或可执行产物。

> Adapter：是将上游优化后的 TTIR 按特定指针分析和转换策略转换为 AnchorIR 的生产组件。

| Track           | 主要方言                                              | 典型使用场景                      |     
| --------------- | ------------------------------------------------- | --------------------------- |
| Linalg Track    | `linalg`、`tensor`、`memref`、`arith`、`math`、`scf` 等 | AME Matrix、Tensor Processor |     
| TritonGPU Track | `triton_gpu`、带 encoding 的 `tt` Op、GPU 相关方言        | SIMT/GPGPU                  |     

Track 与目标硬件的计算范式解耦。Backend 通过 `HWCapability` 显式选择 Track。典型情况下，AME 或专用张量处理器使用 Linalg Track，GPGPU 使用 TritonGPU Track，但这不是强制绑定。

### 1.1 设计动机

双轨契约解决以下问题：

1. **降低前后端耦合**：在 IR 生产路径与 Backend 之间建立明确、稳定的交付边界。Adapter 的指针分析和转换策略可以独立演进，Backend 只需面向所选 Track 的公开 AnchorIR 契约实现后续 lowering；    
2. **统一同一 Track 的交付约束**：同一 Track 的不同 Adapter 或公共转换路径可以产生结构不同的 IR，但必须遵循一致的方言边界、公开语义和验证规则，并保持预期的程序语义；    
3. **避免单一 IR**：Linalg Track 保留结构化张量、缓冲区和控制流抽象，适合矩阵扩展和专用张量处理器；TritonGPU Track 保留 Encoding、布局、warp 和线程映射信息，适合需要显式 GPU 执行语义的 Backend。双轨设计避免非 GPU 后端被迫理解 GPU 专用抽象，也避免 GPU lowering 过早丢失必要的布局信息；    
4. **维护交付边界完整性**：通过基础白名单、Forbidden 集合和 Unknown 方言检查，阻止临时 lowering 方言、前端命名空间及错误 Track 表示泄漏到 Backend；
5. **允许受控扩展**：允许 Backend 在受控边界内声明扩展方言，而不扩大 Adapter 的公共基础输出契约。

## 2. Linalg Track 基础白名单

当前 `LINALG_TRACK_ALLOWED` 共 **15** 个方言：

| 方言 | 含义 | 典型 Op 示例 |
|---|---|---|
| `linalg` | 结构化线性代数和通用张量计算，是本 Track 的核心计算表示 | `linalg.generic`、`linalg.matmul`、`linalg.yield` |
| `linalg_ext` | `triton-linalg` 提供的扩展结构化计算，包括 gather、scatter、原子操作、scan 等 | `linalg_ext.scatter`、`linalg_ext.gather`、`linalg_ext.atomic_rmw`、`linalg_ext.scan` |
| `tensor` | 值语义张量的创建、切片、维度查询和元素访问 | `tensor.empty`、`tensor.extract`、`tensor.extract_slice` |
| `memref` | 带布局和地址空间的缓冲区、载入/存储及视图操作 | `memref.alloc`、`memref.load`、`memref.store` |
| `arith` | 标量、向量或张量上的基础算术、比较、选择与常量 | `arith.constant`、`arith.addf`、`arith.cmpi`、`arith.select` |
| `math` | 标准数学函数 | `math.exp`、`math.sin`、`math.sqrt` |
| `math_ext` | `triton-linalg` 的补充数学运算 | `math_ext.mulhiui` |
| `scf` | 结构化控制流 | `scf.for`、`scf.if`、`scf.while` |
| `func` | 函数定义、调用与返回 | `func.func`、`func.call`、`func.return` |
| `cf` | 基于基本块的非结构化控制流 | `cf.br`、`cf.cond_br`、`cf.switch` |
| `affine` | 仿射循环、条件与索引计算 | `affine.for`、`affine.if`、`affine.apply` |
| `aux` | `triton-linalg` 的辅助资源、视图、调试和优化屏障操作 | `aux.view`、`aux.print`、`aux.optimization_barrier` |
| `index` | `index` 类型上的平台无关整数运算 | `index.constant`、`index.add`、`index.cmp` |
| `bufferization` | Tensor 与 MemRef 之间的缓冲化边界和显式分配 | `bufferization.alloc_tensor`、`bufferization.to_buffer`、`bufferization.to_tensor` |
| `vector` | 显式 SIMD/vector 计算、搬运与 contraction | `vector.transfer_read`、`vector.transfer_write`、`vector.contract` |

典型 Op 仅用于说明方言职责，不构成 Op 级白名单。当前 Validator 按被扫描到的 Op 方言命名空间判断，不会因为某个 Op 未出现在上表而拒绝它。 

## 3. TritonGPU Track 基础白名单

当前 `TRITON_GPU_TRACK_ALLOWED` 共 **8** 个方言：

| 方言           | 含义                                                     | 典型 Op 示例                                             |
| ------------ | ------------------------------------------------------ | ---------------------------------------------------- |
| `triton_gpu` | TritonGPU 核心方言，承载布局、encoding、shared/local memory 和线程映射 | `triton_gpu.convert_layout`、`triton_gpu.local_alloc` |
| `tt`         | 保留 Triton 计算或访存语义并与 TritonGPU encoding 配合的操作           | `tt.load`、`tt.dot`、`tt.store`                        |
| `arith`      | 基础算术、比较、选择与常量                                          | `arith.addf`、`arith.cmpi`                            |
| `math`       | 标准数学函数                                                 | `math.exp`、`math.sqrt`                               |
| `scf`        | 结构化循环和条件控制流                                            | `scf.for`、`scf.if`                                   |
| `func`       | 函数定义、调用与返回                                             | `func.func`、`func.return`                            |
| `gpu`        | MLIR GPU 方言；可选的 GPU 层次或执行表示                            | `gpu.launch`、`gpu.barrier`                           |
| `nvgpu`      | MLIR NVIDIA GPU 扩展方言；仅在相应 lowering 路径中使用               | `nvgpu.mma.sync`                                     |

`gpu` 和 `nvgpu` 位于基础白名单中，表示 AnchorIR Validator 允许被扫描到的相应 Op；这不表示每个 TritonGPU Track Backend 都支持该方言中的全部 Op，也不表示生产者必须生成它们。

## 4. Forbidden 与 Unknown 方言

方言检查采用闭集白名单：一个被扫描到的 Op 只有位于当前阶段的允许集合中才合法。显式 Forbidden 集合用于标记绝不能穿越相应 Track 边界的命名空间，并提供更明确的错误信息；未显式列入 Forbidden 但不在允许集合中的方言属于 Unknown，同样必须失败。

Forbidden 集合按 Track 定义，同一方言在一个 Track 中被禁止，不代表它在另一个 Track 中也被禁止；Forbidden 的判定优先级高于允许集合。

### 4.1 Linalg Track Forbidden

`LINALG_TRACK_FORBIDDEN` 共 7 个命名空间：

| 方言 | 禁止原因 |
|---|---|
| `tt` | Triton 方言必须在进入 Linalg Track 前完全 lowered；残留表示 Adapter 转换不完整 |
| `triton` | `tt` 的别名，按相同规则禁止 |
| `tts` | `triton-shared` 的过渡方言，不是稳定的 Backend 交付契约 |
| `tptr` | Triton pointer 过渡方言，指针语义必须在 AnchorIR 前完成转换 |
| `smt` | DSL 的 Python/前端命名空间，必须在 pre-hook 前转换为基础白名单方言；`smt` 本身不能穿越边界 |
| `triton_gpu` | 属于另一条 Track；Linalg Track 不得混入带 GPU encoding 的表示 |
| `nvidia_gpu` | 源码显式禁止的 NVIDIA 专用命名空间，不属于 Linalg Track 的公共表示 |

### 4.2 TritonGPU Track Forbidden

`TRITON_GPU_TRACK_FORBIDDEN` 共 3 个命名空间：

| 方言 | 禁止原因 |
|---|---|
| `tts` | Adapter 内部过渡方言，必须在 AnchorIR 边界前消除 |
| `tptr` | 过渡指针方言，必须在 AnchorIR 前转换为 Track 允许的稳定表示 |
| `smt` | DSL Python/前端命名空间，不是可直接交付给 GPU Backend 的 AnchorIR 方言 |

`tt` 在 TritonGPU Track 中是合法基础方言，但在 Linalg Track 中被明确禁止。这是双轨契约的核心差异之一。

### 4.3 Unknown 方言

除显式 Forbidden 方言外，下列情况也必须失败：

- pre-hook 中出现任何不属于当前 Track 基础白名单的方言；
- post-hook 中出现任何既不属于基础白名单、也未被 Backend 声明的扩展方言；
- 出现其他 Track 的方言或 Backend 私有方言，但它们未被显式列入当前 Track 的 Forbidden 集合。

例如，Linalg Track pre-hook 中的 `xsmt.alloc` 属于 Unknown。只有在 pre-hook 通过后由 Backend hook 引入，并且 `xsmt` 被声明为 Backend 扩展方言时，它才可能通过 post-hook。TritonGPU Track 中的 `linalg.generic` 当前会以 Unknown 失败，而不是以显式 Forbidden 失败。

## 5. 两阶段验证规则

本文中的 pre-hook 和 post-hook 均以 Backend hook 为参照；两次验证都发生在生产路径完成 AnchorIR 转换之后。Backend 是 AnchorIR 的消费者。

```text
triton-anchor Adapter / 公共转换路径
    │
    │  产生 AnchorIR
    ▼
pre-hook：仅基础白名单；Forbidden 始终拒绝
    │
    ▼
Backend hook：可引入受控的 Backend 扩展
    │
    ▼
post-hook：基础白名单 ∪ Backend 扩展声明；Forbidden 始终拒绝
    │
    ▼
Backend 专用 lowering
```

两个阶段使用同一套 Op 扫描逻辑，区别仅在允许集合。post-hook 会重新扫描 hook 处理后的整个 IR；当前实现不比较 hook 前后的 IR，也不追踪某个 Op 是由生产路径还是 Backend hook 引入的。

### 5.1 Phase 1：pre-hook

API：`AnchorIRValidator.validate_pre_hook(ir_text)`

时机：AnchorIR 生产完成之后、Backend hook 执行之前。

```text
allowed_pre = TRACK_BASE_ALLOWED
```

pre-hook 对被扫描到的 Op 执行以下检查：

1. 方言是否命中当前 Track 的 Forbidden 集合；
2. 非 Forbidden 方言是否位于当前 Track 的基础白名单。

Backend 扩展声明在此阶段不生效。

### 5.2 Backend hook

Backend hook 是 pre-hook 与 post-hook 之间的受控**消费侧处理阶段**。它接收已经通过 pre-hook 验证的 AnchorIR，并可根据 Backend 的 lowering 需求执行以下操作：

- 添加 Backend 所需的属性；    
- 引入 Backend 私有或硬件相关的扩展 Op；    
- 对基础白名单 Op 进行 Backend 侧规范化或转换，为后续专用 lowering 做准备。    

Backend hook 必须遵守以下约束：

1. 不能用于接收 Adapter 或公共转换路径原始输出中的未标准化方言；
2. 新引入的扩展 Op 必须属于 Backend 显式声明的精确方言命名空间，并按 Track 分别维护扩展集合；      
3. Backend hook 不得引入当前 Track 的 Forbidden 方言，扩展声明也不能覆盖 Forbidden 规则。     

### 5.3 Phase 2：post-hook

API：

```python
AnchorIRValidator.validate_post_hook(
    ir_text,
    ext_allowed=backend_extension_dialects,
)
```

时机：Backend hook 完成之后、Backend 专用 lowering 开始之前。

```text
allowed_post = TRACK_BASE_ALLOWED ∪ BACKEND_EXTENSION_ALLOWED
```

post-hook 对 hook 完成后的整个 IR 执行以下检查：

1. 命中当前 Track Forbidden 集合的方言一律失败；
2. 其他方言必须位于基础白名单或 `ext_allowed` 中；
3. 未声明的 Backend 扩展方言以 Unknown 失败。

## 6. 方言准入与扩展机制

AnchorIR 采用“**基础白名单冻结、Backend 扩展受控**”的治理原则，二者分别治理：

| 扩展类型          | 作用域                        | 生效阶段                 | 变更仓库                             | 治理方式                                                   |
| ------------- | -------------------------- | -------------------- | -------------------------------- | ------------------------------------------------------ |
| Track 基础白名单变更 | 相应 Track 的公共 AnchorIR 输入契约 | pre-hook 和 post-hook | `triton-anchor`，必要时联动 Backend 仓库 | `triton-anchor` 项目级 PR、跨组件评审、append-only、基础白名单变更日志     |
| Backend 扩展声明  | 单个 Backend 的消费侧私有边界        | 仅 post-hook          | Backend 所在仓库，通常是独立仓库             | Backend 仓库 PR、Backend owner 评审、post-hook 与 lowering 测试 |

基础白名单新增方言是公共契约变更；Backend 扩展是单个 Backend 在消费侧的受控例外，不会扩大 triton-anchor 生产路径的公共输出契约。

### 6.1 机制选择

方言准入遵循以下优先级：

1. 优先使用当前 Track 已有基础方言表达语义；
2. 仅服务单个 Backend、且能在 pre-hook 之后引入的方言，使用 Backend 扩展；
3. Adapter 或公共 pass 中的临时、过渡或 Backend 私有方言，必须在 pre-hook 前消除或转换，不能以扩展声明规避；
4. 只有现有基础方言确实无法承载一项稳定、Track 级公共语义时，才可发起基础白名单例外变更。

### 6.2 Track 基础白名单变更

#### 6.2.1 提交 Issue/RFC

在修改基础白名单实现或规范之前，提案人必须先提交独立的设计 Issue/RFC，并由 AnchorIR/Core 维护者确认该需求值得进入方案评审。需求说明至少包括：

- 要解决的实际编译场景、当前失败位置及用户可见影响；
- 精确方言命名空间、上游来源、版本基线和目标 Track；
- 现有基础方言为何不足；
- 为何 Backend 扩展不适用，尤其要说明该需求的 Track 级公共价值，而非单 Backend 的实现便利；
- 计划进入 AnchorIR 的公共语义、代表性 Op，以及按整个命名空间放行的影响；
- 哪条公共生产路径会生成该方言；
- 哪些 Backend 或配置会实际接收新表示，哪些不会接收；
- 对兼容性、工具链版本、缓存、诊断和发布节奏的影响；
- 所需关联 PR、负责人、依赖关系、合入顺序、测试计划及失败时的回退方案。

基础白名单新增方言还必须满足以下条件：

- 方言具有可公开说明且相对稳定的语义，符合目标 Track 的抽象边界；
- 方言不是临时 lowering 表示、单 Backend 私有中间态或当前 Track 的 Forbidden 方言；
- 项目所用工具链能够提供、注册、解析、打印和验证该方言，或已有明确的前置接入计划；
- 存在已批准的公共生产路径及真实消费方案，而不是为未来可能使用而预留字符串；
- 提案对整个方言命名空间的准入风险作出评估。

若这些条件不能同时满足，应维持基础白名单不变，并选择 Backend 扩展、继续 lowering 或向适当上游贡献通用能力。

#### 6.2.2 关联变更与合入顺序

基础白名单 PR 是基础白名单变更的协调入口，必须链接 Issue/RFC 及所有关联变更 PR，并标明负责人、当前状态、阻塞关系和预计合入顺序；关联 PR 也应反向链接基础白名单 PR。

下列关联变更可以由基础白名单 PR 触发，提案人必须逐项判断：

1. **无需额外实现 PR**：只有当方言基础设施、公共生产实现以及必要的消费或隔离能力均已就绪，唯一阻塞是 Validator 尚未放行时，才可以只提交基础白名单 PR。PR 中必须引用已有实现和测试证据。
2. **工具链或方言接入变更**：如果当前工具链基线不包含该方言，应先完成工具链或 vendored 上游升级；如果项目 Context 尚不能注册、加载或链接该方言，应先完成方言接入和构建集成。这些前置变更不得提前改变公共 AnchorIR 输出。若目标上游基线仍不提供所需方言，应重新评估方案。
3. **公共生产路径变更**：如果 Adapter 或公共 pass 尚未生成该方言，应提交独立的生产路径 PR，并与基础白名单 PR 互链。生产路径可以并行开发，但默认启用新输出不得早于基础白名单生效以及必要的消费或隔离条件就绪。
4. **参与 Backend 需要联动**：如果新方言通过能力或版本门控只交付给部分 Backend，只有明确选择接收新表示的 Backend 需要提交消费 PR。其他 Backend 无须实现或审批该方言，但必须继续获得不含该方言的合法 AnchorIR，或者在不满足条件时得到明确诊断。  
5. **CI 能力按风险补充**：如果现有 CI 无法覆盖新增的 C++、TableGen、MLIR、构建或端到端风险，应补充 CI 能力，或提供经 CI 维护者认可的可审计外部验证。长期公共能力原则上应进入可重复运行的 CI。

不参与新方言路径、且由能力或版本门控保持原有合法 AnchorIR 输入的 Backend，不属于该变更的强制联动范围。Backend 的“受影响”应由实际数据流和启用条件确定，不能仅因其声明使用同一 Track 就推定。

#### 6.2.3 基础白名单 PR 流程

基础白名单变更按以下流程执行：

1. **接受变更提案**：提交第 6.2.1 节规定的 Issue/RFC。AnchorIR/Core 维护者确认现有基础方言和 Backend 扩展均不能合理解决该问题，并同意进入基础白名单变更流程。
2. **确定变更范围和关联 PR**：明确目标 Track、精确方言命名空间、公共生产路径、参与 Backend、非参与 Backend 的隔离方式，以及第 6.2.2 节要求的关联 PR 和合入顺序。
3. **完成前置变更**：先完成必要的工具链、方言注册、加载和构建能力，并使参与 Backend 具备消费能力或建立可靠隔离。前置变更不得提前扩大公共 AnchorIR 输出。
4. **提交基础白名单 PR**：PR 应保持最小化，只承担公共契约变化本身，包括：    
	- 更新目标 Track 的精确方言命名空间集合；        
    - 同步基础白名单说明和变更日志；        
    - 提供与本次契约变化直接相关的 Validator 回归测试；        
    - 链接需求说明、关联 PR 和对应验证证据。
5. **提供测试与关联证据**：证据可以来自基础白名单 PR 或其关联 PR，但必须对应明确的 commit，并且可重复或可审计。所有基础白名单 PR 都必须验证两阶段允许集合、Track 隔离、Unknown/Forbidden 规则，并通过项目规定的常规 CI。下列验证按实际变更触发：
	- 涉及方言或构建接入时，验证方言能够在项目实际 Context 中注册、加载、解析和验证；        
    - 涉及公共生产路径时，验证真实转换结果符合目标 Track 并通过 pre-hook；        
    - 涉及 Backend 消费时，验证参与 Backend 的后续 lowering，并验证非参与 Backend 或配置仍被可靠隔离；        
    - 只有计算、访存、布局或控制流语义发生变化时，才要求结果一致性或端到端正确性测试。  
6. **完成审批并按顺序合入**：取得第 6.2.4 节要求的审批后，按照以下顺序合入：    
	1. 工具链和方言接入能力就绪；
	2. 参与 Backend 的消费能力或非参与 Backend 的隔离措施就绪；
	3. 基础白名单 PR 合入；
	4. 合入生产路径变更 PR 或解除相关门控，使 Adapter/pass 开始实际生成该方言。           
7. **记录发布信息**：按第 7 节记录首次包含版本、合入日期、PR/Commit 和变更内容。

#### 6.2.4 审批规则与兼容性

基础白名单变更的审批按责任划定：

| 审批角色                | 是否必需 | 必须确认的内容                                           |
| ------------------- | ---- | ------------------------------------------------- |
| AnchorIR/Core 维护者   | 必需   | 需求是否达到变更门槛、Track 归属、命名空间准确性、公共语义、Forbidden 冲突和兼容性 |
| CI 维护者              | 必需   | 测试计划与合入门禁是否覆盖实际风险，关联证据是否可重复或可审计，以及是否需要补充长期 CI 能力  |
| 相关 Adapter/pass 维护者 | 条件必需 | 仅在公共生产路径发生变化时，确认生成语义、启用条件、回退方式和生产路径验证             |
| 上游依赖/方言接入/构建维护者     | 条件必需 | 仅在版本、注册、链接、C++/MLIR 或构建发生变化时，确认工具链和构建兼容性          |

只有明确选择接收新表示的 Backend 才需要在自身仓库完成相应消费 PR，并按该 Backend 的所有权规则审批；其 PR 状态和测试结果作为基础白名单变更的关联证据。未选择参与且被可靠隔离的 Backend 无须实现新方言，也无须为基础白名单 PR 背书。

在当前兼容契约内，基础白名单变更流程只允许**追加**方言，并且追加本身仍需满足本节的流程。删除或重命名已有方言、将 Allowed 改为 Forbidden、或者收紧已有方言的公开语义，都会使以前合法的 IR 失效，这类变更必须作为 AnchorIR 破坏性版本变更单独治理，提供迁移方案、弃用期、跨组件验证和明确的版本边界。

### 6.3 Backend 扩展方言

Backend 扩展用于单个 Backend 在消费侧引入硬件相关、实验性或私有方言，是基础白名单之外的常规扩展机制。它不改变 Adapter 或公共转换路径的合法输出，也不进入第 7 节的基础白名单变更日志。

#### 6.3.1 扩展边界

Backend 扩展必须满足：

- 扩展方言仅在 pre-hook 通过后由 Backend hook 引入或使用；
- 以精确命名空间声明，不允许通配符、前缀模式或正则表达式批量放行；
- 声明按 Track 管理，只加入 post-hook 的允许集合；
- 扩展声明不能覆盖当前 Track 的 Forbidden 集合；
- Adapter 和公共 pass 不得在 pre-hook 前输出 Backend 私有方言；
- 一个 Backend 支持多个 Track 时，必须分别声明、分别验证。

#### 6.3.2 Backend 扩展 PR

Backend 扩展 PR 提交到相应 Backend 仓库。PR 或关联 Issue 至少说明：

- Backend、目标 Track 和精确方言命名空间；
- 方言由哪个 hook 或私有 stage 引入，由哪个 lowering 阶段消费；
- 方言注册、加载和工具链版本要求；
- 不支持该扩展的硬件、配置或 Track 如何隔离并给出诊断；
- 对缓存、持久化 IR、插件接口和 Backend 版本兼容性的影响。

实现必须保证 pre-hook 仍只接受 Track 基础白名单，post-hook 对 hook 完成后的整个 IR 重新验证，并且只有已声明的精确命名空间获得准入。

#### 6.3.3 测试、审批与发布

Backend 扩展 PR 的测试应覆盖以下方面：

- **验证边界**：证明扩展只在声明后的 post-hook 生效，不能进入 pre-hook，也不能覆盖 Forbidden；
- **方言可用性**：证明 Backend 实际 Context 能注册、解析和验证私有方言；
- **生成与消费闭环**：证明 hook 或私有 stage 能产生预期 IR，后续 lowering 能完整消费；
- **配置隔离与诊断**：证明不支持的 Track、硬件或工具链版本不会静默接受扩展；
- **端到端与缓存**：在扩展影响编译产物或缓存键时，验证相应端到端编译和缓存失效行为。

此类 PR 至少由对应 Backend 维护者按其仓库规则批准，确认声明范围、生成路径、消费路径、失败诊断和兼容性。

Backend 应在自身文档或变更日志中记录扩展命名空间、适用 Track、首次支持版本和必要的硬件或工具链条件。扩展方言的删除、重命名和语义变更遵循该 Backend 的兼容性政策。

## 7. 基础白名单变更日志

基础白名单变更 PR 合入后，在表格顶部新增一行。同一 release 包含多项变更时，每项保留独立记录；PR 合入后、正式发布前，首次包含该变更的项目版本记为 `Unreleased`，发布时替换为实际项目 release；合入日期采用 `YYYY-MM-DD`；PR 使用可点击链接。

| 首次包含该变更的项目版本                                                          | 合入日期       | Track     | PR/Commit                                                                                              | 变更内容                                                                                                                                                    |
| --------------------------------------------------------------------- | ---------- | --------- | ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------------------------- |
| [`v0.1`](https://github.com/RACE-org/triton-anchor/releases/tag/v0.1) | 2026-05-08 | Linalg    | [`eb9bd80`](https://github.com/RACE-org/triton-anchor/commit/eb9bd80bdd0bd1a4a492576ba9aafcf1f2cf09d2) | 建立包含 15 个方言的初始基础白名单：`linalg`、`linalg_ext`、`tensor`、`memref`、`arith`、`math`、`math_ext`、`scf`、`func`、`cf`、`affine`、`aux`、`index`、`bufferization`、`vector` |
| [`v0.1`](https://github.com/RACE-org/triton-anchor/releases/tag/v0.1) | 2026-05-08 | TritonGPU | [`eb9bd80`](https://github.com/RACE-org/triton-anchor/commit/eb9bd80bdd0bd1a4a492576ba9aafcf1f2cf09d2) | 建立包含 8 个方言的初始基础白名单：`triton_gpu`、`tt`、`arith`、`math`、`scf`、`func`、`gpu`、`nvgpu`                                                                          |


