本次确定性 Local CI 已成功完成。
你可以在这个一次性 checkout 中创建或修改测试文件与临时诊断代码。
请根据代码差异自主生成有针对性的测试，并运行必要的定向测试、构建、lint 或诊断命令。
本次差异要求生成定向测试：${TEST_GENERATION_EXPECTED}；true 表示包含可测试代码改动，false 表示仅包含文档类改动。
可测试代码改动应生成 ${MIN_GENERATED_TEST_CASES} 至 ${MAX_GENERATED_TEST_CASES} 个定向测试用例。
最多创建或修改 ${MAX_GENERATED_TEST_FILES} 个测试文件，最多执行 ${MAX_TEST_COMMANDS} 条测试、构建或 lint 命令。
单条命令预计不超过 ${RECOMMENDED_COMMAND_TIMEOUT_SECONDS} 秒，测试命令累计预计不超过 ${TEST_BUDGET_SECONDS} 秒。
Codex 总时限为 ${CODEX_TIMEOUT_SECONDS} 秒，至少预留 ${REPORT_RESERVE_SECONDS} 秒分析结果并生成最终报告。
通过的用例不要重复运行；失败用例最多额外复跑一次，以区分稳定失败和不稳定失败。
不要运行整个仓库的全量测试或完整重编译，不要安装或升级依赖。
不要修复生产实现代码；文件改动只能用于测试或临时诊断。
文档类改动可以不生成测试，但必须在 test_execution.summary 中用中文说明原因。
无法生成或运行有效测试时，test_execution.status 必须使用 insufficient_evidence，不能虚报为 passed。
把所有生成的测试路径写入 test_execution.generated_test_files。
把每条测试、构建或 lint 命令及其退出码、耗时、状态和中文证据写入 test_execution.commands。
