"""
Markdown 知识库加载器
按一级标题（# ）将 .md 文件切分为独立文档块。
每个块包含标题、内容文本和元数据（来源文件、标题层级等）。
"""

import os
import re
from dataclasses import dataclass, field # 装饰器，用于定义数据类
from loguru import logger


@dataclass
class DocumentChunk:
    """单个文档块，表示 .md 文件中一个一级标题下的内容"""
    chunk_id: str                   # 唯一标识符（文件名_标题哈希）
    title: str                      # 一级标题文本
    content: str                    # 该标题下的全部内容（包含子标题）
    source_file: str                # 来源文件名
    metadata: dict = field(default_factory=dict)  # 附加元数据 元数据：描述数据的数据 （如：来源文件、标题层级等）


class MarkdownLoader:
    """
    Markdown 文档加载器。
    - 读取 .md 文件内容
    - 按一级标题（# ）切分为文档块
    - 过滤空块，返回 DocumentChunk 列表
    """

    def load_and_split(self, file_path: str) -> list[DocumentChunk]:
        """
        加载单个 .md 文件并按一级标题切块。

        Args:
            file_path: .md 文件的绝对路径

        Returns:
            DocumentChunk 列表（每个一级标题对应一个块）
        """
        if not os.path.exists(file_path):
            logger.error(f"文件不存在: {file_path}")
            return []

        filename = os.path.basename(file_path) # 获取文件地址的文件名（不包括路径）
        logger.debug(f"加载文件: {filename}")

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                raw_content = f.read()
        except Exception as e:
            logger.error(f"读取文件失败 {file_path}: {e}")
            return []

        if not raw_content.strip():
            logger.warning(f"文件为空: {filename}")
            return []

        chunks = self._split_by_h1(raw_content, filename)
        logger.debug(f"文件 {filename} 切分为 {len(chunks)} 块")
        return chunks

    def load_directory(self, dir_path: str) -> list[DocumentChunk]:
        """
        加载目录下所有 .md 文件。

        Args:
            dir_path: 目录路径

        Returns:
            所有文档块的合并列表
        """
        if not os.path.exists(dir_path):
            logger.error(f"目录不存在: {dir_path}")
            return []

        all_chunks: list[DocumentChunk] = []
        md_files = [f for f in os.listdir(dir_path) if f.endswith(".md")]

        for md_file in md_files:
            file_path = os.path.join(dir_path, md_file)
            chunks = self.load_and_split(file_path)
            all_chunks.extend(chunks)

        logger.info(f"目录 {dir_path} 共加载 {len(all_chunks)} 个文档块")
        return all_chunks

    def _split_by_h1(self, content: str, source_file: str) -> list[DocumentChunk]:
        """
        按一级标题（# ）将文本切分为块。
        文件开头（第一个 # 之前）的内容作为"前言"块处理。

        Args:
            content: 原始 Markdown 文本
            source_file: 来源文件名（用于生成 chunk_id 和元数据）

        Returns:
            DocumentChunk 列表
        """
        # 使用正则按 # 开头的一级标题分割（不包含 ## 等子标题）
        
        #^：匹配字符串的‌开头‌。
        # # ：字面量匹配 ‌# 后跟一个空格‌。
        # (.+)：
        # . 匹配任意字符（除换行符外）；
        # + 表示前面的字符（即 .）‌出现一次或多次‌；
        # 括号 () 构成一个‌捕获组‌，用于提取匹配的内容。
        # $：匹配字符串的‌结尾‌。
        # re.MULTILINE 标志：
        # 使 ^ 和 $ 分别匹配‌每一行的开头和结尾‌，而不仅仅是整个字符串的开头和结尾。
        
        pattern = re.compile(r"^# (.+)$", re.MULTILINE)
        # # finditer() 返回一个迭代器，包含所有匹配的结果
        matches = list(pattern.finditer(content))

        # match.start() 返回匹配的开始位置
        # match.end() 返回匹配的结束位置
        # match.span() 返回匹配的开始和结束位置
        # match.groupdict() 返回一个字典，包含所有捕获组的名称和对应的值
        # match.groups() 返回一个元组，包含所有捕获组的值
        # match.group(0) 返回整个匹配的文本
        # match.group(1) 返回第一个捕获组（即第一个括号内的内容）
        # match.group(2) 返回第二个捕获组（即第二个括号内的内容）
        # match.group(3) 返回第三个捕获组（即第三个括号内的内容）
        
        chunks: list[DocumentChunk] = []

        # 处理第一个 # 标题之前的前言内容
        if matches and matches[0].start() > 0:
            preamble = content[:matches[0].start()].strip()
            if preamble:
                chunks.append(self._make_chunk(
                    title="前言",
                    content=preamble,
                    source_file=source_file,
                    index=0,
                ))

        # 按每个一级标题切块
        for i, match in enumerate(matches):
            title = match.group(1).strip() # 匹配到的第一个括号内的内容，即第一个换行符前的内容
            # 内容范围：当前标题行开始 → 下一个标题行之前（或文件末尾）
            start = match.start()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            chunk_text = content[start:end].strip()

            if not chunk_text:
                continue

            chunks.append(self._make_chunk(
                title=title,
                content=chunk_text,
                source_file=source_file,
                index=i + 1,
            ))

        # 若文件无任何一级标题，整个文件作为一个块
        if not chunks and content.strip():
            file_name_no_ext = os.path.splitext(source_file)[0]
            chunks.append(self._make_chunk(
                title=file_name_no_ext,
                content=content.strip(),
                source_file=source_file,
                index=0,
            ))

        return chunks
    
    #静态方法不需要实例化可以直接调用，实例化后也能调用，可以理解成函数。
    @staticmethod
    def _make_chunk(title: str, content: str, source_file: str, index: int) -> DocumentChunk:
        """
        创建 DocumentChunk 对象。

        Args:
            title: 标题文本
            content: 块内容
            source_file: 来源文件名
            index: 块在文件中的序号

        Returns:
            DocumentChunk 实例
        """
        # chunk_id 格式：文件名（去扩展名）_序号_标题前20字符
        
        # [^...] 表示"匹配不在括号内的任何字符"
        # \w 表示单词字符：字母、数字、下划线（a-z、A-Z、0-9、_）
        # \u4e00-\u9fff 表示 Unicode 中的中文字符范围（包括中文、日文、韩文）
        # re.sub(pattern, repl, string) 替换字符串中匹配的子字符串
        # 组合起来：替换所有"不是单词字符也不是中文"的字符为 "_"字符，并取前20个字符
        safe_title = re.sub(r"[^\w\u4e00-\u9fff]", "_", title)[:20]
        file_stem = os.path.splitext(source_file)[0] # 将文件名分割成"主文件名"和"扩展名"两部分，并返回主文件名
        chunk_id = f"{file_stem}_{index}_{safe_title}" # 拼接文件名、序号和标题前20字符，形成唯一标识符

        return DocumentChunk(
            chunk_id=chunk_id,
            title=title,
            content=content,
            source_file=source_file,
            metadata={
                "source": source_file,
                "title": title,
                "index": index,
                "char_count": len(content),
            },
        )
