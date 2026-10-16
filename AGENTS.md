# 项目开发约定

## 工作规则

以下规则作为本项目开发与表达的优先约定；与本文其他风格约定冲突时，以本节为准。

- 代码实现遵循 ponytail full：简单、复用、不过度设计。
- 回复表达遵循 i-have-adhd：先结论、编号步骤、明确下一步。
- 汇报一律简约：只讲结论和重点，不铺垫、不解释过程、不重复背景。
- 格式冲突以 i-have-adhd 为准；不为简短省略必要的安全检查和测试。
- 若当前环境无法使用 i-have-adhd 技能，仍按上述明确的表达要求执行，不声称已加载该技能。

## 默认开发风格

- 所有开发默认使用 `@ponytail full`：优先最简单、最小的可行改动。
- 不增加没有明确需求的功能、抽象、依赖或重构。
- 保留现有功能和用户数据；修改前先检查现有实现。
- 用户说“先不操作”“只讨论”时，只说明方案，不编辑代码、不打包。
- 用户明确说“开始”后才执行修改、测试和打包。

## 项目路径

以下路径都以本文件所在目录（项目根目录，当前为
`C:\Users\Administrator\Desktop\每天工具\飞机系列+N卡识别\飞机抓图`）为基准；
整个目录搬动后不需要改这份文档。

- 源码：`work\telegram_caption_downloader_gui.py`
- 测试：`work\test_telegram_caption_downloader_gui.py`
- 群配置：`outputs\群配置`
- 结果目录：`结果`
- 运行日志：`outputs\运行日志`

## 测试与打包

- 修改后运行（在 `work` 目录、用 `work\.venv` 的解释器）：
  `.\.venv\Scripts\python.exe -m unittest test_telegram_caption_downloader_gui.py`
- 同时运行：`.\.venv\Scripts\python.exe -m py_compile telegram_caption_downloader_gui.py`
- 打包前必须测试通过。
- 打包命令（在 `work` 目录执行）：
  `.\.venv\Scripts\python.exe -m PyInstaller --noconfirm --distpath dist --workpath pyinstaller_current 登录飞机提取图片.spec`
  必须用 `work\.venv` 的解释器：只有它带 `paddlepaddle-gpu` 的包元数据，用系统 Python 打包会在
  `copy_metadata('paddlepaddle-gpu')` 处直接失败。
- 打包产物在 `work\dist\登录飞机提取图片\`，是 onedir 形式（exe + `_internal`）。
- 正式软件始终使用同一个文件：
  `outputs\登录飞机提取图片.exe`
- 不生成或交付 `v4`、`v5` 等新的正式 EXE 文件；版本号只显示在软件界面中，更新时覆盖原 EXE。
- 只改到 Python 代码时，把新的 `登录飞机提取图片.exe` 覆盖到 `outputs\` 即可，`_internal` 里的依赖
  没变可以不动；只有依赖版本变化时才需要同步 `_internal`。

## 数据和安全

- 群配置使用 JSON，不使用 TXT 作为配置格式。
- 不删除登录会话、群配置或结果文件，除非用户明确要求。
- 不执行危险的递归删除或覆盖操作；清理功能只作用于用户指定的结果范围。
