import os

import yaml

from utils.skill_paths import resolve_skills_root


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
    
    def __init__(self, skills_dir: str | None = None):
        # 默认按源码 / 环境变量 / wheel 安装位置解析；显式传入时以传入为准
        if skills_dir:
            current_file_path = os.path.abspath(__file__)
            project_root = os.path.dirname(os.path.dirname(current_file_path))
            self.skills_dir = os.path.join(project_root, skills_dir)
        else:
            self.skills_dir = str(resolve_skills_root())
        self.skills: dict[str, dict] = {}
        
    def load_skills(self) -> dict[str, dict]:
        """
        读取所有 SKILL.md 文件并解析 frontmatter
        返回格式: { "skill-name": { "name": "...", "description": "..." } }
        """
        if not os.path.exists(self.skills_dir):
            print(f"Warning: Skills directory {self.skills_dir} not found.")
            return {}

        try:
            entries = os.listdir(self.skills_dir)
        except OSError as exc:
            # 目录不可读（权限 / 磁盘故障）：技能清单为空而不是抛 PermissionError
            print(f"Warning: cannot list skills directory {self.skills_dir}: {exc}")
            return {}

        for skill_name in entries:
            skill_path = os.path.join(self.skills_dir, skill_name)
            if os.path.isdir(skill_path):
                md_file = os.path.join(skill_path, "SKILL.md")
                if not os.path.exists(md_file):
                    print(f"Warning: missing SKILL.md in {skill_path}, skipped.")
                if os.path.exists(md_file):
                    skill_info = self._parse_skill_md(md_file)
                    if not skill_info:
                        print(
                            f"Warning: SKILL.md of {skill_name} has no valid "
                            "frontmatter, skipped."
                        )
                    if skill_info:
                        # name 必须是字符串：frontmatter 写错类型时退回目录名，
                        # 不能拿列表等不可哈希值当字典键
                        info_name = skill_info.get("name")
                        key = (
                            info_name
                            if isinstance(info_name, str) and info_name
                            else skill_name
                        )
                        self.skills[key] = skill_info
                        
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
                    print(f"Error parsing YAML in {file_path}: {e}")
                    return None
                # frontmatter 不是映射（如整段是字符串 / 列表）时跳过，
                # 否则后续 .get() 会抛 AttributeError 让所有技能加载失败
                if not isinstance(data, dict):
                    print(
                        f"Warning: frontmatter of {file_path} is not a mapping, skipped."
                    )
                    return None
                return data
            return None
        except Exception as e:
            print(f"Error reading {file_path}: {e}")
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
        
        # 排序以保证确定性
        sorted_skills = sorted(self.skills.items())
        
        for name, info in sorted_skills:
            display_name = name
            if skill_mapping and name in skill_mapping:
                display_name = skill_mapping[name]
            elif skill_mapping:
                # 尝试反向查找：如果name是目录名，也试试看有没有在mapping里
                pass
                
            desc = str(info.get("description") or "")
            desc = desc.replace(chr(10), " ")
            prompt_lines.append(f"{index}. {display_name} - {desc}")
            index += 1
            
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
        
        # 2. 如果没找到，尝试按 metadata 中的 name 匹配 (遍历)
        if not target_path:
            if not os.path.isdir(self.skills_dir):
                return None
            try:
                entries = os.listdir(self.skills_dir)
            except OSError as exc:
                print(
                    f"Warning: cannot list skills directory {self.skills_dir}: {exc}"
                )
                return None
            for dirname in entries:
                skill_dir = os.path.join(self.skills_dir, dirname)
                if not os.path.isdir(skill_dir):
                    continue
                    
                md_path = os.path.join(skill_dir, "SKILL.md")
                if os.path.exists(md_path):
                    # 简单读取 name
                    info = self._parse_skill_md(md_path)
                    if info and info.get("name") == skill_name:
                        target_path = md_path
                        break

        if not target_path:
            return None
            
        try:
            # utf-8-sig：BOM 既不能让技能被跳过，也不能原样混进注入的正文
            with open(target_path, 'r', encoding='utf-8-sig') as f:
                content = f.read()
            return _strip_frontmatter(content)
        except Exception as e:
            print(f"Error reading skill content {target_path}: {e}")
            return None

