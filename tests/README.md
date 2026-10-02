# 测试运行

先在仓库根目录安装开发依赖与前端依赖：

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
npm --prefix web ci
```

## 后端

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m pytest -q -m "not audio and not packaging"
.\.venv\Scripts\python.exe -m pytest -q -m audio
.\.venv\Scripts\python.exe -m pytest -q -m packaging
```

默认执行全部测试。`audio` 选择编码、真实音频处理和依赖时间的测试，`packaging` 选择安装与打包契约；分组定义位于 `conftest.py`。公共测试辅助对象位于 `support/`。

原生 AirPlay 2 运行时测试需要 Linux，并将 `MICAST_TEST_NATIVE_RUNTIME` 设置为 AirPlay 2 原生运行时目录。其余测试通过模拟服务与临时数据验证业务行为。

## 前端

```powershell
npm --prefix web test
npm --prefix web run typecheck
npm --prefix web run build
```

浏览器测试使用 Playwright 与模拟接口，覆盖响应式布局、播放器、引导、调音、歌词、组合和异常重试。可通过 `PLAYWRIGHT_EXECUTABLE_PATH` 指定浏览器程序；Windows 自动检测 Microsoft Edge。其他环境可安装 Playwright Chromium。

## 维护

测试覆盖实际业务入口、协议兼容、并发取消、接管、恢复和资源释放。共享数据使用辅助模块；静态打包检查固定必要契约。发布检查包含完整后端测试、浏览器测试、类型检查和生产构建。
