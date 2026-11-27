import os
import sys

import yaml

from utils.skill_paths import resolve_skills_root

# 技能 description / 正文来自可写目录（ALIGO_SKILLS_DIR 支持外部技能包），
# 原样注入会让每轮意图 prompt 或技能调用 prompt 无限膨胀：description 每轮
# 进意图 prompt（单条 500 / 合计 4000，与偏好注入预算同口径），正文进技能
# prompt（单份 20000 字符上限，超出截断并注明）。
MAX_SKILL_DESCRIPTION_CHARS = 500
MAX_SKILL_PROMPT_CHARS = 4000
MAX_SKILL_CONTENT_CHARS = 20000
# 预算告警按技能目录去重：CLI / Web 都是长期进程，每轮重复刷屏没有价值
_WARNED_BUDGET_DIRS: set[str] = set()


def _frontmatter_bounds(content: str) -> tuple[int, int] | None:
    """定位 markdown frontmatter 的 YAML 区间；没有 frontmatter 时返回 None。

    结束标记必须是独立的一行 ``---``（允许行尾空白）：描述值里出现
    ``---`` 时，裸 ``find('---', 3)`` 会在这里提前截断，把残余 frontmatter
    混进技能正文。
    """
    if not content.startswith("---"):
        return None
    first_newline = content.find("\n")
    if first_newline < 0:
        return None
    # 开头围栏必须精确为 `---`（允许行尾空白 / CR）：`----` / `---foo`
    # 这类行是普通正文（如 4 连横线的分隔线），当成围栏会把后面直到某个
    # 独立 --- 行的正文误解析成 frontmatter，登记伪技能并截断注入正文
    if content[:first_newline].rstrip() != "---":
        return None
    yaml_start = first_newline + 1
    index = yaml_start
    while index <= len(content):
        newline = content.find("\n", index)
        line = content[index:] if newline < 0 else content[index:newline]
        # 结束标记必须顶格（只允许行尾空白 / CR）：缩进的 `  ---` 是 YAML
        # 块标量内的内容，按 strip() 判断会提前截断 frontmatter
        if line.rstrip() == "---":
            return yaml_start, index
        if newline < 0:
            return None
        index = newline + 1
    return None


def _strip_frontmatter(content: str) -> str:
    """去掉 frontmatter，返回技能正文（没有 frontmatter 时原样返回）。"""
    bounds = _frontmatter_bounds(content)
    if bounds is None:
        return content.strip()
    closing_start = bounds[1]
    newline = content.find("\n", closing_start)
    if newline < 0:
        return ""
    return content[newline + 1:].strip()


