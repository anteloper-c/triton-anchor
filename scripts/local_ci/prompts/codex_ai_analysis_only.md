本次确定性 Local CI 退出码为 ${LOCAL_CI_STATUS}，因此进入只分析模式。
请检查代码差异、${LOCAL_CI_LOG_PATH} 和只读产物目录 ${ARTIFACT_DIR}。
不要创建或修改文件，不要运行构建或测试；只允许运行读取代码、差异、日志和产物清单所需的命令。
test_execution.status 必须为 not_run，generated_test_files 和 commands 必须为空数组。
在 test_execution.summary 中用中文说明未执行测试的原因。
