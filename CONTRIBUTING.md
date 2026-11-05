# 贡献指南

感谢你愿意参与改进 Aligo 智能旅行助手！请遵循以下约定。

## 沟通语言

本仓库的 Issue、Pull Request 标题与描述、提交信息统一使用**中文**（代码标识符与注释可用英文）。

## 开发环境

```bash
# 1. 安装运行依赖（含 RAG 重型依赖，首次安装较慢）
pip install -r requirements.txt

# 2. 配置大模型 API Key（二选一，密钥切勿提交到仓库）
#    方式一：在项目根目录创建 config_local.py，内容为：API_KEY = "sk-..."
#    方式二：设置环境变量 LLM_API_KEY
```

## 本地检查（提交前必须通过）

```bash
# 代码风格检查
ruff check .

# 类型检查
mypy .

# 回归测试（不需要 API Key，使用确定性 mock 模型）
pytest tests/ -q
```

如果不想安装 torch / pymilvus 等重型依赖，可以只安装轻量测试依赖（与 CI 环境一致）：

```bash
pip install -r requirements-ci.txt
```

### 提交前钩子（可选）

```bash
pip install pre-commit && pre-commit install
```

之后每次 `git commit` 会自动运行 ruff 与基础文件检查（配置见 `.pre-commit-config.yaml`）。

## 分支与提交规范

- 分支命名：`feat/简短说明`、`fix/简短说明`、`docs/简短说明`、`chore/简短说明`
- 提交信息采用 [Conventional Commits](https://www.conventionalcommits.org/) 前缀（`feat` / `fix` / `docs` / `chore` / `test` / `refactor`），描述使用中文
- 一个提交只做一件事，避免混合无关改动

## Pull Request

- 标题格式：`类型: 中文描述`，例如 `fix: 修复偏好查询永远为空的问题`
- 使用仓库的 PR 模板（创建 PR 时自动带出），完整填写变更说明与验证方式
- 关联相关 Issue（正文写 `Closes #123`）
- 提交前确保 CI 全绿（ruff / mypy / pytest）
- PR 标题与描述请使用中文

## 发布

推送 `v*` 标签会触发 `.github/workflows/release.yml`：自动构建 sdist/wheel 并创建 GitHub Release。

发布前检查清单：

1. 同步版本号（`python cli.py doctor` 会检查 `pyproject.toml`、`index.html` 静态资源 `?v=` 与 `utils/version.py` 是否一致）：
   - `pyproject.toml` 的 `version`
   - `utils/version.py` 的 `APP_VERSION`
   - `webapp/static/index.html` 中静态资源引用 `?v=` 查询参数（避免浏览器继续用旧缓存）
   - README 安装示例中的 wheel 文件名
2. 把 `CHANGELOG.md` 的 `[Unreleased]` 整理为 `[x.y.z] - 日期`，必要时用中文精选说明替换自动生成的 Release notes
3. 本地检查通过：`ruff check .`、`mypy .`、`pytest tests/ -q`、`python cli.py doctor`
4. 合并版本 PR 后，在 `main` 上创建 annotated tag 并推送：

```bash
git tag -a v1.0.1 -m "v1.0.1: 简要说明"
git push origin v1.0.1
```

5. 确认 Release workflow 成功、wheel / sdist 资产可下载；建议在全新虚拟环境安装 wheel 后做一次冒烟（`aligo-web --version`、`/api/health` 的 `skills.count`、静态页面）
6. 如果发布后发现问题（如 wheel 缺文件），保留原 tag / Release，在 Release 说明中标注替代版本；不要重写已发布的标签

## 敏感信息

- **不要提交任何 API Key、密码、内网地址**；
- 本地配置请放在 `config_local.py`（已在 `.gitignore` 中）；
- `data/memory/`、`data/models/`、向量库文件等运行时产物同样不应提交。

## Issue 规范

- Bug 反馈请使用 Bug 模板，附上复现步骤、期望结果、实际结果与环境信息；
- 功能建议请使用 Feature 模板，说明使用场景与期望行为。