class SkillLoader:
    """加载 .claude/skills 下的技能描述"""

    def __init__(self, skills_dir: str | None = None, dispatchable_only: bool = False):
        # 默认按源码 / 环境变量 / wheel 安装位置解析；显式传入时以传入为准
        if skills_dir:
            current_file_path = os.path.abspath(__file__)
            project_root = os.path.dirname(os.path.dirname(current_file_path))
            self.skills_dir = os.path.join(project_root, skills_dir)
        else:
            self.skills_dir = str(resolve_skills_root())
        # 只列出可被 LazyAgentRegistry 真正调度的技能（含 script/agent.py）：
        # 意图 prompt 若宣传纯文档技能，模型调度时会永远「未注册」
        self.dispatchable_only = dispatchable_only
        self.skills: dict[str, dict] = {}
        # key → 技能目录名 / SKILL.md 路径：宣传（load_skills）与注入
        # （get_skill_content）必须解析到同一份文件，否则重名技能会出现
        # 「宣传 B、注入 A」
        self._skill_dirs: dict[str, str] = {}
        self._skill_md_paths: dict[str, str] = {}
        
    def load_skills(self) -> dict[str, dict]:
        """
        读取所有 SKILL.md 文件并解析 frontmatter
        返回格式: { "skill-name": { "name": "...", "description": "..." } }
        """
        if not os.path.exists(self.skills_dir):
            print(f"Warning: Skills directory {self.skills_dir} not found.", file=sys.stderr)
            return {}

        try:
            entries = os.listdir(self.skills_dir)
        except OSError as exc:
            # 目录不可读（权限 / 磁盘故障）：技能清单为空而不是抛 PermissionError
            print(f"Warning: cannot list skills directory {self.skills_dir}: {exc}", file=sys.stderr)
            return {}

        # 全量重扫：先清空上次结果，保证重复调用幂等（否则每一步都会把
        # 自己上次登记的名字当成目录间重名反复告警）
        self.skills = {}
        self._skill_dirs = {}
        self._skill_md_paths = {}

        # 排序遍历：重名技能保留谁必须确定（os.listdir 顺序随文件系统变化，
        # 同一目录两次启动可能宣传不同技能）
        for skill_name in sorted(entries):
            skill_path = os.path.join(self.skills_dir, skill_name)
            if not os.path.isdir(skill_path):
                continue
            if self.dispatchable_only and not os.path.exists(
                os.path.join(skill_path, "script", "agent.py")
            ):
                continue
            md_file = os.path.join(skill_path, "SKILL.md")
            if not os.path.exists(md_file):
                print(f"Warning: missing SKILL.md in {skill_path}, skipped.", file=sys.stderr)
                continue
            skill_info = self._parse_skill_md(md_file)
            if not skill_info:
                print(
                    f"Warning: SKILL.md of {skill_name} has no valid "
                    "frontmatter, skipped.",
                    file=sys.stderr,
                )
                continue
            # name 必须是字符串：frontmatter 写错类型时退回目录名，
            # 不能拿列表等不可哈希值当字典键
            info_name = skill_info.get("name")
            key = (
                info_name
                if isinstance(info_name, str) and info_name
                else skill_name
            )
            existing_dir = self._skill_dirs.get(key)
            if existing_dir is not None:
                if key == skill_name and existing_dir != key:
                    # 目录名与 frontmatter name 相同：与 get_skill_content
                    # 的目录直配同向，让该目录自己的 SKILL.md 胜出
                    print(
                        f"Warning: duplicate skill name {key!r}: "
                        f"preferring directory {skill_name!r} over "
                        f"{existing_dir!r}.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"Warning: duplicate skill name {key!r} in "
                        f"{existing_dir!r} and {skill_name!r}, "
                        f"keeping {existing_dir!r}.",
                        file=sys.stderr,
                    )
                    continue
            self.skills[key] = skill_info
            self._skill_dirs[key] = skill_name
            self._skill_md_paths[key] = md_file

        return self.skills

    def _parse_skill_md(self, file_path: str) -> dict | None:
        """解析 markdown 文件的 yaml frontmatter"""
        try:
            # utf-8-sig：带 BOM 的 SKILL.md 此前 content.startswith('---')
            # 失败而被静默跳过
            with open(file_path, 'r', encoding='utf-8-sig') as f:
                content = f.read()

            bounds = _frontmatter_bounds(content)
            if bounds is not None:
                yaml_content = content[bounds[0]:bounds[1]]
                try:
                    data = yaml.safe_load(yaml_content)
                except yaml.YAMLError as e:
                    print(f"Error parsing YAML in {file_path}: {e}", file=sys.stderr)
                    return None
                # frontmatter 不是映射（如整段是字符串 / 列表）时跳过，
                # 否则后续 .get() 会抛 AttributeError 让所有技能加载失败
                if not isinstance(data, dict):
                    print(
                        f"Warning: frontmatter of {file_path} is not a mapping, skipped.",
                        file=sys.stderr,
                    )
                    return None
                # name 是意图 prompt / 注册器别名共用的键：带首尾空白
                # （引号标量、`name: |` 块标量）会让意图侧 strip 后登记、
                # 注册表按原始名匹配，同一技能永远「未注册」——入口统一归一
                name_value = data.get("name")
                if isinstance(name_value, str):
                    data["name"] = name_value.strip()
                return data
            return None
        except Exception as e:
            print(f"Error reading {file_path}: {e}", file=sys.stderr)
            return None

    def get_skill_prompt(self, skill_mapping: dict[str, str] | None = None) -> str:
        """
        生成用于 Prompt 的技能描述字符串
        
        Args:
            skill_mapping: 可选，将 skill name 映射为系统内部的 intent name
                          例如: {"plan-trip": "itinerary_planning"}
        """
        if not self.skills:
            self.load_skills()
            
        prompt_lines = []
        index = 1
        used = 0
        clipped = False

        # 排序以保证确定性
        sorted_skills = sorted(self.skills.items())

        for name, info in sorted_skills:
            display_name = name
            if skill_mapping and name in skill_mapping:
                display_name = skill_mapping[name]

            desc = str(info.get("description") or "")
            desc = desc.replace(chr(10), " ")
            # description 来自 SKILL.md 元数据，每轮意图 prompt 都会注入：
            # 单条与总量都要设上限，否则外部技能包可把 prompt 撑爆
            if len(desc) > MAX_SKILL_DESCRIPTION_CHARS:
                desc = desc[:MAX_SKILL_DESCRIPTION_CHARS] + "…"
                clipped = True
            line = f"{index}. {display_name} - {desc}"
            if used + len(line) + 2 > MAX_SKILL_PROMPT_CHARS:
                # 只省略描述，保留序号与技能名：整条省略会让该技能从
                # 意图清单里消失、永远不被调度
                line = f"{index}. {display_name} - （描述已省略）"
                clipped = True
            used += len(line) + 2
            prompt_lines.append(line)
            index += 1

        if clipped and self.skills_dir not in _WARNED_BUDGET_DIRS:
            _WARNED_BUDGET_DIRS.add(self.skills_dir)
            print(
                "Warning: skill descriptions exceed prompt budget in "
                f"{self.skills_dir}; truncated.",
                file=sys.stderr,
            )

        return "\n\n".join(prompt_lines)

    def get_skill_content(self, skill_name: str) -> str | None:
        """
        获取指定 Skill 的完整 markdown 内容（去除 frontmatter）
        用于执行阶段的 Prompt 注入
        """
        if not self.skills:
            self.load_skills()

        target_path = None

        # 1. 优先尝试直接按目录名匹配 (最快)
        test_dir_path = os.path.join(self.skills_dir, skill_name, "SKILL.md")
        if os.path.exists(test_dir_path):
            target_path = test_dir_path

        # 2. 否则按 metadata name 查找：使用 load_skills 记录的同一份
        #    路径映射，不再按 os.listdir 顺序重新扫描（重名技能时宣传与
        #    注入必须指向同一份正文）
        if not target_path:
            target_path = self._skill_md_paths.get(str(skill_name))

        if not target_path:
            return None

        try:
            # utf-8-sig：BOM 既不能让技能被跳过，也不能原样混进注入的正文
            with open(target_path, 'r', encoding='utf-8-sig') as f:
                content = f.read()
            content = _strip_frontmatter(content)
            if len(content) > MAX_SKILL_CONTENT_CHARS:
                # 只截断注入正文，load_skills / get_skill_prompt 行为不变
                content = (
                    content[:MAX_SKILL_CONTENT_CHARS]
                    + "\n…（技能正文过长已截断）"
                )
            return content
        except Exception as e:
            print(f"Error reading skill content {target_path}: {e}", file=sys.stderr)
            return None

