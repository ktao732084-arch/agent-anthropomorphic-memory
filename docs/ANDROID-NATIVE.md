# 安卓原生运行

本项目的核心记忆流程已移植到 [大肥鱼 Android 0.4.0](https://github.com/ktao732084-arch/dafeiyu-android/tree/v0.4.0)。原生代码编译进APK，手机直接执行，不要求用户运行本仓库的Python服务、连接电脑或使用ADB。

## 对应模块

- `NativeMemoryRules.java`：从本仓库固定源码的AST生成过滤、重要性和更正信号规则表。
- `NativeMemoryTime.java`：中文相对时间算法的 `java.time` 移植，保留原话并按观察时区解析日期。
- `NativeMemoryEngine.java`：本机过滤、去重、更正、归档、来源与检索；兼容已保存的记忆JSON。
- `NativeMemoryIntelligence.java`：手机直接复用已配置的聊天模型，执行语义ID选择与批量写入判断。不是将ONNX权重或Python解释器嵌入APK。

源代码、源项目对应关系和逐文件校验值见 [SOURCE_MAP.md](https://github.com/ktao732084-arch/dafeiyu-android/blob/v0.4.0/SOURCE_MAP.md)。原生语义路径与本仓库向量路径实现不同，不能将Python/ONNX模型的分数直接套到手机模型路径。

## 验证

手机原生引擎在不启动Python、不联网的条件下重放本仓库未经修改的84个场景，规则模式145/166，与已有规则基线一致；21项已知不足继续保留。11项原生专项与原有Android测试合计37场景通过。模型配置复用、非法引用、原子性、删除竞态等使用受控模型测试；真实手机、真实云模型质量和耗电仍待验证。

原来的 `companion_api.py` 保留为可选开发对照方式，参见 [ANDROID-COMPANION.md](ANDROID-COMPANION.md)，不是手机运行的前置条件。
